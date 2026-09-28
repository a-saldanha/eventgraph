"""Infer the archive owner from structure, not from names.

The owner is the person the archive belongs to. We find them by how the corpus is
shaped — how many conversations and channels an identity touches, how often it is a
recipient (inbox exports are addressed *to* the owner), and whether it authors
self-marked rows (a calendar/notes "self" marker). Name similarity is never used, so
a leetspeak handle, a calendar "self" marker and an email address can all resolve to
one owner even though they share no identifier.

`OWNER_IDENTIFIERS` (comma-separated emails/phones) is an optional cross-check that is
logged against the inference; it does not override it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..schema import SourceItem, SourceType
from .identity import IdentityIndex, Ref

# how much stronger the top identity must be than the runner-up to be confident
_MARGIN = 1.15


@dataclass
class OwnerResult:
    refs: list[Ref] = field(default_factory=list)     # every participant ref that is the owner
    primary_key: str | None = None                    # winning identity cluster key
    label: str | None = None                          # best real name for the owner
    confident: bool = False
    seed_agrees: bool | None = None                   # vs OWNER_IDENTIFIERS, if set
    scores: dict[str, float] = field(default_factory=dict)


def _seed_identifiers() -> set[str]:
    raw = os.getenv("OWNER_IDENTIFIERS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def infer_owner(items: list[SourceItem], index: IdentityIndex) -> OwnerResult:
    by_id = {it.id: it for it in items}

    # Score each identity cluster on structural reach.
    scores: dict[str, float] = {}
    for c in index.clusters:
        convos, channels, recipient_hits = set(), set(), 0
        for item_id, p_index in c.members:
            it = by_id.get(item_id)
            if not it:
                continue
            convos.add(it.conversation_id)
            channels.add(it.source_type.value)
            if it.participants[p_index].role in ("recipient", "cc"):
                recipient_hits += 1
        scores[c.key] = len(convos) + 2 * len(channels) + 0.25 * recipient_hits

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    result = OwnerResult(scores=scores)
    if not ranked:
        return result

    top_key, top_score = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    result.primary_key = top_key
    result.confident = top_score >= max(1.0, runner * _MARGIN)

    owner_cluster = index.cluster(top_key)
    refs: list[Ref] = list(owner_cluster.members) if owner_cluster else []

    # Fold in the cross-channel owner surfaces that carry no shared identifier:
    #   - every "self" participant (author of self-authored rows)
    #   - the WhatsApp sender present in the most distinct chats (the export owner)
    refs += _self_refs(items)
    refs += _whatsapp_owner_refs(items, by_id)

    # de-dupe refs, keep order
    seen: set[Ref] = set()
    result.refs = [r for r in refs if not (r in seen or seen.add(r))]

    result.label = _best_owner_label(owner_cluster.display_names if owner_cluster else set())

    seed = _seed_identifiers()
    if seed:
        result.seed_agrees = bool(seed & {i.lower() for i in (owner_cluster.identifiers if owner_cluster else set())})
    return result


def _self_refs(items: list[SourceItem]) -> list[Ref]:
    out = []
    for it in items:
        for pi, p in enumerate(it.participants):
            if p.kind == "self":
                out.append((it.id, pi))
    return out


def _whatsapp_owner_refs(items: list[SourceItem], by_id) -> list[Ref]:
    """The WhatsApp sender that appears in the most distinct chats is the export owner."""
    chats_by_name: dict[str, set[str]] = {}
    refs_by_name: dict[str, list[Ref]] = {}
    for it in items:
        if it.source_type != SourceType.WHATSAPP:
            continue
        for pi, p in enumerate(it.participants):
            if p.role != "sender" or p.kind not in ("person", "unknown"):
                continue
            name = (p.display_name or p.raw or "").strip()
            if not name:
                continue
            chats_by_name.setdefault(name, set()).add(it.conversation_id)
            refs_by_name.setdefault(name, []).append((it.id, pi))
    if not chats_by_name:
        return []
    owner_name = max(chats_by_name, key=lambda n: len(chats_by_name[n]))
    # only treat as owner if present across several chats (an export-wide presence)
    if len(chats_by_name[owner_name]) < 2:
        return []
    return refs_by_name[owner_name]


def _best_owner_label(names: set[str]) -> str | None:
    real = [n for n in names if n and "@" not in n]
    if real:
        return max(real, key=lambda n: (len(n.split()), len(n)))
    return next(iter(names), None)
