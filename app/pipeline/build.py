"""Orchestrate the pipeline into an EventGraph, and derive relations + timeline.

parse (done upstream) → dedup → relevance → extract entities → resolve people →
relations → timeline. Heuristic today, interfaces ready for LLM swap-in.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..graph_model import Edge, Entity, EntityType, EventGraph, Mention, RelevanceVerdict
from ..ingest.dedup import DedupResult, find_duplicates
from ..ingest.participants import mark_shared_mailboxes
from ..schema import SourceItem
from . import entities as E
from .relevance import score_relevance

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_MONEY_RE = re.compile(r"([0-9][0-9,]*\.[0-9]{2})")


@dataclass
class Bundle:
    items: list[SourceItem]
    dedup: DedupResult
    graph: EventGraph
    timeline: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def build_graph(items: list[SourceItem], mode: str = "heuristic", progress=None) -> Bundle:
    mark_shared_mailboxes(items)  # downgrade addresses used by 3+ names to system
    dedup = find_duplicates(items)

    if mode == "llm":
        relevance, llm_ents = _llm_stage(items, progress)
    else:
        relevance, llm_ents = score_relevance(items), None

    relevant_ids = {v.item_id for v in relevance if v.relevant}
    rel_items = [it for it in items if it.id in relevant_ids] or items

    # People + their correspondence come from message HEADERS in both modes — this
    # is deterministic and data-independent, so the social graph always connects.
    # The LLM (when on) contributes the harder, content-derived entity types.
    people, merges = E.resolve_people(rel_items)
    if llm_ents is not None:
        orgs = _group_llm(llm_ents["org"], EntityType.ORG, "org")
        locations = _group_llm(llm_ents["location"], EntityType.LOCATION, "loc")
        money = _money_from_llm(llm_ents["money"], rel_items)
    else:
        orgs = E.extract_orgs(rel_items, people)
        locations = E.extract_gazetteer(rel_items, E.LOCATIONS, EntityType.LOCATION, "loc")
        money = E.extract_money(rel_items)
    # Institution/company orgs derived generically from email domains (any dataset).
    domain_orgs = _domain_orgs(people, len(orgs))
    orgs = orgs + domain_orgs
    # sub-events + documents stay heuristic in both modes (timeline needs them)
    subevents = E.extract_subevents(rel_items)
    documents = _extract_documents(rel_items)

    all_entities = people + orgs + locations + money + subevents + documents
    edges = _relations(rel_items, people, orgs, locations, money, subevents, documents)

    graph = EventGraph(entities=all_entities, edges=edges, merges=merges, relevance=relevance)
    timeline = _timeline(subevents, items)
    stats = {
        "items": len(items), "relevant_items": len(relevant_ids),
        "exact_dupes": dedup.n_exact_dupes, "near_dupes": dedup.n_near_dupes,
        "entities": len(all_entities), "edges": len(edges),
        "people": len(people), "merges": len(merges), "mode": mode,
    }
    return Bundle(items=items, dedup=dedup, graph=graph, timeline=timeline, stats=stats)


# --------------------------------------------------------------------------- LLM stage
def _llm_stage(items, progress):
    """Run LLM extraction, return (relevance verdicts, grouped LLM entity mentions)."""
    from ..llm.extract import extract_items

    def _p(done, total):
        if progress:
            progress("llm", f"LLM extraction {done}/{total} batches")

    extractions = extract_items(items, progress=_p)
    itemmap = {it.id: it for it in items}
    relevance = [RelevanceVerdict(item_id=x.item_id, relevant=x.relevant,
                                  score=1.0 if x.relevant else 0.0,
                                  rationale=x.rationale or "LLM verdict") for x in extractions]
    people, org, location, money = [], [], [], []
    for x in extractions:
        if not x.relevant:
            continue
        for e in x.entities:
            t, name, surface = e.get("type"), e.get("name", ""), e.get("surface", e.get("name", ""))
            if t == "person":
                email = surface if "@" in surface else (name if "@" in name else "")
                people.append((name, email.lower(), x.item_id, surface))
            elif t == "org":
                org.append((name, x.item_id, surface))
            elif t == "location":
                location.append((name, x.item_id, surface))
            elif t == "money":
                money.append((name, x.item_id, surface))
    return relevance, {"people": people, "org": org, "location": location, "money": money}


def _group_llm(rows, etype: EntityType, prefix: str) -> list[Entity]:
    """Group (name, item_id, surface) mentions into canonical entities by name."""
    from collections import defaultdict

    groups: dict[str, list[tuple]] = defaultdict(list)
    for name, iid, surface in rows:
        key = re.sub(r"\s+", " ", name.strip().lower())
        if key:
            groups[key].append((name, iid, surface))
    ents = []
    for k, (key, members) in enumerate(sorted(groups.items())):
        label = max((m[0] for m in members), key=len)
        ents.append(Entity(id=f"{prefix}:{k}", type=etype, label=label,
                           aliases=sorted({m[2] for m in members}),
                           mentions=[Mention(item_id=m[1], text=m[2]) for m in members]))
    return ents


_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "proton.me", "protonmail.com", "aol.com",
}


def _domain_orgs(people, start_index=0) -> list[Entity]:
    """Derive institution/company orgs from people's email domains — generic, works
    on any dataset (replaces the corpus-specific gazetteer for affiliation)."""
    from collections import defaultdict

    by_domain: dict[str, set] = defaultdict(set)
    dom_items: dict[str, set] = defaultdict(set)
    for p in people:
        for email in p.attrs.get("emails", []):
            dom = email.split("@")[-1].lower().strip(">")
            if not dom or dom in _PERSONAL_DOMAINS:
                continue
            by_domain[dom].add(p.id)
            dom_items[dom] |= set(list(p.item_ids)[:8])
    ents = []
    for k, (dom, pids) in enumerate(sorted(by_domain.items())):
        core = dom.split(".")[0]
        label = core.upper() if len(core) <= 4 else core.capitalize()
        ents.append(Entity(
            id=f"dorg:{k}", type=EntityType.ORG, label=label,
            attrs={"domain": dom, "member_person_ids": sorted(pids)},
            mentions=[Mention(item_id=i, text=dom) for i in sorted(dom_items[dom])],
        ))
    return ents


def _money_from_llm(rows, items) -> list[Entity]:
    from collections import defaultdict
    from . import currency as C

    amt_items: dict[float, set] = defaultdict(set)
    amt_surfaces: dict[float, list] = defaultdict(list)
    for name, iid, surface in rows:
        m = _MONEY_RE.search(f"{name} {surface}")
        if m:
            amt = float(m.group(1).replace(",", ""))
            amt_items[amt].add(iid)
            amt_surfaces[amt].append(surface)
    by_id = {it.id: it for it in items}
    ents = []
    for k, (amt, iids) in enumerate(sorted(amt_items.items(), key=lambda x: -x[0])):
        attrs = C.resolve_amount_currency(amt, iids, by_id, amt_surfaces[amt])
        lbl = C.label(amt, attrs)
        ents.append(Entity(id=f"money:{k}", type=EntityType.MONEY, label=lbl, attrs=attrs,
                           mentions=[Mention(item_id=i, text=lbl) for i in sorted(iids)]))
    return ents


def _extract_documents(items: list[SourceItem]) -> list[Entity]:
    docs = []
    for k, it in enumerate(i for i in items if i.source_type.value == "pdf"):
        label = (it.subject or "document").replace(".pdf", "")
        # nicer label from body cue
        low = it.body.lower()
        for cue, name in (("receipt", "Registration Receipt"), ("invoice", "Invoice"),
                          ("certificate", "Certificate of Attendance"), ("boarding", "Boarding Pass"),
                          ("visa", "Visa Letter"), ("itinerary", "Flight Itinerary")):
            if cue in low:
                label = name
                break
        docs.append(Entity(id=f"doc:{k}", type=EntityType.DOCUMENT, label=label,
                          mentions=[E.Mention(item_id=it.id, text=label)]))
    return docs


def _relations(items, people, orgs, locations, money, subevents, documents) -> list[Edge]:
    edges: list[Edge] = []
    eid = [0]

    def add(src, tgt, kind, item_ids, weight=1.0, attrs=None):
        edges.append(Edge(id=f"e{eid[0]}", source=src, target=tgt, kind=kind,
                          weight=weight, evidence_item_ids=sorted(set(item_ids))[:20],
                          attrs=attrs or {}))
        eid[0] += 1

    # person lookup by surface AND by email (email match is robust across
    # heuristic + LLM modes, where surface strings differ).
    surf2person: dict[str, str] = {}
    email2person: dict[str, str] = {}
    for p in people:
        for a in p.aliases:
            surf2person.setdefault(a, p.id)
            for em in _EMAIL_RE.findall(a):
                email2person.setdefault(em.lower(), p.id)
        for em in p.attrs.get("emails", []):
            email2person.setdefault(em.lower(), p.id)

    def resolve_participant(p) -> Optional[str]:
        if p.kind != "person":
            return None
        if p.id_type == "email" and p.id_value and p.id_value in email2person:
            return email2person[p.id_value]
        return surf2person.get(p.raw)

    # corresponded_with (person <-> person), aggregated
    pair_items: dict[tuple, set] = defaultdict(set)
    for it in items:
        senders = [r for r in (resolve_participant(p) for p in it.senders) if r]
        recipients = [r for r in (resolve_participant(p) for p in it.addressees) if r]
        for s in senders:
            for t in recipients:
                if t != s:
                    pair_items[tuple(sorted((s, t)))].add(it.id)
    for (a, b), iids in pair_items.items():
        add(a, b, "corresponded_with", iids, weight=float(len(iids)))

    # affiliated_with (person -> domain-derived org). Generic — any email domain.
    for o in orgs:
        for pid in o.attrs.get("member_person_ids", []):
            add(pid, o.id, "affiliated_with", list(o.item_ids)[:5])

    # money -> org (paid_to): link each amount to the org it most co-occurs with
    for mnt in money:
        best, best_ov = None, 0
        for o in orgs:
            ov = len(mnt.item_ids & o.item_ids)
            if ov > best_ov:
                best, best_ov = o, ov
        if best:
            add(mnt.id, best.id, "paid_to", list(mnt.item_ids & best.item_ids))

    # org/subevent -> location (held_at) by co-occurrence
    for loc in locations:
        for o in orgs:
            ov = o.item_ids & loc.item_ids
            if len(ov) >= 2:
                add(o.id, loc.id, "held_at", list(ov), weight=float(len(ov)))

    # document -> subevent (document_for) by shared items
    for d in documents:
        best, best_ov = None, 0
        for se in subevents:
            ov = len(d.item_ids & se.item_ids)
            if ov > best_ov:
                best, best_ov = se, ov
        if best:
            add(d.id, best.id, "document_for", list(d.item_ids & best.item_ids))

    return edges


def _as_utc(dt):
    """Comparable key: treat naive (WhatsApp) as UTC, convert aware (email) to UTC."""
    from datetime import timezone

    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _timeline(subevents: list[Entity], items: list[SourceItem]) -> list[dict]:
    itemmap = {it.id: it for it in items}
    rows = []
    for se in subevents:
        ts = [_as_utc(itemmap[i].timestamp) for i in se.item_ids if itemmap.get(i) and itemmap[i].timestamp]
        if not ts:
            continue
        ts_sorted = sorted(ts)
        rows.append({
            "label": se.label, "entity_id": se.id,
            "start": ts_sorted[0].isoformat(), "end": ts_sorted[-1].isoformat(),
            "count": len(se.item_ids),
        })
    return sorted(rows, key=lambda r: r["start"])
