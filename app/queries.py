"""Structured queries over the event graph — results always cite source items.

Each query uses GraphNavigator to walk the graph rather than doing a flat scan.
Returns a standard envelope:
  answer        str   — one-sentence summary
  answer_parts  list  — same, for the UI accordion
  table / steps list  — structured rows
  caveats       list  — strings
  citations     list  — {item_id, source_type, snippet}
  subgraph      list  — entity ids to highlight
"""
from __future__ import annotations

from .graph_nav import GraphNavigator
from .pipeline.build import Bundle

QUERY_TYPES = [
    {"id": "money",    "q": "What are the financial amounts and costs?"},
    {"id": "timeline", "q": "Show the timeline of events in order."},
    {"id": "who",      "q": "Who are the key people involved?"},
]


def list_queries():
    return QUERY_TYPES


def _cite(b: Bundle, item_ids, k: int = 8) -> list[dict]:
    m = {it.id: it for it in b.items}
    out = []
    for iid in list(item_ids)[:k]:
        it = m.get(iid)
        if it:
            out.append({
                "item_id": iid,
                "source_type": it.source_type.value,
                "snippet": (it.subject or it.body[:100] or "").strip()[:120],
            })
    return out


def run(query_id: str, b: Bundle) -> dict:
    if query_id not in _HANDLERS:
        raise KeyError(query_id)
    return _HANDLERS[query_id](b)


# ── money ─────────────────────────────────────────────────────────────────────

def money(b: Bundle) -> dict:
    nav = GraphNavigator(b)
    rows = nav.money_rows()
    totals = nav.money_totals()

    n_resolved = sum(1 for r in rows if not r["flagged"])
    n_flagged = sum(1 for r in rows if r["flagged"])

    table = []
    for r in rows:
        row = {
            "label": r["label"],
            "amount": r["label"],
            "currency": r["currency"] or "?",
            "payers": r["payers"],
            "payees": r["payees"],
            "sources": len(r["item_ids"]),
            "flagged": r["flagged"],
            "context": r["context"],
        }
        table.append(row)

    total_lines = [f"{v:,.2f} {cur}" for cur, v in sorted(totals.items())]
    if total_lines:
        total_str = "Total: " + ", ".join(total_lines)
    else:
        total_str = ""

    if rows:
        answer = f"{n_resolved} amount(s) resolved"
        if n_flagged:
            answer += f"; {n_flagged} flagged (no currency marker — excluded from totals)"
        if total_str:
            answer += ". " + total_str
    else:
        answer = "No financial amounts found."

    all_item_ids = sorted({iid for r in rows for iid in r["item_ids"]})
    return {
        "answer": answer,
        "answer_parts": [answer],
        "table": table,
        "totals": totals,
        "caveats": (
            ["Amounts with no currency marker are flagged and excluded from totals."]
            if n_flagged else []
        ),
        "citations": _cite(b, all_item_ids),
        "subgraph": [r["entity_id"] for r in rows],
    }


# ── timeline ──────────────────────────────────────────────────────────────────

def timeline(b: Bundle) -> dict:
    nav = GraphNavigator(b)
    episodes = nav.timeline_episodes()

    steps = [
        {
            "timestamp": ep["start"],
            "text": f'{ep["label"]} — {ep["count"]} item(s)'
                    + (f' through {ep["end"][:10]}' if ep["start"][:10] != ep["end"][:10] else ""),
            "source_type": ep.get("source", "heuristic"),
            "item_id": ep.get("entity_id"),
            "item_ids": ep.get("item_ids", []),
        }
        for ep in episodes
    ]

    answer = f"Timeline: {len(steps)} episode(s) in order." if steps else "No timeline events found."
    all_item_ids = [iid for ep in episodes for iid in ep.get("item_ids", [])]
    subgraph = [ep.get("entity_id") for ep in episodes if ep.get("entity_id")]

    return {
        "answer": answer,
        "answer_parts": [answer],
        "steps": steps,
        "citations": _cite(b, all_item_ids),
        "subgraph": subgraph,
    }


# ── who ───────────────────────────────────────────────────────────────────────

def who(b: Bundle) -> dict:
    nav = GraphNavigator(b)
    rows = nav.who_rows()

    table = [
        {
            "name": r["name"],
            "emails": r["emails"],
            "mentions": r["mentions"],
            "correspondence_weight": r["correspondence_weight"],
            "orgs": r["orgs"],
            "channels": r["channels"],
        }
        for r in rows
    ]

    answer = f"{len(table)} key people identified (owner excluded)." if table else "No people found."
    all_item_ids = [iid for r in rows for iid in r["item_ids"]]

    return {
        "answer": answer,
        "answer_parts": [answer],
        "table": table,
        "citations": _cite(b, all_item_ids),
        "subgraph": [r["entity_id"] for r in rows],
    }


# ── entity detail ─────────────────────────────────────────────────────────────

def entity(eid: str, b: Bundle) -> dict:
    e = b.graph.entity(eid)
    if not e:
        return {
            "error": f"entity {eid!r} not found",
            "answer": "", "answer_parts": [], "citations": [], "subgraph": [],
        }
    edges = [ed for ed in b.graph.edges if ed.source == eid or ed.target == eid]
    return {
        "answer": f"Entity: {e.label} ({e.type.value})",
        "answer_parts": [f"Entity: {e.label} ({e.type.value})"],
        "entity": e.model_dump(),
        "connected_edges": [ed.model_dump() for ed in edges[:20]],
        "citations": _cite(b, list(e.item_ids)[:8]),
        "subgraph": [eid] + [ed.target if ed.source == eid else ed.source for ed in edges[:10]],
    }


# ── starter questions ─────────────────────────────────────────────────────────

def starter_questions(b: Bundle) -> list[dict]:
    questions = list(QUERY_TYPES)
    people = sorted(
        [e for e in b.graph.entities if e.type.value == "person" and not e.attrs.get("owner")],
        key=lambda e: -len(e.mentions),
    )
    for p in people[:2]:
        questions.append({"id": "who", "q": f"What do we know about {p.label}?",
                          "entity_id": p.id})
    return questions


_HANDLERS = {
    "money": money,
    "timeline": timeline,
    "who": who,
    "correspondents": who,
}
