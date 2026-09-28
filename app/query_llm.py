"""Free-form natural-language querying over the event graph (GraphRAG answer step).

Retrieves the most relevant source items for a question, packs them alongside a
compact view of the knowledge graph (canonical entities + timeline), and asks a
*smarter* LLM to answer using only that context — citing item ids for provenance.
This is the "queryable data" payoff: ask anything, get a grounded answer.
"""
from __future__ import annotations

import re

from .llm.client import LLMClient, get_query_client
from .pipeline.build import Bundle
from .schema import normalize_text

_STOP = set(
    "the a an of to in on for and or is are was were be been with at by from as it "
    "this that these those i you he she they we my your our what when where who how "
    "did do does me about into out over under".split()
)

SYSTEM = """You answer questions about one person's real event — an academic
conference trip (paper submission → registration/payment → visa → travel →
attending) — using ONLY the knowledge graph and source items provided.

Rules:
- Ground every claim in the source items. Cite the item ids you used in square
  brackets, e.g. [batch1_emails_redacted#3]. Cite multiple when relevant.
- Be specific: give exact amounts, dates, names, and counts when present.
- If the context doesn't contain the answer, say so plainly — do not guess.
- Prefer the resolved entities in the graph (e.g. a person's multiple emails are
  the same person) when reasoning about who/what.""".strip()


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", normalize_text(s)) if t not in _STOP and len(t) > 1}


def _retrieve(question: str, bundle: Bundle, k: int = 16):
    rel = {v.item_id for v in bundle.graph.relevance if v.relevant}
    q = _tokens(question)
    scored = []
    for it in bundle.items:
        overlap = len(q & _tokens(f"{it.subject or ''} {it.body} {it.sender}"))
        if overlap:
            # small boost for relevant items so noise doesn't dominate
            scored.append((overlap + (1 if it.id in rel else 0), it))
    scored.sort(key=lambda x: -x[0])
    top = [it for _, it in scored[:k]]
    # if nothing matched, fall back to the most recent relevant items
    if not top:
        top = [it for it in bundle.items if it.id in rel][:k]
    return top


def _graph_context(bundle: Bundle) -> str:
    lines = []
    ppl = [e for e in bundle.graph.entities if e.type.value == "person"]
    ppl.sort(key=lambda e: -len(e.mentions))
    for e in ppl[:25]:
        emails = e.attrs.get("emails", [])
        lines.append(f"- PERSON {e.label}" + (f" <{', '.join(emails[:3])}>" if emails else ""))
    for e in bundle.graph.entities:
        if e.type.value in ("org", "location", "money"):
            lines.append(f"- {e.type.value.upper()} {e.label}")
    tl = "\n".join(f"- {r['label']}: {r['start'][:10]} → {r['end'][:10]} ({r['count']} items)"
                   for r in bundle.timeline)
    return "ENTITIES:\n" + "\n".join(lines[:80]) + "\n\nTIMELINE:\n" + tl


def query_graph(question: str, bundle: Bundle, client: LLMClient | None = None) -> dict:
    client = client or get_query_client()
    items = _retrieve(question, bundle)
    itemmap = {it.id: it for it in bundle.items}

    def _line(it):
        ts = it.timestamp.isoformat()[:16] if it.timestamp else "n/a"
        body = re.sub(r"\s+", " ", it.body)[:450]
        return f"[{it.id}] ({it.source_type.value}, {ts}) from {it.sender[:50]}: {body}"

    item_block = "\n\n".join(_line(it) for it in items)
    user = (
        f"QUESTION: {question}\n\n{_graph_context(bundle)}\n\n"
        f"SOURCE ITEMS (cite these ids):\n{item_block}"
    )
    answer = client.complete_json(SYSTEM, user, max_tokens=1024)

    cited_ids = [c for c in re.findall(r"\[([^\]]+)\]", answer) if c in itemmap]
    citations = [
        {"item_id": cid, "source_type": itemmap[cid].source_type.value,
         "snippet": (itemmap[cid].subject or itemmap[cid].body[:90] or "").strip()[:120]}
        for cid in dict.fromkeys(cited_ids)
    ]
    return {
        "question": question,
        "answer": answer,
        "citations": citations,
        "retrieved": [it.id for it in items],
        "model": getattr(client, "_model", "mock"),
    }
