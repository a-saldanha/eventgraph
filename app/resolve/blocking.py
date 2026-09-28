"""Blocking: group profiles that *might* be the same entity into candidate blocks.

Profiles in the same block are sent together to the LLM for resolution.
Two profiles are co-blocked when they:
  - have the same type AND share at least one of:
    - an identifier (email/phone)
    - an email-stem (local-part stripped of digits/punctuation, len >= 6)
    - a normalized name token (unigram, len >= 4, both profiles)
    - first name + shared conversation/channel (weak signal for people)
    - org domain
    - location containment (one surface is a prefix/suffix of the other)
    - shared item_id co-occurrence (appear in the same source message)

Blocks are capped at MAX_BLOCK_SIZE profiles to keep LLM prompts small.
Profiles that share no blocking key with any other profile of the same type
remain as singleton blocks and are resolved trivially (no LLM call needed).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from .profiles import Profile

log = logging.getLogger(__name__)

MAX_BLOCK_SIZE = 12
_MIN_STEM_LEN = 6
_MIN_TOKEN_LEN = 4


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm_name(surface: str) -> str:
    """Lowercase, strip non-alpha, collapse spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", surface.lower())).strip()


def _email_stem(email: str) -> str:
    """Local-part of an email with digits/punctuation stripped."""
    local = email.split("@")[0]
    return re.sub(r"[^a-z]", "", local.lower())


def _name_tokens(surface: str) -> set[str]:
    """Unigrams of a normalized surface that are long enough to be signal."""
    return {t for t in _norm_name(surface).split() if len(t) >= _MIN_TOKEN_LEN}


def _first_name(surface: str) -> str | None:
    parts = _norm_name(surface).split()
    return parts[0] if parts else None


def _org_domain(identifier: str) -> str | None:
    """If the identifier looks like an email, return its domain."""
    if "@" in identifier:
        return identifier.split("@")[-1].lower()
    return None


def _location_containment(a: str, b: str) -> bool:
    """True when one location surface is contained in the other (city/country pairs)."""
    na, nb = _norm_name(a), _norm_name(b)
    return na and nb and (na in nb or nb in na)


# ---------------------------------------------------------------------------
# Union-Find for building blocks
# ---------------------------------------------------------------------------

class _UF:
    def __init__(self, keys):
        self.p = {k: k for k in keys}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_blocks(profiles: list[Profile]) -> list[list[Profile]]:
    """Return a list of candidate blocks (each block is a list of Profiles).

    Singleton blocks (one profile, no partner found) are included so downstream
    code can treat all profiles uniformly.  The LLM resolver can skip singletons.
    """
    if not profiles:
        return []

    # Group by type first — different types never merge.
    by_type: dict[str, list[Profile]] = defaultdict(list)
    for p in profiles:
        by_type[p.type].append(p)

    all_blocks: list[list[Profile]] = []
    for ptype, group in by_type.items():
        blocks = _block_group(ptype, group)
        all_blocks.extend(blocks)

    log.info(
        "Blocking: %d profiles → %d blocks (types: %s)",
        len(profiles),
        len(all_blocks),
        ", ".join(f"{t}={len(v)}" for t, v in by_type.items()),
    )
    return all_blocks


def _block_group(ptype: str, profiles: list[Profile]) -> list[list[Profile]]:
    """Build blocks for one entity type."""
    ids = [p.id for p in profiles]
    uf = _UF(ids)
    by_id = {p.id: p for p in profiles}

    # Index building
    by_identifier: dict[str, list[str]] = defaultdict(list)   # exact ident -> pids
    by_stem: dict[str, list[str]] = defaultdict(list)          # email stem -> pids
    by_token: dict[str, list[str]] = defaultdict(list)         # name token -> pids
    by_first: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    # first_name -> channel -> pids  (people only)
    by_domain: dict[str, list[str]] = defaultdict(list)        # org domain -> pids

    for p in profiles:
        for ident in p.identifiers:
            by_identifier[ident].append(p.id)
            stem = _email_stem(ident)
            if len(stem) >= _MIN_STEM_LEN:
                by_stem[stem].append(p.id)
            dom = _org_domain(ident)
            if dom:
                by_domain[dom].append(p.id)

        for surf in p.surfaces:
            for tok in _name_tokens(surf):
                by_token[tok].append(p.id)
            if ptype == "person":
                fn = _first_name(surf)
                if fn:
                    for ch in (p.channels or {"_"}):
                        by_first[fn][ch].append(p.id)

    # Union exact identifier matches
    for pid_list in by_identifier.values():
        _union_all(uf, pid_list)

    # Union same email-stem
    for pid_list in by_stem.values():
        _union_all(uf, pid_list)

    # Union shared name token
    for pid_list in by_token.values():
        if len(pid_list) <= MAX_BLOCK_SIZE:
            _union_all(uf, pid_list)

    # Union first-name + shared channel (people only)
    if ptype == "person":
        for first, ch_map in by_first.items():
            for ch, pid_list in ch_map.items():
                if len(pid_list) <= MAX_BLOCK_SIZE:
                    _union_all(uf, pid_list)

    # Union same org domain (orgs only)
    if ptype == "org":
        for pid_list in by_domain.values():
            _union_all(uf, pid_list)

    # Location containment (pairwise — O(n^2), small n expected)
    if ptype == "location":
        pid_list = ids
        for i, a in enumerate(pid_list):
            pa = by_id[a]
            for b in pid_list[i + 1:]:
                pb = by_id[b]
                for sa in pa.surfaces:
                    for sb in pb.surfaces:
                        if _location_containment(sa, sb):
                            uf.union(a, b)

    # Shared item_id co-occurrence: profiles that appear in the same source
    # message are candidates (the LLM will decide whether to merge them).
    by_item: dict[str, list[str]] = defaultdict(list)
    for p in profiles:
        for iid in p.item_ids:
            by_item[iid].append(p.id)
    for iid_pid_list in by_item.values():
        if len(iid_pid_list) <= MAX_BLOCK_SIZE:
            _union_all(uf, iid_pid_list)

    # Collect root -> members
    roots: dict[str, list[str]] = defaultdict(list)
    for pid in ids:
        roots[uf.find(pid)].append(pid)

    # Cap oversized blocks by splitting greedily on identifier connectivity.
    blocks: list[list[Profile]] = []
    for root, members in roots.items():
        if len(members) <= MAX_BLOCK_SIZE:
            blocks.append([by_id[m] for m in members])
        else:
            # split: keep identifier-connected sub-groups, rest go solo
            sub = _split_large_block([by_id[m] for m in members])
            blocks.extend(sub)

    return blocks


def _union_all(uf: _UF, pids: list[str]) -> None:
    for j in pids[1:]:
        uf.union(pids[0], j)


def _split_large_block(profiles: list[Profile]) -> list[list[Profile]]:
    """Split a block that exceeds MAX_BLOCK_SIZE.

    Strategy: greedily keep only exact-identifier-connected sub-groups (which
    are the most reliable) up to MAX_BLOCK_SIZE; remaining profiles form
    singleton or pair blocks.
    """
    uf = _UF([p.id for p in profiles])
    by_id = {p.id: p for p in profiles}
    by_ident: dict[str, list[str]] = defaultdict(list)
    for p in profiles:
        for ident in p.identifiers:
            by_ident[ident].append(p.id)
    for pid_list in by_ident.values():
        _union_all(uf, pid_list)

    roots: dict[str, list[str]] = defaultdict(list)
    for p in profiles:
        roots[uf.find(p.id)].append(p.id)

    result: list[list[Profile]] = []
    for root, members in roots.items():
        chunk = members[:MAX_BLOCK_SIZE]
        result.append([by_id[m] for m in chunk])
        # Leftover go solo
        for m in members[MAX_BLOCK_SIZE:]:
            result.append([by_id[m]])

    return result
