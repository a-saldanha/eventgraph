"""Orchestrate the pipeline into an EventGraph, and derive relations + timeline.

parse (done upstream) → dedup → extract entities → resolve people →
relations → timeline. Heuristic today, interfaces ready for LLM swap-in.
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..graph_model import Edge, Entity, EntityType, EventGraph, Mention, MergeRecord, RelevanceVerdict
from ..ingest.dedup import DedupResult, find_duplicates
from ..ingest.participants import mark_shared_mailboxes
from ..schema import SourceItem
from . import entities as E

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_MONEY_RE = re.compile(r"([0-9][0-9,]*\.[0-9]{2})")

_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "proton.me", "protonmail.com", "aol.com",
}


@dataclass
class Bundle:
    items: list[SourceItem]
    dedup: DedupResult
    graph: EventGraph
    timeline: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    review_queue: list[dict] = field(default_factory=list)
    item_topics: dict = field(default_factory=dict)


def _all_relevant(items):
    """Mark all items as relevant (relevance is now a query-time concern)."""
    return [RelevanceVerdict(item_id=it.id, relevant=True, score=1.0,
                              rationale="all items included") for it in items]


def _consolidate_topics(all_topics: list[str]) -> dict[str, str]:
    """Map each raw topic label to its canonical (normalized, deduplicated) form."""
    norm: dict[str, str] = {}
    canonical_groups: list[tuple[str, set]] = []
    for t in sorted(set(all_topics)):
        tn = re.sub(r'[^a-z0-9 ]', '', t.lower().strip())
        for i, (canon, variants) in enumerate(canonical_groups):
            canon_n = re.sub(r'[^a-z0-9 ]', '', canon.lower().strip())
            ratio = difflib.SequenceMatcher(None, tn, canon_n).ratio()
            if ratio >= 0.85:
                variants.add(t)
                norm[t] = canon
                break
        else:
            canonical_groups.append((t, {t}))
            norm[t] = t
    return norm


def build_graph(items: list[SourceItem], mode: str = "heuristic", progress=None) -> Bundle:
    mark_shared_mailboxes(items)  # downgrade addresses used by 3+ names to system
    dedup = find_duplicates(items)
    _mark_duplicates(items, dedup)
    canon_items = [it for it in items if not it.duplicate_of]

    item_topics: dict = {}

    if mode == "llm":
        relevance, llm_extractions, item_topics = _llm_stage(canon_items, progress)
    else:
        relevance, llm_extractions = _all_relevant(canon_items), None

    # All canonical items go through extraction — relevance is query-time only
    rel_items = canon_items
    relevant_ids = {v.item_id for v in relevance if v.relevant}  # for stats

    from ..resolve.identity import build_identity_index
    from ..resolve.owner import infer_owner
    identity = build_identity_index(canon_items)
    owner = infer_owner(canon_items, identity)

    review_queue: list[dict] = []
    all_merges: list[MergeRecord] = []

    if mode == "llm" and llm_extractions is not None:
        people, orgs, locations, money, merges, rq = _llm_resolve_stage(
            rel_items, llm_extractions, owner, identity,
        )
        review_queue.extend(rq)
        all_merges.extend(merges)
        resolve_label = "llm"
    else:
        people, merges = E.resolve_people(
            rel_items, owner_refs=set(owner.refs), owner_label=owner.label,
            identifier_only=True,
        )
        all_merges.extend(merges)
        orgs = _domain_orgs_heuristic(people, start_index=0)
        locations = []
        money = E.extract_money(rel_items)
        resolve_label = "heuristic (identifier merges + structural relations)"

    subevents = E.extract_subevents(rel_items)
    documents = _extract_documents(rel_items)

    all_entities = people + orgs + locations + money + subevents + documents
    edges = _relations(rel_items, people, orgs, locations, money, subevents, documents)

    graph = EventGraph(entities=all_entities, edges=edges, merges=all_merges, relevance=relevance)
    timeline = _timeline(subevents, items)
    stats = {
        "items": len(items), "canonical_items": len(canon_items),
        "relevant_items": len(relevant_ids),
        "exact_dupes": dedup.n_exact_dupes, "near_dupes": dedup.n_near_dupes,
        "entities": len(all_entities), "edges": len(edges),
        "people": len(people), "merges": len(all_merges), "mode": mode,
        "owner": owner.label, "owner_confident": owner.confident,
        "resolve_label": resolve_label,
        "review_queue": len(review_queue),
    }
    return Bundle(items=items, dedup=dedup, graph=graph, timeline=timeline, stats=stats,
                  review_queue=review_queue, item_topics=item_topics)


def _mark_duplicates(items: list[SourceItem], dedup: DedupResult) -> None:
    for it in items:
        canon = dedup.canonical.get(it.id)
        it.duplicate_of = canon if (canon and canon != it.id) else None


# --------------------------------------------------------------------------- LLM stage
def _llm_stage(items, progress):
    """Run LLM extraction, return (relevance verdicts, chunk extractions, item_topics)."""
    from ..llm.extract import extract_chunks

    def _p(done, total):
        if progress:
            progress("llm", f"LLM extraction {done}/{total} batches")

    extractions = extract_chunks(items, progress=_p)

    # Collect all topics across all items
    item_topics_raw: dict[str, list[str]] = {}
    for ce in extractions:
        for mt in ce.messages:
            item_topics_raw.setdefault(mt.real_item_id, []).extend(mt.topics)

    # Consolidate near-identical topic labels
    all_topic_labels = [t for topics in item_topics_raw.values() for t in topics]
    topic_map = _consolidate_topics(all_topic_labels) if all_topic_labels else {}

    # Apply consolidation
    item_topics: dict[str, list[str]] = {}
    for iid, topics in item_topics_raw.items():
        consolidated = list(dict.fromkeys(topic_map.get(t, t) for t in topics))
        item_topics[iid] = consolidated

    relevance = []
    for it in items:
        topics = item_topics.get(it.id, [])
        # All items are included; topics stored for query-time scoping
        relevance.append(RelevanceVerdict(
            item_id=it.id,
            relevant=True,
            score=1.0 if topics else 0.5,
            rationale=", ".join(topics[:3]) if topics else "no topics assigned",
            topics=topics,
        ))

    return relevance, extractions, item_topics


def _llm_resolve_stage(rel_items, extractions, owner, identity):
    from ..resolve.profiles import build_profiles_from_extractions, build_profiles_from_identity_clusters
    from ..resolve.llm_resolve import resolve_profiles

    items_by_id = {it.id: it for it in rel_items}

    id_profiles = build_profiles_from_identity_clusters(identity.clusters, items_by_id)
    person_bundle = resolve_profiles(id_profiles, "person")

    ext_profiles = [p for p in build_profiles_from_extractions(extractions, items_by_id)
                    if p.type == "person"]

    people, base_merges = E.resolve_people(
        rel_items, owner_refs=set(owner.refs), owner_label=owner.label,
        identifier_only=True,
    )

    all_merges: list[MergeRecord] = list(base_merges) + list(person_bundle.merges)
    review_queue: list[dict] = list(person_bundle.review_queue)

    org_profiles = [p for p in build_profiles_from_extractions(extractions, items_by_id)
                    if p.type == "org"]
    if org_profiles:
        org_bundle = resolve_profiles(org_profiles, "org")
        all_merges.extend(org_bundle.merges)
        review_queue.extend(org_bundle.review_queue)
        orgs = _profiles_to_entities(org_profiles, org_bundle.merged_groups, EntityType.ORG, "org")
    else:
        orgs = _domain_orgs_heuristic(people, start_index=0)

    loc_profiles = [p for p in build_profiles_from_extractions(extractions, items_by_id)
                    if p.type == "location"]
    if loc_profiles:
        loc_bundle = resolve_profiles(loc_profiles, "location")
        all_merges.extend(loc_bundle.merges)
        review_queue.extend(loc_bundle.review_queue)
        locations = _profiles_to_entities(loc_profiles, loc_bundle.merged_groups,
                                          EntityType.LOCATION, "loc")
    else:
        locations = []

    money = _money_from_llm(extractions, rel_items)

    return people, orgs, locations, money, all_merges, review_queue


def _profiles_to_entities(profiles, merged_groups: dict, etype: EntityType, prefix: str) -> list[Entity]:
    profiles_by_id = {p.id: p for p in profiles}
    used: set[str] = set()
    ents: list[Entity] = []
    k = 0
    for root, members in merged_groups.items():
        if root in used:
            continue
        used |= members
        surface_counts: dict[str, int] = defaultdict(int)
        item_ids: set[str] = set()
        for mid in members:
            p = profiles_by_id.get(mid)
            if p:
                for surf, cnt in p.surfaces.items():
                    surface_counts[surf] += cnt
                item_ids |= p.item_ids
        if not surface_counts:
            continue
        label = max(surface_counts, key=lambda s: (surface_counts[s], len(s)))
        aliases = sorted(surface_counts)
        ents.append(Entity(
            id=f"{prefix}:{k}", type=etype, label=label,
            aliases=aliases,
            mentions=[Mention(item_id=i, text=label) for i in sorted(item_ids)],
        ))
        k += 1
    return ents


def _domain_orgs_heuristic(people, start_index=0) -> list[Entity]:
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


def _money_from_llm(extractions, items) -> list[Entity]:
    from collections import defaultdict
    from . import currency as C

    amt_items: dict[float, set] = defaultdict(set)
    amt_surfaces: dict[float, list] = defaultdict(list)

    for ce in extractions:
        for mn in ce.mentions:
            if mn.type != "money":
                continue
            m = _MONEY_RE.search(mn.surface)
            if m:
                amt = float(m.group(1).replace(",", ""))
                amt_items[amt].add(mn.real_item_id)
                amt_surfaces[amt].append(mn.surface)

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

    for o in orgs:
        for pid in o.attrs.get("member_person_ids", []):
            add(pid, o.id, "affiliated_with", list(o.item_ids)[:5])

    for mnt in money:
        best, best_ov = None, 0
        for o in orgs:
            ov = len(mnt.item_ids & o.item_ids)
            if ov > best_ov:
                best, best_ov = o, ov
        if best:
            add(mnt.id, best.id, "paid_to", list(mnt.item_ids & best.item_ids))

    for loc in locations:
        for o in orgs:
            ov = o.item_ids & loc.item_ids
            if len(ov) >= 2:
                add(o.id, loc.id, "held_at", list(ov), weight=float(len(ov)))

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
