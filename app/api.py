"""FastAPI app for the local mockup.

Builds the whole EventGraph in memory on startup (no DB needed to view progress),
and serves the corpus, graph, timeline, ER merge log, relevance verdicts, and a set
of canned cross-source queries with citations + highlighted sub-graph.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from .ingest.markdown import parse_batch_file
from .jobs import JobManager
from .pipeline.build import Bundle, build_graph
from . import queries as Q

DATA_DIR = Path(__file__).resolve().parents[1] / "processed_data"

STATE: dict[str, Bundle] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import store

    saved = store.load_bundle()
    if saved is not None:
        STATE["bundle"] = saved  # your last real ingest — no rebuild, no re-cost
    else:
        items = []  # first run: seed the demo corpus so the UI isn't empty
        for f in sorted(DATA_DIR.glob("batch*.md")):
            items += parse_batch_file(f)
        STATE["bundle"] = build_graph(items)
    yield


app = FastAPI(title="EventGraph", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def bundle() -> Bundle:
    return STATE["bundle"]


def _publish(new_bundle: Bundle):
    STATE["bundle"] = new_bundle


JOBS = JobManager(on_bundle=_publish)


@app.post("/api/ingest")
async def ingest(files: list[UploadFile] = File(...), mode: str = Query("heuristic")):
    """Bulk upload → async pipeline. mode=heuristic (instant) | llm (uses the model)."""
    payload = [(f.filename or "upload", await f.read()) for f in files]
    job = JOBS.start(payload, mode=mode)
    return {"job_id": job.id, "n_files": len(payload)}


@app.get("/api/capabilities")
def capabilities():
    from .llm.client import llm_available
    return {"llm_available": llm_available()}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    return EventSourceResponse(JOBS.stream(job_id))


@app.get("/api/stats")
def stats():
    b = bundle()
    return {**b.stats, "queries": Q.list_queries()}


@app.get("/api/graph")
def graph(min_degree: int = 1):
    """Nodes + edges for visualization. Isolated low-signal nodes trimmed."""
    b = bundle()
    deg: dict[str, int] = {}
    for e in b.graph.edges:
        deg[e.source] = deg.get(e.source, 0) + 1
        deg[e.target] = deg.get(e.target, 0) + 1
    keep = {
        e.id for e in b.graph.entities
        if deg.get(e.id, 0) >= min_degree or e.type.value in ("subevent", "org", "location", "money", "document")
    }
    nodes = [
        {"id": e.id, "label": e.label, "type": e.type.value,
         "mentions": len(e.mentions), "degree": deg.get(e.id, 0),
         "emails": e.attrs.get("emails", []),
         "currency": e.attrs.get("currency"), "needs_review": e.attrs.get("needs_review", False)}
        for e in b.graph.entities if e.id in keep
    ]
    links = [
        {"source": e.source, "target": e.target, "kind": e.kind, "weight": e.weight}
        for e in b.graph.edges if e.source in keep and e.target in keep
    ]
    return {"nodes": nodes, "links": links}


@app.get("/api/entity/{entity_id}")
def entity(entity_id: str):
    b = bundle()
    e = b.graph.entity(entity_id)
    if not e:
        raise HTTPException(404, "entity not found")
    itemmap = {it.id: it for it in b.items}
    provenance = [
        {"item_id": m.item_id, "text": m.text,
         "source_type": itemmap[m.item_id].source_type.value if m.item_id in itemmap else "",
         "snippet": (itemmap[m.item_id].body[:160] if m.item_id in itemmap else "")}
        for m in e.mentions[:50]
    ]
    return {"entity": e.model_dump(), "provenance": provenance}


@app.get("/api/merges")
def merges():
    return [m.model_dump() for m in bundle().graph.merges if len(m.merged_forms) > 1]


@app.get("/api/timeline")
def timeline():
    return bundle().timeline


@app.get("/api/items")
def items(source_type: str | None = None, relevant: bool | None = None,
          conversation: str | None = None, q: str | None = None,
          limit: int = Query(100, le=500), offset: int = 0):
    b = bundle()
    rel = {v.item_id: v for v in b.graph.relevance}
    rows = b.items
    if source_type:
        rows = [it for it in rows if it.source_type.value == source_type]
    if conversation:
        rows = [it for it in rows if it.conversation_id == conversation]
    if relevant is not None:
        rows = [it for it in rows if rel[it.id].relevant == relevant]
    if q:
        ql = q.lower()
        rows = [it for it in rows if ql in it.body.lower() or ql in (it.subject or "").lower()]
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "total": total,
        "items": [
            {"id": it.id, "source_type": it.source_type.value, "channel": it.channel,
             "conversation_id": it.conversation_id,
             "timestamp": it.timestamp.isoformat() if it.timestamp else None,
             "sender": it.sender_display, "subject": it.subject,
             "preview": it.body[:140],
             "relevant": rel[it.id].relevant, "relevance_rationale": rel[it.id].rationale}
            for it in page
        ],
    }


@app.get("/api/item/{item_id}")
def item(item_id: str):
    b = bundle()
    it = next((x for x in b.items if x.id == item_id), None)
    if not it:
        raise HTTPException(404, "item not found")
    rel = next((v for v in b.graph.relevance if v.item_id == item_id), None)
    ents = [{"id": e.id, "label": e.label, "type": e.type.value}
            for e in b.graph.entities if item_id in e.item_ids]
    return {
        "id": it.id, "source_type": it.source_type.value, "channel": it.channel,
        "conversation_id": it.conversation_id,
        "timestamp": it.timestamp.isoformat() if it.timestamp else None,
        "sender": it.sender_display, "recipients": it.recipients_display,
        "participants": [p.model_dump() for p in it.participants],
        "subject": it.subject, "body": it.body, "notes": it.notes,
        "relevance": rel.model_dump() if rel else None,
        "entities": ents,
    }


@app.get("/api/flags/currency")
def currency_flags():
    """Amounts whose currency the system could NOT determine from the source — the
    user resolves these; we never assume a currency."""
    from .pipeline import currency as C
    b = bundle()
    itemmap = {it.id: it for it in b.items}
    out = []
    for e in b.graph.entities:
        if e.type.value != "money" or not e.attrs.get("needs_review"):
            continue
        srcs = []
        for m in e.mentions[:4]:
            it = itemmap.get(m.item_id)
            if it:
                srcs.append({"item_id": m.item_id, "source_type": it.source_type.value,
                             "snippet": (it.body[:140]).strip()})
        out.append({"entity_id": e.id, "amount": e.attrs.get("amount"),
                    "reason": "mixed-currency" if e.attrs.get("currency_ambiguous") else "no-currency-marker",
                    "evidence": e.attrs.get("currency_evidence", []), "sources": srcs})
    out.sort(key=lambda x: -(x["amount"] or 0))
    return {"currencies": C.KNOWN, "flagged": out}


@app.post("/api/resolve/currency")
def resolve_currency(entity_id: str = Query(...), currency: str = Query(...)):
    """User resolves a flagged amount by assigning its real currency. Persisted."""
    from .pipeline import currency as C
    if currency not in C.KNOWN:
        raise HTTPException(400, f"unknown currency {currency!r}; use one of {C.KNOWN}")
    b = bundle()
    e = b.graph.entity(entity_id)
    if not e or e.type.value != "money":
        raise HTTPException(404, "money entity not found")
    e.attrs.update({"currency": currency, "currency_resolved": True,
                    "currency_ambiguous": False, "needs_review": False, "resolved_by": "user"})
    e.label = C.label(e.attrs["amount"], e.attrs)
    for m in e.mentions:
        m.text = e.label
    from . import store
    store.save_bundle(b)
    return {"entity_id": entity_id, "label": e.label, "currency": currency}


@app.get("/api/query/{query_id}")
def run_query(query_id: str):
    b = bundle()
    try:
        return Q.run(query_id, b)
    except KeyError:
        raise HTTPException(404, "unknown query")


@app.get("/api/ask")
def ask(q: str = Query(..., min_length=2)):
    """Free-form natural-language question answered by the smart LLM over the graph."""
    from .query_llm import query_graph
    return query_graph(q, bundle())


@app.get("/api/capabilities/models")
def model_info():
    import os
    return {
        "extraction_model": os.getenv("LLM_MODEL", "claude-sonnet-4-6"),
        "query_model": os.getenv("LLM_QUERY_MODEL", "claude-opus-4-8"),
    }
