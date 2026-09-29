"""Free-form natural-language querying via graph-navigation + LLM.

Flow:
  1. Match entities in the question → seed set.
  2. BFS-expand neighbourhood (depth 2) → subgraph + evidence items.
  3. Feed compact subgraph description + top evidence items to the LLM.
  4. Parse cited item IDs from the answer, validate against corpus.
"""
from __future__ import annotations

import re

from .graph_nav import GraphNavigator
from .llm.client import LLMClient, get_query_client
from .pipeline.build import Bundle

SYSTEM = """You answer questions about a personal archive using ONLY the knowledge
graph and source items provided below.

Rules:
- Ground every claim in the source items. Cite item ids in square brackets,
  e.g. [my_email#3]. Cite multiple ids when relevant.
- Be specific: give exact amounts, dates, names, and counts when present.
- If the context does not contain the answer, say so plainly — do not guess.
- Prefer resolved graph entities (a person's multiple aliases/emails are one
  person) when reasoning about who-did-what.""".strip()


def query_graph(
    question: str,
    bundle: Bundle,
    client: LLMClient | None = None,
) -> dict:
    client = client or get_query_client()
    nav = GraphNavigator(bundle)

    # Graph-navigation retrieval
    subgraph_text, items = nav.ask_context(question, k_items=16)
    itemmap = {it.id: it for it in bundle.items}

    def _line(it) -> str:
        ts = it.timestamp.isoformat()[:16] if it.timestamp else "n/a"
        body = re.sub(r"\s+", " ", it.body)[:450]
        sender = getattr(it, "sender_display", "") or ""
        return f"[{it.id}] ({it.source_type.value}, {ts}) from {sender[:50]}: {body}"

    item_block = "\n\n".join(_line(it) for it in items)
    user = (
        f"QUESTION: {question}\n\n"
        f"GRAPH CONTEXT (relevant subgraph):\n{subgraph_text}\n\n"
        f"SOURCE ITEMS (cite these ids):\n{item_block}"
    )

    answer = client.complete_json(SYSTEM, user, max_tokens=1024)

    cited_ids = [c for c in re.findall(r"\[([^\]]+)\]", answer) if c in itemmap]
    citations = [
        {
            "item_id": cid,
            "source_type": itemmap[cid].source_type.value,
            "snippet": (itemmap[cid].subject or itemmap[cid].body[:90] or "").strip()[:120],
        }
        for cid in dict.fromkeys(cited_ids)
    ]

    return {
        "question": question,
        "answer": answer,
        "citations": citations,
        "retrieved": [it.id for it in items],
        "model": getattr(client, "_model", "mock"),
        "subgraph_text": subgraph_text,
    }
