"""LLM extraction stage: per-chunk typed mentions + topics + relations.

Design:
- Group canonical items into conversation chunks (chunking.py).
- Call the LLM with a tool-use JSON schema so output is structured.
- Verify every claim against the source (verify.py); drop failures.
- Cache each chunk call by sha256(model, prompt_version, chunk_text).
- Same input -> identical bundle.

The old `extract_items` interface (used by build.py._llm_stage) is preserved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..schema import SourceItem
from .client import LLMClient, get_llm_client
from .chunking import Chunk, chunk_items
from .prompts import EXTRACT_V1, EXTRACT_VERSION
from .verify import verify, VerifyResult

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "llm"

# ---------------------------------------------------------------------------
# Tool-use schema (sent to the LLM as a tool definition).
# ---------------------------------------------------------------------------

EXTRACT_TOOL = {
    "name": "extract_mentions",
    "description": (
        "Extract entity mentions, per-message topics, and relations "
        "from the conversation segment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "description": "Per-message topic labels and reasoning.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":     {"type": "string", "description": "Message local id (e.g. m1)."},
                        "topics": {"type": "array", "items": {"type": "string"},
                                   "description": "1-3 short topic labels."},
                        "reason": {"type": "string", "description": "One-line reason."},
                    },
                    "required": ["id", "topics", "reason"],
                },
            },
            "mentions": {
                "type": "array",
                "description": "Entity mentions found in the messages.",
                "items": {
                    "type": "object",
                    "properties": {
                        "message_id":    {"type": "string"},
                        "surface":       {"type": "string", "description": "Exact surface form from the text."},
                        "type":          {"type": "string",
                                          "enum": ["person","org","location","money","document","event","unknown"]},
                        "participant_id":{"type": ["string","null"], "description": "Participant table pid if this refers to a known participant."},
                        "local_entity":  {"type": "string", "description": "Short stable key shared by co-referent mentions."},
                        "clues": {
                            "type": "object",
                            "properties": {
                                "email":             {"type": ["string","null"]},
                                "phone":             {"type": ["string","null"]},
                                "affiliation":       {"type": ["string","null"]},
                                "role":              {"type": ["string","null"]},
                                "relation_to_owner": {"type": ["string","null"]},
                            },
                        },
                    },
                    "required": ["message_id", "surface", "type", "local_entity"],
                },
            },
            "relations": {
                "type": "array",
                "description": "Relations between entities.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type":                {"type": "string",
                                               "enum": ["works_at","paid","sent_document","located_in",
                                                        "organizes","attends","member_of","booked"]},
                        "subject_local_entity":{"type": "string"},
                        "object_local_entity": {"type": "string"},
                        "message_id":          {"type": "string"},
                        "evidence_span":       {"type": "string", "description": "Exact text span from the message."},
                    },
                    "required": ["type","subject_local_entity","object_local_entity","message_id","evidence_span"],
                },
            },
        },
        "required": ["messages", "mentions", "relations"],
    },
}


# ---------------------------------------------------------------------------
# Dataclasses for extracted results
# ---------------------------------------------------------------------------

@dataclass
class MentionExtraction:
    message_id: str     # local chunk id (m1..)
    real_item_id: str   # mapped back from chunk.id_map
    surface: str
    type: str
    participant_id: str | None
    local_entity: str
    clues: dict = field(default_factory=dict)


@dataclass
class RelationExtraction:
    type: str
    subject_local_entity: str
    object_local_entity: str
    real_item_id: str
    evidence_span: str


@dataclass
class MessageTopics:
    real_item_id: str
    topics: list[str]
    reason: str


@dataclass
class ChunkExtraction:
    chunk_id: str
    conversation_id: str
    messages: list[MessageTopics] = field(default_factory=list)
    mentions: list[MentionExtraction] = field(default_factory=list)
    relations: list[RelationExtraction] = field(default_factory=list)
    verify_result: VerifyResult | None = None
    from_cache: bool = False


# ---------------------------------------------------------------------------
# Old-style result (kept for backward-compatibility with build.py._llm_stage)
# ---------------------------------------------------------------------------

@dataclass
class ItemExtraction:
    item_id: str
    relevant: bool
    rationale: str
    entities: list[dict] = field(default_factory=list)  # {type,name,surface}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _chunk_cache_key(chunk: Chunk, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(EXTRACT_VERSION.encode())
    h.update(chunk.full_text.encode())
    return h.hexdigest()[:20]


def _parse_tool_use(text: str) -> dict:
    """Try to extract JSON from a tool-use response or plain text fallback."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"messages": [], "mentions": [], "relations": []}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"messages": [], "mentions": [], "relations": []}


# ---------------------------------------------------------------------------
# Per-chunk extraction
# ---------------------------------------------------------------------------

_LLM_TIMEOUT = int(os.getenv("LLM_CHUNK_TIMEOUT", "60"))


def _call_llm_once(client: LLMClient, user_content: str) -> dict:
    """Single LLM call, raising on any error."""
    if hasattr(client, "complete_tool"):
        text = client.complete_tool(EXTRACT_V1, user_content, EXTRACT_TOOL)
    else:
        schema_hint = json.dumps(EXTRACT_TOOL["input_schema"], indent=2)
        text = client.complete_json(
            EXTRACT_V1 + "\n\nReturn ONLY valid JSON matching this schema:\n" + schema_hint,
            user_content,
        )
    return _parse_tool_use(text)


def _extract_chunk(
    chunk: Chunk,
    client: LLMClient,
    model: str,
    use_cache: bool,
) -> ChunkExtraction:
    import concurrent.futures
    import logging

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _chunk_cache_key(chunk, model)
    cache_f = CACHE_DIR / f"{key}.json"

    if use_cache and cache_f.exists():
        raw = json.loads(cache_f.read_text())
        from_cache = True
    else:
        user_content = chunk.full_text
        raw = None
        from_cache = False
        # Try once; on any error retry once; on second failure use empty fallback.
        for attempt in range(2):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(_call_llm_once, client, user_content)
                    raw = fut.result(timeout=_LLM_TIMEOUT)
                break
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "LLM extraction attempt %d failed for chunk %s: %s",
                    attempt + 1, chunk.chunk_id, exc,
                )
        if raw is None:
            # Fallback: heuristic extraction for this chunk (empty LLM output).
            raw = {"messages": [], "mentions": [], "relations": []}
        cache_f.write_text(json.dumps(raw))

    vresult = verify(chunk, raw)
    ce = ChunkExtraction(
        chunk_id=chunk.chunk_id,
        conversation_id=chunk.conversation_id,
        verify_result=vresult,
        from_cache=from_cache,
    )

    for msg in vresult.messages:
        mid = msg.get("id", "")
        real_id = chunk.id_map.get(mid, mid)
        ce.messages.append(MessageTopics(
            real_item_id=real_id,
            topics=msg.get("topics", []),
            reason=msg.get("reason", ""),
        ))

    for m in vresult.mentions:
        mid = m.get("message_id", "")
        real_id = chunk.id_map.get(mid, mid)
        ce.mentions.append(MentionExtraction(
            message_id=mid,
            real_item_id=real_id,
            surface=m.get("surface", ""),
            type=m.get("type", "unknown"),
            participant_id=m.get("participant_id"),
            local_entity=m.get("local_entity", ""),
            clues=m.get("clues", {}),
        ))

    for rel in vresult.relations:
        mid = rel.get("message_id", "")
        real_id = chunk.id_map.get(mid, mid)
        ce.relations.append(RelationExtraction(
            type=rel.get("type", ""),
            subject_local_entity=rel.get("subject_local_entity", ""),
            object_local_entity=rel.get("object_local_entity", ""),
            real_item_id=real_id,
            evidence_span=rel.get("evidence_span", ""),
        ))

    return ce


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_chunks(
    items: list[SourceItem],
    client: LLMClient | None = None,
    concurrency: int = 5,
    model: str | None = None,
    use_cache: bool = True,
    progress=None,
) -> list[ChunkExtraction]:
    """Chunk items and run LLM extraction over each chunk."""
    model = model or os.getenv("LLM_EXTRACT_MODEL") or os.getenv("LLM_MODEL", "claude-haiku-4-5")
    client = client or get_llm_client(model)
    chunks = chunk_items(items)

    done = [0]
    results: list[ChunkExtraction] = []

    def run(chunk: Chunk) -> ChunkExtraction:
        r = _extract_chunk(chunk, client, model, use_cache)
        done[0] += 1
        if progress:
            progress(done[0], len(chunks))
        return r

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = list(ex.map(run, chunks))

    return results


def extract_items(
    items: list[SourceItem],
    client: LLMClient | None = None,
    batch_size: int = 14,      # kept for API compat; unused (chunking handles sizing)
    concurrency: int = 5,
    model: str | None = None,
    use_cache: bool = True,
    progress=None,
) -> list[ItemExtraction]:
    """Backward-compatible interface: returns one ItemExtraction per SourceItem.

    Wraps extract_chunks and flattens per-chunk extractions back to per-item.
    The old `relevant` field is derived from whether the item got any topics.
    """
    chunk_results = extract_chunks(
        items, client=client, concurrency=concurrency,
        model=model, use_cache=use_cache, progress=progress,
    )

    # Accumulate per real_item_id.
    item_topics: dict[str, list[str]] = {}
    item_reasons: dict[str, str] = {}
    item_entities: dict[str, list[dict]] = {}

    for ce in chunk_results:
        for mt in ce.messages:
            item_topics.setdefault(mt.real_item_id, []).extend(mt.topics)
            item_reasons.setdefault(mt.real_item_id, mt.reason)
        for mn in ce.mentions:
            item_entities.setdefault(mn.real_item_id, []).append({
                "type": mn.type,
                "name": mn.local_entity or mn.surface,
                "surface": mn.surface,
                "clues": mn.clues,
                "participant_id": mn.participant_id,
            })

    results: list[ItemExtraction] = []
    for it in items:
        topics = item_topics.get(it.id, [])
        relevant = bool(topics)
        rationale = item_reasons.get(it.id, "no topics returned by LLM")
        entities = item_entities.get(it.id, [])
        results.append(ItemExtraction(
            item_id=it.id,
            relevant=relevant,
            rationale=rationale if rationale else (", ".join(topics) if topics else "no topics"),
            entities=entities,
        ))

    return results
