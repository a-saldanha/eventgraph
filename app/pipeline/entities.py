"""Entity extraction + resolution (heuristic stand-in for the LLM stages).

Extraction pulls typed mentions (people from headers; money/documents/subevents
from the text). Resolution canonicalizes them — the hard part — with a visible
merge log. Both are behind plain functions an LLM version can replace.

Corpus-specific gazetteers (DOMAIN_ORG, LOCATIONS, SUBEVENTS) have been
removed. The heuristic path uses only structural signals: identifier merges
(email/phone) for people, domain-derived orgs, keyword-based subevents.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..graph_model import Entity, EntityType, MergeRecord, Mention
from ..schema import SourceItem, normalize_text

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-@]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_MONEY_RE = re.compile(r"\$\s?([0-9][0-9,]*\.[0-9]{2})")

# Generic subevent keywords — no corpus-specific names or places.
_SUBEVENT_KEYWORDS: dict[str, list[str]] = {
    "Acceptance": ["accept", "acceptance", "camera-ready", "camera ready"],
    "Rebuttal": ["rebuttal"],
    "Registration & Payment": ["registration", "register", "invoice", "receipt", "payment"],
    "Visa": ["visa"],
    "Flights": ["flight", "boarding", "itinerary", "airline"],
    "Accommodation": ["airbnb", "accommodation", "hotel", "check-in"],
    "Presentation": ["presentation", "poster", "badge", "session", "workshop"],
}


# ----------------------------------------------------------------------------- extraction
def _people_mentions(items: list[SourceItem], owner_refs: set | None = None):
    """(display_name, email, item_id, surface_text) from typed person participants,
    plus the index set of mentions that belong to the archive owner.

    Only participants the parser classified as people are considered, so
    placeholders, group titles, role mailboxes and org issuers never become people —
    *except* participants whose (item_id, index) is in `owner_refs`. Those are the
    owner's own cross-channel surfaces (a calendar "self" marker, a WhatsApp leet
    handle) that carry no shared identifier; they are folded into the one owner
    entity by a forced union, never surfaced as separate people.
    """
    owner_refs = owner_refs or set()
    out = []
    owner_idx: set[int] = set()
    for it in items:
        for pi, p in enumerate(it.participants):
            is_owner = (it.id, pi) in owner_refs
            if p.kind != "person" and not is_owner:
                continue
            email = p.id_value if p.id_type == "email" else ""
            name = p.display_name or (p.id_value if p.id_type == "phone" else "")
            surface = p.raw or name or email or ""
            if is_owner:
                owner_idx.add(len(out))
            out.append((name or "", email or "", it.id, surface))
    return out, owner_idx


def _email_stem(email: str) -> str:
    local = email.split("@")[0]
    return re.sub(r"[^a-z]", "", local.lower())  # drop digits/dots: rohan0707.menezes -> rohanmenezes


# ----------------------------------------------------------------------------- resolution
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


def resolve_people(
    items: list[SourceItem],
    owner_refs: set | None = None,
    owner_label: str | None = None,
    identifier_only: bool = False,
) -> tuple[list[Entity], list[MergeRecord]]:
    raw, owner_idx = _people_mentions(items, owner_refs)
    return _resolve_person_mentions(raw, owner_idx=owner_idx, owner_label=owner_label,
                                    identifier_only=identifier_only)


def _resolve_person_mentions(
    raw,
    owner_idx: set[int] | None = None,
    owner_label: str | None = None,
    identifier_only: bool = False,
) -> tuple[list[Entity], list[MergeRecord]]:
    """Core entity resolution over (name, email, item_id, surface) tuples.

    `identifier_only=True` (heuristic fallback): merge only on shared email/phone.
    `identifier_only=False` (legacy): also merge on email-stem (default is now True
    for the heuristic path to avoid false positives).

    `owner_idx` names the mentions that structural owner inference tied to the
    archive owner; they are force-unioned into one entity (flagged `owner`) so the
    owner's email, leet handle and calendar-self surfaces collapse even though they
    share no identifier.
    """
    owner_idx = owner_idx or set()
    keys = list(range(len(raw)))
    uf = _UF(keys)

    by_email: dict[str, list[int]] = defaultdict(list)
    by_stem: dict[str, list[int]] = defaultdict(list)
    for i, (name, email, _iid, _surf) in enumerate(raw):
        if email:
            by_email[email].append(i)
            if not identifier_only:
                stem = _email_stem(email)
                if len(stem) >= 6:
                    by_stem[stem].append(i)

    for group in by_email.values():
        for j in group[1:]:
            uf.union(group[0], j)
    if not identifier_only:
        for group in by_stem.values():
            for j in group[1:]:
                uf.union(group[0], j)

    # collapse every owner surface into one identity, across channels
    owner_members = sorted(owner_idx)
    for j in owner_members[1:]:
        uf.union(owner_members[0], j)

    # assemble canonical entities
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in keys:
        clusters[uf.find(i)].append(i)

    entities: list[Entity] = []
    merges: list[MergeRecord] = []
    for ci, (root, members) in enumerate(clusters.items()):
        is_owner = bool(owner_idx.intersection(members))
        names = [raw[i][0] for i in members if raw[i][0]]
        emails = sorted({raw[i][1] for i in members if raw[i][1]})
        surfaces = sorted({raw[i][3] for i in members})
        label = owner_label if (is_owner and owner_label) else _best_label(names, emails)
        eid = f"person:{ci}"
        mentions = [Mention(item_id=raw[i][2], text=raw[i][3]) for i in members]
        attrs = {"emails": emails}
        if is_owner:
            attrs["owner"] = True
        entities.append(Entity(
            id=eid, type=EntityType.PERSON, label=label,
            aliases=surfaces, mentions=mentions, attrs=attrs,
        ))
        if len(surfaces) > 1:
            why = []
            if len(emails) > 1:
                stems = {_email_stem(e) for e in emails}
                if len(stems) < len(emails):
                    why.append(f"{len(emails)} emails share a name stem")
                else:
                    why.append(f"{len(emails)} emails co-occur with the same name")
            if is_owner:
                why.append("owner surfaces across channels collapsed by structural inference")
            merges.append(MergeRecord(
                canonical_id=eid, merged_forms=surfaces[:12],
                rationale="; ".join(why) or "grouped by shared identifiers",
            ))
    return entities, merges


def _best_label(names, emails):
    real = [n for n in names if n and "@" not in n and len(n) > 1]
    if real:
        return max(real, key=lambda n: (len(n.split()), len(n)))
    return emails[0] if emails else "unknown"


def extract_money(items: list[SourceItem]) -> list[Entity]:
    from . import currency as C

    amt_items: dict[float, set[str]] = defaultdict(set)
    for it in items:
        for m in _MONEY_RE.findall(f"{it.subject or ''} {it.body}"):
            val = float(m.replace(",", ""))
            if val > 0:
                amt_items[val].add(it.id)
    by_id = {it.id: it for it in items}
    ents = []
    for k, (amt, iids) in enumerate(sorted(amt_items.items(), key=lambda x: -x[0])):
        attrs = C.resolve_amount_currency(amt, iids, by_id)
        lbl = C.label(amt, attrs)
        ents.append(Entity(id=f"money:{k}", type=EntityType.MONEY, label=lbl, attrs=attrs,
                           mentions=[Mention(item_id=i, text=lbl) for i in sorted(iids)]))
    return ents


def extract_subevents(items: list[SourceItem]) -> list[Entity]:
    ents = []
    for k, (name, kws) in enumerate(_SUBEVENT_KEYWORDS.items()):
        pats = [re.compile(rf"\b{re.escape(w)}\b") for w in kws]
        iids = set()
        for it in items:
            text = normalize_text(f"{it.subject or ''} {it.body}")
            if any(p.search(text) for p in pats):
                iids.add(it.id)
        if iids:
            ents.append(Entity(id=f"subevent:{k}", type=EntityType.SUBEVENT, label=name,
                              mentions=[Mention(item_id=i, text=name) for i in sorted(iids)]))
    return ents
