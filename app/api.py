"""FastAPI app for the EventGraph demo.

DEMO_READONLY=1 (env): the committed data/bundle.json is served read-only.
Uploads in that mode build a session-scoped in-memory bundle (cookie session
id, LRU+TTL) — never persisted, never replaces the shared demo graph.

Normal mode: uploads replace the live bundle and are persisted to .cache/.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .ingest.markdown import parse_batch_file
from .jobs import JobManager
from .pipeline.build import Bundle, build_graph
from . import queries as Q
from .session_store import SessionStore
from .rate_limit import RateLimiter

DATA_DIR = Path(__file__).resolve().parents[1] / "processed_data"

DEMO_READONLY: bool = os.getenv("DEMO_READONLY", "").lower() in ("1", "true", "yes")
_MAX_Q_LEN = 500

# Simple hardcoded auth gate. Set APP_TOKEN=<secret> in env to enable.
# All /api/* routes require "Authorization: Bearer <token>" when set.
# Leave unset (or empty) to disable — handy for local dev.
_APP_TOKEN: str | None = os.getenv("APP_TOKEN") or None

STATE: dict[str, Bundle] = {}
SESSION_STORE = SessionStore()
RATE_LIMITER = RateLimiter()

_raw_origins = os.getenv("CORS_ORIGINS", "*")
_cors_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
# Browsers reject wildcard + credentials together; only enable credentials
# when explicit origins are configured (i.e. production with a real domain).
_cors_credentials = "*" not in _cors_origins


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import store
    import logging
    log = logging.getLogger(__name__)

    saved = store.load_bundle()
    if saved is not None:
        STATE["bundle"] = saved  # .cache/ or data/bundle.json — whichever is fresher
    else:
        # No persisted bundle — try building from processed_data/ if it exists locally.
        # On a clean deploy (no volume) this yields an empty graph, which is fine:
        # the UI shows an upload prompt.
        items = []
        for f in sorted(DATA_DIR.glob("batch*.md")):
            items += parse_batch_file(f)
        if not items:
            log.warning(
                "No bundle and no corpus found — starting with an empty graph. "
                "Upload files via the UI or mount data/bundle.json to populate the demo."
            )
        STATE["bundle"] = build_graph(items)
    yield


app = FastAPI(title="EventGraph", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=_cors_credentials,
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Reject all /api/* calls unless the correct Bearer token is presented.

    Skipped entirely when APP_TOKEN is not set (local dev default).
    /api/health is always public so uptime monitors don't need the token.
    """
    # Let CORS preflight through — the browser sends OPTIONS with no auth header.
    if (
        _APP_TOKEN
        and request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/health"
    ):
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {_APP_TOKEN}":
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)


# ── helpers ────────────────────────────────────────────────────────────────────

def _get_ip(request: Request) -> str:
    ff = request.headers.get("x-forwarded-for")
    if ff:
        return ff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_bundle(
    session_id: str | None = Cookie(None, alias="eventgraph_session"),
) -> Bundle:
    """FastAPI dependency: session bundle when present, else the demo/live bundle."""
    if DEMO_READONLY and session_id:
        sess = SESSION_STORE.get(session_id)
        if sess is not None:
            return sess  # type: ignore[return-value]
    return STATE["bundle"]


def _publish(bundle: Bundle, session_id: str | None = None) -> None:
    """Publish a freshly built bundle.

    DEMO_READONLY: store under the session — never touch STATE or disk.
    Normal: replace the live bundle and persist to .cache/.
    """
    if DEMO_READONLY:
        if session_id:
            SESSION_STORE.put(session_id, bundle)
    else:
        STATE["bundle"] = bundle
        from . import store
        store.save_bundle(bundle)


JOBS = JobManager(on_bundle=_publish)


# ── endpoints ──────────────────────────────────────────────────────────────────

@app.post("/api/admin/upload-bundle")
async def upload_bundle(file: UploadFile = File(...)):
    """One-time admin endpoint: upload a bundle.json to populate the demo graph.

    Requires APP_TOKEN (handled by auth_gate middleware).
    After upload the bundle is hot-reloaded — no restart needed.
    """
    from . import store as _store
    contents = await file.read()
    _store.DATA_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _store.DATA_BUNDLE.with_suffix(".tmp")
    tmp.write_bytes(contents)
    tmp.replace(_store.DATA_BUNDLE)

    bundle = _store._parse_bundle(_store.DATA_BUNDLE)
    if bundle is None:
        raise HTTPException(400, "Uploaded file is not a valid bundle.json")
    STATE["bundle"] = bundle
    return {
        "status": "loaded",
        "entities": len(bundle.graph.entities),
        "items": len(bundle.items),
    }


@app.get("/api/health")
def health():
    from .llm.client import llm_available
    from . import store as _store
    b = STATE.get("bundle")
    # Determine which bundle source was used at startup
    if _store.SNAPSHOT.exists():
        source = "local_cache"
    elif _store.DATA_BUNDLE.exists():
        source = "committed_data"
    else:
        source = "rebuilt"
    return {
        "status": "ok",
        "demo_readonly": DEMO_READONLY,
        "bundle_source": source,
        "entities": len(b.graph.entities) if b else 0,
        "items": len(b.items) if b else 0,
        "llm_available": llm_available(),
        "active_sessions": SESSION_STORE.count() if DEMO_READONLY else None,
    }


@app.post("/api/ingest")
async def ingest(
    request: Request,
    files: list[UploadFile] = File(...),
    mode: str = Query("heuristic"),
    session_id: str | None = Cookie(None, alias="eventgraph_session"),
):
    """Bulk upload → async pipeline. mode=heuristic (instant) | llm (uses the model).

    In DEMO_READONLY mode the upload builds a session-scoped bundle and sets a
    session cookie — the shared demo graph is never modified.
    """
    payload = [(f.filename or "upload", await f.read()) for f in files]

    if DEMO_READONLY:
        if not session_id:
            session_id = str(uuid.uuid4())
        job = JOBS.start(payload, mode=mode, session_id=session_id)
        resp = JSONResponse({"job_id": job.id, "n_files": len(payload)})
        resp.set_cookie(
            "eventgraph_session", session_id,
            max_age=3600, samesite="lax", httponly=True,
        )
        return resp

    job = JOBS.start(payload, mode=mode)
    return {"job_id": job.id, "n_files": len(payload)}


@app.get("/api/capabilities")
def capabilities():
    from .llm.client import llm_available
    return {"llm_available": llm_available(), "demo_readonly": DEMO_READONLY}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    return EventSourceResponse(JOBS.stream(job_id))


@app.get("/api/stats")
def stats(b: Bundle = Depends(get_bundle)):
    return {**b.stats, "queries": Q.list_queries()}


@app.get("/api/graph")
def graph(min_degree: int = 1, b: Bundle = Depends(get_bundle)):
    """Nodes + edges for visualization. Isolated low-signal nodes trimmed."""
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
def entity(entity_id: str, b: Bundle = Depends(get_bundle)):
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
def merges(b: Bundle = Depends(get_bundle)):
    return [m.model_dump() for m in b.graph.merges if len(m.merged_forms) > 1]


@app.get("/api/timeline")
def timeline(b: Bundle = Depends(get_bundle)):
    return b.timeline


@app.get("/api/items")
def items(
    source_type: str | None = None,
    relevant: bool | None = None,
    conversation: str | None = None,
    q: str | None = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
    b: Bundle = Depends(get_bundle),
):
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
def item(item_id: str, b: Bundle = Depends(get_bundle)):
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
def currency_flags(b: Bundle = Depends(get_bundle)):
    """Amounts whose currency the system could NOT determine — user resolves these."""
    from .pipeline import currency as C
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
def resolve_currency(
    entity_id: str = Query(...),
    currency: str = Query(...),
    session_id: str | None = Cookie(None, alias="eventgraph_session"),
    b: Bundle = Depends(get_bundle),
):
    """User resolves a flagged amount by assigning its real currency.

    In DEMO_READONLY mode requires an active session (upload first).
    """
    from .pipeline import currency as C

    if DEMO_READONLY and not session_id:
        raise HTTPException(
            403,
            "The demo graph is read-only. Upload your own files first to create a session.",
        )

    if currency not in C.KNOWN:
        raise HTTPException(400, f"unknown currency {currency!r}; use one of {C.KNOWN}")
    e = b.graph.entity(entity_id)
    if not e or e.type.value != "money":
        raise HTTPException(404, "money entity not found")
    e.attrs.update({"currency": currency, "currency_resolved": True,
                    "currency_ambiguous": False, "needs_review": False, "resolved_by": "user"})
    e.label = C.label(e.attrs["amount"], e.attrs)
    for m in e.mentions:
        m.text = e.label

    if DEMO_READONLY:
        SESSION_STORE.put(session_id, b)  # type: ignore[arg-type]
    else:
        from . import store
        store.save_bundle(b)

    return {"entity_id": entity_id, "label": e.label, "currency": currency}


@app.get("/api/query/{query_id}")
def run_query(query_id: str, b: Bundle = Depends(get_bundle)):
    try:
        return Q.run(query_id, b)
    except KeyError:
        raise HTTPException(404, "unknown query")


@app.get("/api/ask")
def ask(
    request: Request,
    q: str = Query(..., min_length=2, max_length=_MAX_Q_LEN),
    b: Bundle = Depends(get_bundle),
):
    """Free-form natural-language question answered by the LLM over the graph.

    Rate-limited: 10 questions/minute per IP, 200/day globally.
    """
    from .llm.client import llm_available
    if not llm_available():
        return JSONResponse(
            status_code=503,
            content={"error": "llm_unavailable",
                     "message": "Free-form questions need an API key. The structured queries above still work."},
        )

    ip = _get_ip(request)
    allowed, reason = RATE_LIMITER.check(ip)
    if not allowed:
        return JSONResponse(status_code=429, content={"error": "rate_limited", "message": reason})

    RATE_LIMITER.record(ip)
    from .query_llm import query_graph
    return query_graph(q, b)


@app.get("/api/starter_questions")
def starter_questions_endpoint(b: Bundle = Depends(get_bundle)):
    return Q.starter_questions(b)


@app.get("/api/capabilities/models")
def model_info():
    return {
        "extraction_model": os.getenv("LLM_MODEL", "claude-sonnet-4-6"),
        "query_model": os.getenv("LLM_QUERY_MODEL", "claude-opus-4-8"),
    }
