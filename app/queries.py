"""Parameterized query types — the 'why a graph beats flat RAG' proof.

Each returns a structured answer with citations, table/steps, subgraph, caveats.
Deterministic over the built graph; query-time topic scoping is via item_topics on Bundle.
"""
from __future__ import annotations

from .pipeline.build import Bundle

QUERY_TYPES = [
    {"id": "money", "q": "What are the financial amounts and costs?"},
    {"id": "timeline", "q": "Show the timeline of events in order."},
    {"id": "who", "q": "Who are the key people involved?"},
]


def list_queries():
    return QUERY_TYPES


def _cite(b: Bundle, item_ids, k=8):
    m = {it.id: it for it in b.items}
    out = []
    for i in list(item_ids)[:k]:
        it = m.get(i)
        if it:
            out.append({"item_id": i, "source_type": it.source_type.value,
                        "snippet": (it.subject or it.body[:100] or "").strip()[:120]})
    return out


def run(query_id: str, b: Bundle) -> dict:
    if query_id not in _HANDLERS:
        raise KeyError(query_id)
    return _HANDLERS[query_id](b)


def money(b: Bundle) -> dict:
    """Return all money entities, split into resolved vs flagged (no currency marker)."""
    money_ents = [e for e in b.graph.entities if e.type.value == "money"]
    resolved = []
    flagged = []
    for m in money_ents:
        if m.attrs.get("needs_review") or not m.attrs.get("currency"):
            flagged.append(m)
        else:
            resolved.append(m)

    table = []
    for m in sorted(resolved, key=lambda e: -e.attrs.get("amount", 0)):
        table.append({
            "amount": m.label,
            "currency": m.attrs.get("currency", ""),
            "label": m.label,
            "sources": len(m.item_ids),
            "flagged": False,
        })
    for m in sorted(flagged, key=lambda e: -e.attrs.get("amount", 0)):
        table.append({
            "amount": f'{m.attrs.get("amount", 0):,.2f}',
            "currency": "?",
            "label": m.label,
            "sources": len(m.item_ids),
            "flagged": True,
        })

    n_resolved = len(resolved)
    n_flagged = len(flagged)
    answer = (
        f"{n_resolved} amount(s) resolved"
        + (f"; {n_flagged} amount(s) flagged (no currency marker, excluded from totals)" if n_flagged else "")
    ) if money_ents else "No financial amounts found."

    caveats = (
        ["Amounts with no currency marker are excluded from totals and labelled partial."]
        if n_flagged else []
    )

    all_ids = sorted({i for m in money_ents for i in m.item_ids})
    return {
        "answer": answer,
        "answer_parts": [answer],
        "table": table,
        "caveats": caveats,
        "citations": _cite(b, all_ids),
        "subgraph": [m.id for m in money_ents],
    }


def timeline(b: Bundle) -> dict:
    """Return timeline of sub-events and items with timestamps."""
    steps = []
    for r in b.timeline:
        steps.append({
            "timestamp": r["start"],
            "text": f'{r["label"]} — {r["count"]} items',
            "source_type": "subevent",
            "item_id": r.get("entity_id"),
        })

    answer = f"Timeline: {len(steps)} sub-event(s) in order." if steps else "No timeline events found."
    subgraph = [r.get("entity_id") for r in b.timeline if r.get("entity_id")]
    return {
        "answer": answer,
        "answer_parts": [answer],
        "steps": steps,
        "citations": [],
        "subgraph": subgraph,
    }


def who(b: Bundle) -> dict:
    """Return key people, excluding the owner entity."""
    people = {e.id: e for e in b.graph.entities if e.type.value == "person"}
    # Exclude owner
    people = {pid: e for pid, e in people.items() if not e.attrs.get("owner")}

    # Score by correspondence edges
    scores: dict[str, float] = {}
    for edge in b.graph.edges:
        if edge.kind == "corresponded_with":
            scores[edge.source] = scores.get(edge.source, 0) + edge.weight
            scores[edge.target] = scores.get(edge.target, 0) + edge.weight

    # Fall back to mention count
    for pid, e in people.items():
        if pid not in scores:
            scores[pid] = float(len(e.mentions))

    ranked = sorted(
        [(pid, sc) for pid, sc in scores.items() if pid in people],
        key=lambda kv: -kv[1]
    )[:10]

    table = []
    for pid, sc in ranked:
        e = people[pid]
        emails = e.attrs.get("emails", [])
        channels = list({m.item_id.split(":")[0] for m in e.mentions if ":" in m.item_id})
        table.append({
            "name": e.label,
            "mentions": len(e.mentions),
            "channels": channels,
            "emails": emails[:2],
        })

    answer = f"{len(table)} key people identified (owner excluded)." if table else "No people found."
    return {
        "answer": answer,
        "answer_parts": [answer],
        "table": table,
        "citations": [],
        "subgraph": [pid for pid, _ in ranked],
    }


def entity(eid: str, b: Bundle) -> dict:
    """Fetch entity by id, return attributes, aliases, mentions, edges."""
    e = b.graph.entity(eid)
    if not e:
        return {"error": f"entity {eid!r} not found", "answer": "", "answer_parts": [], "citations": [], "subgraph": []}
    edges = [ed for ed in b.graph.edges if ed.source == eid or ed.target == eid]
    return {
        "answer": f"Entity: {e.label} ({e.type.value})",
        "answer_parts": [f"Entity: {e.label} ({e.type.value})"],
        "entity": e.model_dump(),
        "connected_edges": [ed.model_dump() for ed in edges[:20]],
        "citations": _cite(b, list(e.item_ids)[:8]),
        "subgraph": [eid] + [ed.target if ed.source == eid else ed.source for ed in edges[:10]],
    }


def starter_questions(b: Bundle) -> list[dict]:
    """Generate starter questions from query types + top entities."""
    questions = list(QUERY_TYPES)
    # Add entity-specific questions from top 2 most-mentioned people
    people = sorted(
        [e for e in b.graph.entities if e.type.value == "person" and not e.attrs.get("owner")],
        key=lambda e: -len(e.mentions)
    )
    for p in people[:2]:
        questions.append({
            "id": "who",
            "q": f"What do we know about {p.label}?",
            "entity_id": p.id,
        })
    return questions


_HANDLERS = {
    "money": money,
    "timeline": timeline,
    "who": who,
    # keep backward compat aliases
    "trip_cost": money,
    "correspondents": who,
    "visa_timeline": timeline,
}
