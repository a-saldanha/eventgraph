"""LLM extraction stage: per-item relevance + typed entities, provenance-preserving.

Design for scale (the corpus is ~1,458 items):
  - batch many short items into one call (distillation, not per-item calls),
  - cache each batch by content hash so re-runs are free,
  - run batches concurrently (client handles rate-limit backoff).
Raw item text is never mutated — extractions point back to item_ids.
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..schema import SourceItem
from .client import LLMClient, get_llm_client

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "llm"

SYSTEM = """You extract structured information from a person's messy personal archive
(emails, WhatsApp, documents) that surrounds ONE real event: an academic paper's
journey from conference acceptance to attending/presenting at the conference
(registration, payment, visa, flights, accommodation, the event itself).

For EACH input item decide:
1) relevant: is this item about THAT conference-trip event? (false for unrelated
   work, other trips, group chatter, illness, spam).
2) a short rationale.
3) entities it mentions, each typed as one of:
   person | org | location | money | document | date
   Give a normalized `name` (canonical form) and the `surface` text as it appeared.

Return ONLY JSON:
{"items":[{"id":"<item id>","relevant":true,"rationale":"...","entities":[
  {"type":"person","name":"Jane Smith","surface":"jsmith@x.com"}, ...]}]}
Never invent items or ids. Only use ids present in the input."""


@dataclass
class ItemExtraction:
    item_id: str
    relevant: bool
    rationale: str
    entities: list[dict] = field(default_factory=list)  # {type,name,surface}


def _fmt_item(it: SourceItem) -> str:
    body = re.sub(r"\s+", " ", it.body)[:800]
    subj = f" | subject: {it.subject}" if it.subject else ""
    return f"[{it.id}] ({it.source_type.value}) from: {it.sender[:60]}{subj}\n{body}"


def _batches(items: list[SourceItem], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _batch_key(batch: list[SourceItem], model: str) -> str:
    h = hashlib.sha256(model.encode())
    for it in batch:
        h.update(it.content_hash.encode())
    return h.hexdigest()[:20]


def _parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0)).get("items", [])
    except json.JSONDecodeError:
        return []


def extract_items(
    items: list[SourceItem],
    client: LLMClient | None = None,
    batch_size: int = 14,
    concurrency: int = 5,
    model: str | None = None,
    use_cache: bool = True,
    progress=None,
) -> list[ItemExtraction]:
    import os

    # One source of truth for the model: the cache key AND the client use it, so
    # switching the extraction model can't silently mismatch the cache.
    model = model or os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    client = client or get_llm_client(model)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    batches = list(_batches(items, batch_size))
    results: dict[str, ItemExtraction] = {}
    done = [0]

    def run_batch(batch: list[SourceItem]) -> list[tuple[str, dict]]:
        key = _batch_key(batch, model)
        cache_f = CACHE_DIR / f"{key}.json"
        if use_cache and cache_f.exists():
            raw = json.loads(cache_f.read_text())
        else:
            user = "Items:\n\n" + "\n\n".join(_fmt_item(it) for it in batch)
            text = client.complete_json(SYSTEM, user)
            raw = _parse(text)
            cache_f.write_text(json.dumps(raw))
        done[0] += 1
        if progress:
            progress(done[0], len(batches))
        # Map returned rows to THIS batch's current item ids: by echoed id when it
        # matches, else positionally (robust to id drift / cache reuse).
        batch_ids = {it.id for it in batch}
        pairs, leftover = [], []
        used = set()
        for row in raw:
            rid = row.get("id")
            if rid in batch_ids and rid not in used:
                pairs.append((rid, row)); used.add(rid)
            else:
                leftover.append(row)
        remaining = [it.id for it in batch if it.id not in used]
        for iid, row in zip(remaining, leftover):
            pairs.append((iid, row))
        return pairs

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for pairs in ex.map(run_batch, batches):
            for iid, row in pairs:
                results[iid] = ItemExtraction(
                    item_id=iid,
                    relevant=bool(row.get("relevant", False)),
                    rationale=str(row.get("rationale", "")),
                    entities=[e for e in row.get("entities", []) if e.get("type") and e.get("name")],
                )

    # items the model didn't return get a conservative default
    for it in items:
        results.setdefault(it.id, ItemExtraction(it.id, False, "not returned by extractor"))
    return [results[it.id] for it in items]
