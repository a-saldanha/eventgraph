"""Relevance scoping (heuristic stand-in for the LLM stage).

Decides which items belong to THIS event vs. noise (other work, illness, unrelated
group chatter). Deterministic keyword+conversation scoring for the mockup; the
interface (score_relevance) is what an LLM version would replace.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..graph_model import RelevanceVerdict
from ..schema import SourceItem, normalize_text

# Event vocabulary — tuned to this corpus (grep-derived).
EVENT_TERMS = [
    "icvsp", "ispa", "conference", "rebuttal", "registration", "register",
    "camera-ready", "camera ready", "visa", "flight", "airbnb", "accommodation",
    "kaldera", "norvania", "badge", "presentation", "poster", "paper", "submission",
    "5163", "prism-f", "confdesk", "invoice", "receipt", "boarding", "itinerary",
    "hotel", "check-in", "workshop", "proceedings", "acceptance",
]
# Terms that signal off-event content (helps flag noise explicitly).
NOISE_TERMS = ["fever", "sick", "illness", "assignment deadline", "lab meeting",
               "internship", "placement", "birthday", "cricket", "movie"]

_TERM_RES = [re.compile(rf"\b{re.escape(t)}\b") for t in EVENT_TERMS]
_NOISE_RES = [re.compile(rf"\b{re.escape(t)}\b") for t in NOISE_TERMS]


def _item_score(item: SourceItem) -> tuple[float, list[str], list[str]]:
    text = normalize_text(f"{item.subject or ''} {item.body} {item.channel}")
    hits = [t.pattern.strip("\\b") for t in _TERM_RES if t.search(text)]
    noise = [t.pattern.strip("\\b") for t in _NOISE_RES if t.search(text)]
    score = len(set(hits))
    return score, hits, noise


def score_relevance(items: list[SourceItem], threshold: float = 1.0) -> list[RelevanceVerdict]:
    """Two-pass: per-item keyword score, then lift items in event-dominated
    conversations (a bare 'ok, see you there' inherits its thread's relevance)."""
    raw: dict[str, tuple[float, list[str], list[str]]] = {}
    conv_hits: dict[str, float] = defaultdict(float)
    conv_total: dict[str, int] = defaultdict(int)
    for it in items:
        s, hits, noise = _item_score(it)
        raw[it.id] = (s, hits, noise)
        conv_hits[it.conversation_id] += 1 if s > 0 else 0
        conv_total[it.conversation_id] += 1

    verdicts: list[RelevanceVerdict] = []
    for it in items:
        s, hits, noise = raw[it.id]
        conv_ratio = conv_hits[it.conversation_id] / max(1, conv_total[it.conversation_id])
        relevant = s >= threshold or conv_ratio >= 0.5
        if s >= threshold:
            rationale = f"matched event terms: {', '.join(hits[:5])}"
        elif relevant:
            rationale = f"inherited from event-dominated conversation ({conv_ratio:.0%} on-topic)"
        elif noise:
            rationale = f"off-event signal ({', '.join(noise[:3])}) and no event terms"
        else:
            rationale = "no event terms and conversation is mostly off-topic"
        verdicts.append(RelevanceVerdict(item_id=it.id, relevant=relevant,
                                         score=float(s), rationale=rationale))
    return verdicts
