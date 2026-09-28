"""Exact + near-duplicate detection over SourceItems (deterministic).

Exact: identical normalized body/sender/subject (content_hash).
Near:  high token-shingle overlap within a source_type block — catches forwarded
       emails that quote the original, resends, and reposted text.

This is the first tooth of the hard problem (duplication). It runs before any LLM
so the graph is built over deduped-but-provenance-preserving units: we keep every
raw item, we just record which items are duplicates of which.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schema import SourceItem, normalize_text

_WORD_RE = re.compile(r"\w+")


def _shingles(text: str, k: int = 3) -> set[str]:
    toks = _WORD_RE.findall(normalize_text(text))
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


class _UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


@dataclass
class DedupResult:
    exact_groups: list[list[str]] = field(default_factory=list)   # item ids, size>1
    near_clusters: list[list[str]] = field(default_factory=list)  # item ids, size>1
    # id -> canonical id (first-seen representative of its dup group)
    canonical: dict[str, str] = field(default_factory=dict)

    @property
    def n_exact_dupes(self) -> int:
        return sum(len(g) - 1 for g in self.exact_groups)

    @property
    def n_near_dupes(self) -> int:
        return sum(len(c) - 1 for c in self.near_clusters)


def find_duplicates(
    items: list[SourceItem],
    near_threshold: float = 0.6,
    min_tokens: int = 8,
) -> DedupResult:
    """Group exact and near duplicates. `min_tokens` skips trivially short
    messages ("ok", "👍") from near-dup clustering — they aren't meaningful dupes."""
    result = DedupResult()

    # ---- exact (hash) ----
    by_hash: dict[str, list[str]] = {}
    for it in items:
        by_hash.setdefault(it.content_hash, []).append(it.id)
    for ids in by_hash.values():
        if len(ids) > 1:
            result.exact_groups.append(ids)
            for i in ids[1:]:
                result.canonical[i] = ids[0]

    # ---- near (shingle Jaccard within source_type blocks) ----
    idx = list(range(len(items)))
    shingle_cache: dict[int, set[str]] = {}
    blocks: dict[str, list[int]] = {}
    for i in idx:
        toks = _WORD_RE.findall(normalize_text(items[i].body))
        if len(toks) < min_tokens:
            continue
        shingle_cache[i] = _shingles(items[i].body)
        blocks.setdefault(items[i].source_type.value, []).append(i)

    uf = _UnionFind(len(items))
    linked = False
    for block in blocks.values():
        for a in range(len(block)):
            ia = block[a]
            sa = shingle_cache[ia]
            for b in range(a + 1, len(block)):
                ib = block[b]
                if _jaccard(sa, shingle_cache[ib]) >= near_threshold:
                    uf.union(ia, ib)
                    linked = True

    if linked:
        groups: dict[int, list[str]] = {}
        for i in shingle_cache:
            groups.setdefault(uf.find(i), []).append(items[i].id)
        for members in groups.values():
            if len(members) > 1:
                result.near_clusters.append(members)
                for m in members[1:]:
                    result.canonical.setdefault(m, members[0])

    return result
