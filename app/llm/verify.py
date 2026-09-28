"""Post-extraction verification: every claim must be grounded in the source text.

`verify(chunk, response)` checks each mention and relation returned by the LLM:
- message_id must exist in the chunk's id_map.
- `surface` must appear verbatim in that message's rendered line (after stripping
  invisible Unicode marks — same normalizer as the hasher).
- relation `evidence_span` must appear verbatim in the stated message's line.
- relation `type` must be in the closed vocabulary.

Anything that fails a check is dropped; counts are returned for logging.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .chunking import Chunk

_INVISIBLE_RE = re.compile(r"[‎‏‪-‮⁦-⁩ ]")

# Closed relation vocabulary (from the phase spec).
VALID_RELATION_TYPES: frozenset[str] = frozenset({
    "works_at", "paid", "sent_document", "located_in",
    "organizes", "attends", "member_of", "booked",
})

VALID_MENTION_TYPES: frozenset[str] = frozenset({
    "person", "org", "location", "money", "document", "event", "unknown",
})


def _strip_invisible(s: str) -> str:
    return _INVISIBLE_RE.sub("", s)


@dataclass
class VerifyResult:
    messages: list[dict] = field(default_factory=list)
    mentions: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=lambda: {
        "bad_message_id": 0,
        "surface_not_found": 0,
        "bad_relation_type": 0,
        "evidence_not_found": 0,
        "bad_mention_type": 0,
    })

    @property
    def total_rejected(self) -> int:
        return sum(self.rejected.values())


def _get_message_text(chunk: Chunk, local_id: str) -> str | None:
    """Return the rendered message line for a local id, or None if unknown."""
    if local_id not in chunk.id_map:
        return None
    # The rendered lines in chunk.message_lines are indexed by position.
    # We need to find the line with <local_id> at the start.
    for line in chunk.message_lines:
        if line.startswith(f"<{local_id}>"):
            return line
    return None


def verify(chunk: Chunk, response: dict[str, Any]) -> VerifyResult:
    """Verify an LLM response dict against the chunk's source text.

    Parameters
    ----------
    chunk:    The Chunk sent to the LLM.
    response: The parsed tool-use dict (keys: messages, mentions, relations).

    Returns
    -------
    VerifyResult with kept items and per-category rejection counts.
    """
    result = VerifyResult()

    # --- messages (topic labels + reason) ---
    for msg in response.get("messages", []):
        mid = msg.get("id", "")
        text = _get_message_text(chunk, mid)
        if text is None:
            result.rejected["bad_message_id"] += 1
            continue
        result.messages.append(msg)

    # --- mentions ---
    for m in response.get("mentions", []):
        mid = m.get("message_id", "")
        surface = m.get("surface", "")
        mtype = m.get("type", "")

        # Check message id exists.
        msg_text = _get_message_text(chunk, mid)
        if msg_text is None:
            result.rejected["bad_message_id"] += 1
            continue

        # Check mention type is in vocabulary.
        if mtype not in VALID_MENTION_TYPES:
            result.rejected["bad_mention_type"] += 1
            continue

        # Check surface appears verbatim (after invisible-strip on both sides).
        stripped_text = _strip_invisible(msg_text)
        stripped_surface = _strip_invisible(surface)
        if not stripped_surface or stripped_surface not in stripped_text:
            result.rejected["surface_not_found"] += 1
            continue

        result.mentions.append(m)

    # --- relations ---
    for rel in response.get("relations", []):
        rtype = rel.get("type", "")
        evidence = rel.get("evidence_span", "")
        mid = rel.get("message_id", "")

        # Check relation type in closed vocab.
        if rtype not in VALID_RELATION_TYPES:
            result.rejected["bad_relation_type"] += 1
            continue

        # Check message id exists.
        msg_text = _get_message_text(chunk, mid)
        if msg_text is None:
            result.rejected["bad_message_id"] += 1
            continue

        # Check evidence_span appears verbatim.
        stripped_text = _strip_invisible(msg_text)
        stripped_evidence = _strip_invisible(evidence)
        if not stripped_evidence or stripped_evidence not in stripped_text:
            result.rejected["evidence_not_found"] += 1
            continue

        result.relations.append(rel)

    return result
