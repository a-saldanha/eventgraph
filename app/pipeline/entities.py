"""Entity extraction + resolution (heuristic stand-in for the LLM stages).

Extraction pulls typed mentions (people from headers; orgs from email domains +
gazetteer; locations/money/documents/subevents from the text). Resolution
canonicalizes them — the hard part — with a visible merge log. Both are behind
plain functions an LLM version can replace.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..graph_model import Entity, EntityType, MergeRecord, Mention
from ..schema import SourceItem, normalize_text

# ----------------------------------------------------------------------------- gazetteers
DOMAIN_ORG = {
    "ispa.org": "ISPA", "ispasignalsociety.org": "ISPA", "ispaicvsp.wpengine.com": "ISPA",
    "confdesk.io": "ConfDesk", "itaravali.ac.in": "Institute of Technology Aravali",
    "students.itaravali.ac.in": "Institute of Technology Aravali",
    "kuni.eu": "Kuni University", "eitbihar.ac.in": "EIT Bihar",
    "ipalvor.pt": "IPAlvor", "hmcoe.onmicrosoft.com": "HM College O365",
}
LOCATIONS = ["Kaldera", "Norvania", "Mumbai", "India"]
SUBEVENTS = {
    "Acceptance": ["accept", "acceptance", "camera-ready", "camera ready"],
    "Rebuttal": ["rebuttal"],
    "Registration & Payment": ["registration", "register", "invoice", "receipt", "payment"],
    "Visa": ["visa"],
    "Flights": ["flight", "boarding", "itinerary", "airline"],
    "Accommodation": ["airbnb", "accommodation", "hotel", "check-in"],
    "Presentation": ["presentation", "poster", "badge", "session", "workshop"],
}

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-@]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_NAME_EMAIL_RE = re.compile(r"\s*\"?([^<>\"]+?)\"?\s*<([^>]+)>")
_MONEY_RE = re.compile(r"\$\s?([0-9][0-9,]*\.[0-9]{2})")


# ----------------------------------------------------------------------------- extraction
def _people_mentions(items: list[SourceItem]):
    """(display_name, email, item_id, surface_text) from every header field."""
    out = []
    for it in items:
        for field in (it.sender, it.recipients):
            if not field:
                continue
            for chunk in field.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                m = _NAME_EMAIL_RE.match(chunk)
                if m:
                    name, email = m.group(1).strip(), m.group(2).strip().lower()
                elif _EMAIL_RE.fullmatch(chunk):
                    name, email = "", chunk.lower()
                else:
                    name, email = chunk, ""
                out.append((name, email, it.id, chunk))
    return out


def _leet_norm(name: str) -> str:
    s = name.lower().translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "@": "a", "$": "s"}))
    s = re.sub(r"[^a-z ]", "", s)
    toks = sorted(t for t in s.split() if t)
    return " ".join(toks)


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


def resolve_people(items: list[SourceItem]) -> tuple[list[Entity], list[MergeRecord]]:
    return resolve_person_mentions(_people_mentions(items))


def resolve_person_mentions(raw) -> tuple[list[Entity], list[MergeRecord]]:
    """Core entity resolution over (name, email, item_id, surface) tuples — shared
    by the header-based (heuristic) and LLM extraction paths."""
    # index each mention by a stable key
    keys = list(range(len(raw)))
    uf = _UF(keys)

    by_email: dict[str, list[int]] = defaultdict(list)
    by_stem: dict[str, list[int]] = defaultdict(list)
    by_name: dict[str, list[int]] = defaultdict(list)
    for i, (name, email, _iid, _surf) in enumerate(raw):
        if email:
            by_email[email].append(i)
            stem = _email_stem(email)
            if len(stem) >= 6:  # avoid merging on tiny stems
                by_stem[stem].append(i)
        nn = _leet_norm(name)
        if len(nn) >= 3:
            by_name[nn].append(i)

    reasons: dict[tuple, str] = {}
    for group in by_email.values():
        for j in group[1:]:
            uf.union(group[0], j)
    for stem, group in by_stem.items():
        for j in group[1:]:
            uf.union(group[0], j)
            reasons[(uf.find(group[0]),)] = f"shared email stem '{stem}'"
    for nn, group in by_name.items():
        for j in group[1:]:
            uf.union(group[0], j)
            reasons[(uf.find(group[0]),)] = f"name match after normalization ('{nn}')"

    # assemble canonical entities
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in keys:
        clusters[uf.find(i)].append(i)

    entities: list[Entity] = []
    merges: list[MergeRecord] = []
    for ci, (root, members) in enumerate(clusters.items()):
        names = [raw[i][0] for i in members if raw[i][0]]
        emails = sorted({raw[i][1] for i in members if raw[i][1]})
        surfaces = sorted({raw[i][3] for i in members})
        label = _best_label(names, emails)
        eid = f"person:{ci}"
        mentions = [Mention(item_id=raw[i][2], text=raw[i][3]) for i in members]
        entities.append(Entity(
            id=eid, type=EntityType.PERSON, label=label,
            aliases=surfaces, mentions=mentions, attrs={"emails": emails},
        ))
        if len(surfaces) > 1:
            why = []
            if len(emails) > 1:
                stems = {_email_stem(e) for e in emails}
                if len(stems) < len(emails):
                    why.append(f"{len(emails)} emails share a name stem")
                else:
                    why.append(f"{len(emails)} emails co-occur with the same name")
            norm_names = {_leet_norm(n) for n in names if n}
            if len(names) > 1 and len(norm_names) == 1 and any(n != names[0] for n in names):
                why.append("name variants match after leet/normalization")
            merges.append(MergeRecord(
                canonical_id=eid, merged_forms=surfaces[:12],
                rationale="; ".join(why) or "grouped by shared identifiers",
            ))
    return entities, merges


def _best_label(names, emails):
    real = [n for n in names if n and "@" not in n and len(n) > 1]
    if real:
        # prefer the longest, most complete-looking name
        return max(real, key=lambda n: (len(n.split()), len(n)))
    return emails[0] if emails else "unknown"


def extract_orgs(items: list[SourceItem], people: list[Entity]) -> list[Entity]:
    org_items: dict[str, set[str]] = defaultdict(set)
    for it in items:
        for email in _EMAIL_RE.findall(f"{it.sender} {it.recipients} {it.body}"):
            domain = email.split("@")[-1].lower()
            for dom, org in DOMAIN_ORG.items():
                if domain.endswith(dom):
                    org_items[org].add(it.id)
    ents = []
    for k, (org, iids) in enumerate(sorted(org_items.items())):
        ents.append(Entity(id=f"org:{k}", type=EntityType.ORG, label=org,
                           mentions=[Mention(item_id=i, text=org) for i in sorted(iids)]))
    return ents


def extract_gazetteer(items: list[SourceItem], terms: list[str], etype: EntityType, prefix: str) -> list[Entity]:
    found: dict[str, set[str]] = defaultdict(set)
    pats = {t: re.compile(rf"\b{re.escape(t.lower())}\b") for t in terms}
    for it in items:
        text = normalize_text(f"{it.subject or ''} {it.body}")
        for t, pat in pats.items():
            if pat.search(text):
                found[t].add(it.id)
    ents = []
    for k, (t, iids) in enumerate(sorted(found.items())):
        ents.append(Entity(id=f"{prefix}:{k}", type=etype, label=t,
                           mentions=[Mention(item_id=i, text=t) for i in sorted(iids)]))
    return ents


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
    for k, (name, kws) in enumerate(SUBEVENTS.items()):
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
