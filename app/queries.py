"""Canned cross-source queries — the 'why a graph beats flat RAG' proof.

Each returns a structured answer with citations (item ids) and a sub-graph to
highlight. Deterministic over the built graph; an LLM query planner would replace
`run` while keeping this shape.
"""
from __future__ import annotations

from datetime import timezone

from .pipeline.build import Bundle

QUERIES = [
    {"id": "trip_cost", "q": "What did the trip cost, and what was each payment for?"},
    {"id": "visa_timeline", "q": "Show me everything about the visa, in order."},
    {"id": "correspondents", "q": "Who did I correspond with about the conference?"},
    {"id": "timeline", "q": "What happened between acceptance and the trip, in order?"},
    {"id": "sick_control", "q": "Was I sick during the trip? (negative control — noise handling)"},
]


def list_queries():
    return QUERIES


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
    return _HANDLERS[query_id](b)


def _trip_cost(b: Bundle) -> dict:
    from collections import defaultdict
    from .pipeline import currency as C

    money = [e for e in b.graph.entities if e.type.value == "money"]
    by_cur: dict = defaultdict(list)
    flagged = []
    for m in money:
        if m.attrs.get("needs_review") or not m.attrs.get("currency"):
            flagged.append(m)
        else:
            by_cur[m.attrs["currency"]].append(m)

    groups = []
    for cur, ms in sorted(by_cur.items()):
        ms.sort(key=lambda e: -e.attrs["amount"])
        amounts = [e.attrs["amount"] for e in ms]
        largest = max(amounts)
        comp_sum = sum(a for a in amounts if 0 < a < largest)
        groups.append({
            "currency": cur, "symbol": C.SYMBOL_OF.get(cur, cur),
            "largest": largest, "components_sum": comp_sum,
            "rows": [{"amount": e.label, "evidence_items": len(e.item_ids)} for e in ms],
        })

    flag_rows = [{"amount": f'{e.attrs["amount"]:,.2f}', "entity_id": e.id,
                  "why": "mixed-currency" if e.attrs.get("currency_ambiguous") else "no currency marker in source"}
                 for e in sorted(flagged, key=lambda e: -e.attrs["amount"])]

    parts = [f'{g["symbol"]}{g["largest"]:,.2f} ({g["currency"]})' for g in groups]
    answer = "Costs span multiple currencies: " + ", ".join(parts) if parts else "No resolved amounts."
    note = (
        "Amounts are grouped by the currency actually found in the source — never assumed. "
        f"{len(flag_rows)} amount(s) carried NO currency marker (e.g. bare WhatsApp numbers) and are "
        "FLAGGED for you to resolve; they are excluded from every total until you do. "
        "Within the USD invoice, the largest is the registration total and the smaller amounts are its "
        "line-item components (summing all would double-count)."
    )
    cites = _cite(b, sorted({i for m in money for i in m.item_ids}))
    return {"question": QUERIES[0]["q"], "answer": answer,
            "groups": groups, "flagged": flag_rows, "note": note, "citations": cites,
            "subgraph": [m.id for m in money] + [e.id for e in b.graph.entities if e.type.value == "org"][:2]}


def _subevent(b: Bundle, label: str):
    return next((e for e in b.graph.entities if e.type.value == "subevent" and e.label == label), None)


def _visa_timeline(b: Bundle) -> dict:
    se = _subevent(b, "Visa")
    m = {it.id: it for it in b.items}
    rel = {v.item_id for v in b.graph.relevance if v.relevant}
    items = [m[i] for i in (se.item_ids if se else set()) if i in m and i in rel]
    items.sort(key=lambda it: (it.timestamp.replace(tzinfo=timezone.utc)
                               if it.timestamp and it.timestamp.tzinfo is None
                               else it.timestamp) or _min())
    steps = [{"timestamp": it.timestamp.isoformat() if it.timestamp else None,
              "source_type": it.source_type.value, "channel": it.channel,
              "text": (it.subject or it.body[:90]).strip()[:110], "item_id": it.id}
             for it in items[:15]]
    return {"question": QUERIES[1]["q"],
            "answer": f"{len(items)} visa-related items across "
                      f"{len({it.source_type.value for it in items})} channels, in order:",
            "steps": steps, "citations": _cite(b, [it.id for it in items]),
            "subgraph": [se.id] if se else []}


def _correspondents(b: Bundle) -> dict:
    people = {e.id: e for e in b.graph.entities if e.type.value == "person"}
    scores = {}
    for e in b.graph.edges:
        if e.kind == "corresponded_with":
            scores[e.source] = scores.get(e.source, 0) + e.weight
            scores[e.target] = scores.get(e.target, 0) + e.weight
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:10]
    rows = [{"person": people[pid].label, "emails": people[pid].attrs.get("emails", []),
             "message_weight": int(w)} for pid, w in ranked if pid in people]
    return {"question": QUERIES[2]["q"],
            "answer": f"Top correspondents (across email + WhatsApp), after resolving "
                      f"duplicate identities:",
            "table": rows, "citations": [],
            "subgraph": [pid for pid, _ in ranked]}


def _timeline_q(b: Bundle) -> dict:
    return {"question": QUERIES[3]["q"],
            "answer": "Reconstructed event timeline (sub-events by earliest evidence):",
            "steps": [{"timestamp": r["start"], "text": f'{r["label"]} — {r["count"]} items',
                       "source_type": "subevent", "item_id": None} for r in b.timeline],
            "citations": [], "subgraph": [r["entity_id"] for r in b.timeline]}


def _sick_control(b: Bundle) -> dict:
    """Negative control: illness content exists but was scoped OUT of the event —
    proves relevance handling both keeps it findable AND out of event answers."""
    m = {it.id: it for it in b.items}
    rel = {v.item_id: v for v in b.graph.relevance}
    sick = [it for it in b.items
            if any(w in it.body.lower() for w in ("fever", "sick", "unwell", "ill "))]
    excluded = [it for it in sick if not rel[it.id].relevant]
    return {"question": QUERIES[4]["q"],
            "answer": f"Found {len(sick)} messages mentioning illness. "
                      f"{len(excluded)} were scoped OUT of the event graph as noise "
                      f"(so they never contaminate cost/timeline answers) — but they "
                      f"remain findable here.",
            "steps": [{"timestamp": it.timestamp.isoformat() if it.timestamp else None,
                       "source_type": it.source_type.value,
                       "text": it.body[:110].strip(),
                       "item_id": it.id,
                       "note": rel[it.id].rationale} for it in sick[:10]],
            "citations": _cite(b, [it.id for it in sick]), "subgraph": []}


def _min():
    from datetime import datetime
    return datetime.min.replace(tzinfo=timezone.utc)


_HANDLERS = {
    "trip_cost": _trip_cost, "visa_timeline": _visa_timeline,
    "correspondents": _correspondents, "timeline": _timeline_q, "sick_control": _sick_control,
}
