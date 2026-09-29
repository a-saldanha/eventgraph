"""FastAPI app for the EventGraph demo.

DEMO_READONLY=1 (env): a committed or volume-mounted bundle.json is served read-only.
Writes (currency resolution, review decisions) work on a per-visitor copy-on-write
session — never the shared bundle.

Normal mode: uploads replace the live bundle and are persisted to .cache/.
"""
from __future__ import annotations

import copy
import hmac
import os
import time
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
from .limits import (
    MAX_FILES, MAX_FILE_BYTES, MAX_REQUEST_BYTES,
    IP_MAX_INGESTS, IP_WINDOW_SECS,
    LLM_MAX_PER_SESSION_HOUR, LLM_DAILY_CAP,
)

DATA_DIR = Path(os.getenv("DATA_DIR", "")).resolve() if os.getenv("DATA_DIR") else \
    Path(__file__).resolve().parents[1] / "data"

DEMO_READONLY: bool = os.getenv("DEMO_READONLY", "").lower() in ("1", "true", "yes")

# Set BACKEND_TOKEN=<secret> in env to require auth on every route except /api/health.
# Leave unset (or empty) for local dev — auth gate is disabled.
_BACKEND_TOKEN: str | None = os.getenv("BACKEND_TOKEN") or None

STATE: dict[str, Bundle] = {}
# Which source was actually loaded at startup — set by the lifespan.
_BUNDLE_SOURCE: str = "empty"

SESSION_STORE = SessionStore()
RATE_LIMITER = RateLimiter()

# Sliding-window per-IP ingest rate tracker: ip -> list of timestamps
_ingest_timestamps: dict[str, list[float]] = {}

# LLM-build counters: global daily count and per-session last-build time
_llm_daily_count: int = 0
_llm_daily_date: str = ""
_llm_session_last: dict[str, float] = {}

_raw_origins = os.getenv("CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] or ["*"]
_cors_credentials = "*" not in _cors_origins


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _BUNDLE_SOURCE
    from . import store
    import logging
    log = logging.getLogger(__name__)

    saved = store.load_bundle()
    if saved is not None:
        STATE["bundle"] = saved
        _BUNDLE_SOURCE = store.LAST_SOURCE
    else:
        items = []
        for f in sorted(DATA_DIR.glob("batch*.md")):
            items += parse_batch_file(f)
        if not items:
            log.warning(
                "No bundle and no corpus found — starting with an empty graph. "
                "Place bundle.json in DATA_DIR or mount a Railway volume."
            )
        STATE["bundle"] = build_graph(items)
        _BUNDLE_SOURCE = "rebuilt" if items else "empty"
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
    """Require Bearer BACKEND_TOKEN on all /api/* routes except /api/health.

    Disabled when BACKEND_TOKEN is not set (local dev default).
    CORS preflight (OPTIONS) passes through unconditionally.
    """
    if (
        _BACKEND_TOKEN
        and request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/health"
    ):
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {_BACKEND_TOKEN}"
        if not hmac.compare_digest(auth, expected):
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
    """Return the session bundle when present, otherwise the shared bundle."""
    if DEMO_READONLY and session_id:
        sess = SESSION_STORE.get(session_id)
        if sess is not None:
            return sess  # type: ignore[return-value]
    return STATE["bundle"]


def _publish(bundle: Bundle, session_id: str | None = None) -> None:
    """Publish a freshly built bundle.

    DEMO_READONLY: store under the session. Never touch STATE or disk.
    Normal mode: replace the live bundle and persist to .cache/.
    """
    if DEMO_READONLY:
        if session_id:
            SESSION_STORE.put(session_id, bundle)
    else:
        STATE["bundle"] = bundle
        from . import store
        store.save_bundle(bundle)


def _cow_session(session_id: str | None) -> tuple[Bundle, str]:
    """Return (session_bundle, session_id), creating a CoW copy if needed.

    In DEMO_READONLY mode any write that doesn't have an active session gets
    its own deep copy of the shared bundle, so visitors can resolve currencies
    or accept merges without uploading first.
    """
    if session_id:
        sess = SESSION_STORE.get(session_id)
        if sess is not None:
            return sess, session_id  # type: ignore[return-value]
    # No active session — create one from the shared bundle.
    sid = session_id or str(uuid.uuid4())
    bundle_copy = copy.deepcopy(STATE["bundle"])
    SESSION_STORE.put(sid, bundle_copy)
    return bundle_copy, sid


def _check_ingest_rate(ip: str) -> tuple[bool, str]:
    now = time.time()
    window = now - IP_WINDOW_SECS
    times = [t for t in _ingest_timestamps.get(ip, []) if t > window]
    _ingest_timestamps[ip] = times
    if len(times) >= IP_MAX_INGESTS:
        return False, (
            f"Too many uploads from this IP ({IP_MAX_INGESTS} per "
            f"{IP_WINDOW_SECS // 60} minutes). Try again later."
        )
    return True, ""


def _record_ingest(ip: str) -> None:
    _ingest_timestamps.setdefault(ip, []).append(time.time())


def _check_llm_quota(session_id: str | None) -> tuple[bool, str]:
    global _llm_daily_count, _llm_daily_date
    import datetime
    today = datetime.date.today().isoformat()
    if _llm_daily_date != today:
        _llm_daily_count = 0
        _llm_daily_date = today
    if _llm_daily_count >= LLM_DAILY_CAP:
        return False, (
            f"Daily LLM-build cap ({LLM_DAILY_CAP}) reached. "
            "Use Instant mode instead."
        )
    if session_id:
        last = _llm_session_last.get(session_id, 0)
        if time.time() - last < 3600 / LLM_MAX_PER_SESSION_HOUR:
            return False, (
                "LLM mode is limited to 1 build per hour per session. "
                "Use Instant mode or wait."
            )
    return True, ""


def _record_llm_build(session_id: str | None) -> None:
    global _llm_daily_count
    _llm_daily_count += 1
    if session_id:
        _llm_session_last[session_id] = time.time()


JOBS = JobManager(on_bundle=_publish)


# ── endpoints ──────────────────────────────────────────────────────────────────


@app.post("/api/replay")
async def replay(
    request: Request,
    mode: str = Query("heuristic"),
    session_id: str | None = Cookie(None, alias="eventgraph_session"),
):
    """Run the full pipeline on $DATA_DIR/raw/ files as a session-scoped job.

    Model responses are cached in $DATA_DIR/cache/llm — this is fast and free
    as long as the source files haven't changed since the last build.
    """
    raw_dir = DATA_DIR / "raw"
    if not raw_dir.is_dir():
        return JSONResponse(
            status_code=400,
            content={"error": "no_raw_dir",
                     "message": "No raw/ directory on this server. Contact the site owner."},
        )

    native_exts = {".mbox", ".eml", ".txt", ".csv", ".tsv", ".xlsx", ".pdf"}
    files: list[tuple[str, bytes]] = []
    for f in sorted(raw_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in native_exts:
            files.append((f.name, f.read_bytes()))

    if not files:
        return JSONResponse(
            status_code=400,
            content={"error": "no_files",
                     "message": "No source files found in DATA_DIR/raw/."},
        )

    if not session_id:
        session_id = str(uuid.uuid4())

    job = JOBS.start(files, mode=mode, session_id=session_id)
    resp = JSONResponse({"job_id": job.id, "n_files": len(files), "source": "replay"})
    resp.set_cookie(
        "eventgraph_session", session_id,
        max_age=1800, samesite="lax", httponly=True,
    )
    return resp


@app.post("/api/session/reset")
async def session_reset():
    """Clear the session cookie so the shared bundle is served again."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(
        "eventgraph_session",
        path="/",
        samesite="lax",
        httponly=True,
    )
    return resp


@app.get("/api/health")
def health():
    from .llm.client import llm_available
    from . import store as _store
    b = STATE.get("bundle")
    return {
        "status": "ok",
        "demo_readonly": DEMO_READONLY,
        "bundle_source": _BUNDLE_SOURCE,
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
    """Bulk upload → async pipeline. Returns a job_id to poll."""
    ip = _get_ip(request)

    # Per-IP rate limit
    allowed, reason = _check_ingest_rate(ip)
    if not allowed:
        return JSONResponse(status_code=429, content={"error": "rate_limited", "message": reason})

    # File count limit
    if len(files) > MAX_FILES:
        return JSONResponse(
            status_code=413,
            content={"error": "too_many_files",
                     "message": f"Upload at most {MAX_FILES} files per request."},
        )

    # Read and validate each file
    payload: list[tuple[str, bytes]] = []
    total_bytes = 0
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": "file_too_large",
                         "message": f"{f.filename!r} exceeds the {MAX_FILE_BYTES // (1024*1024)} MB per-file limit."},
            )
        total_bytes += len(data)
        if total_bytes > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": "request_too_large",
                         "message": f"Total upload exceeds {MAX_REQUEST_BYTES // (1024*1024)} MB. Split across requests."},
            )
        payload.append((f.filename or "upload", data))

    # LLM-mode quota
    if mode == "llm":
        ok, reason = _check_llm_quota(session_id)
        if not ok:
            return JSONResponse(status_code=429, content={"error": "llm_quota", "message": reason})

    _record_ingest(ip)
    if mode == "llm":
        _record_llm_build(session_id)

    if DEMO_READONLY:
        if not session_id:
            session_id = str(uuid.uuid4())
        job = JOBS.start(payload, mode=mode, session_id=session_id)
        resp = JSONResponse({"job_id": job.id, "n_files": len(payload)})
        resp.set_cookie(
            "eventgraph_session", session_id,
            max_age=1800, samesite="lax", httponly=True,
        )
        return resp

    job = JOBS.start(payload, mode=mode)
    return {"job_id": job.id, "n_files": len(payload)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    """Poll a job's current state and full event history."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {
        "status": job.status,
        "events": job.events,
        "stats": job.result_stats,
        "error": job.error,
    }


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    """SSE stream — kept for local dev. UI uses /api/jobs/{id} polling instead."""
    return EventSourceResponse(JOBS.stream(job_id))


@app.get("/api/capabilities")
def capabilities():
    from .llm.client import llm_available
    return {"llm_available": llm_available(), "demo_readonly": DEMO_READONLY}


@app.get("/api/capabilities/models")
def model_info():
    return {
        "extract_model": os.getenv("LLM_EXTRACT_MODEL", "claude-haiku-4-5"),
        "resolve_model": os.getenv("LLM_RESOLVE_MODEL", "claude-sonnet-4-6"),
        "query_model": os.getenv("LLM_QUERY_MODEL", "claude-sonnet-4-6"),
    }


@app.get("/api/stats")
def stats(b: Bundle = Depends(get_bundle)):
    return {**b.stats, "queries": Q.list_queries()}


@app.get("/api/graph")
def graph(min_degree: int = 1, b: Bundle = Depends(get_bundle)):
    deg: dict[str, int] = {}
    for e in b.graph.edges:
        deg[e.source] = deg.get(e.source, 0) + 1
        deg[e.target] = deg.get(e.target, 0) + 1
    keep = {
        e.id for e in b.graph.entities
        if deg.get(e.id, 0) >= min_degree
        or e.type.value in ("subevent", "org", "location", "money", "document")
    }
    nodes = [
        {"id": e.id, "label": e.label, "type": e.type.value,
         "mentions": len(e.mentions), "degree": deg.get(e.id, 0),
         "emails": e.attrs.get("emails", []),
         "currency": e.attrs.get("currency"),
         "needs_review": e.attrs.get("needs_review", False)}
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
        "subject": it.subject, "body": it.body,
        "relevance": rel.model_dump() if rel else None,
        "entities": ents,
    }


@app.get("/api/flags/currency")
def currency_flags(b: Bundle = Depends(get_bundle)):
    """Amounts whose currency the system could not determine — user resolves these."""
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
                    "reason": "mixed-currency" if e.attrs.get("currency_ambiguous")
                    else "no-currency-marker",
                    "evidence": e.attrs.get("currency_evidence", []), "sources": srcs})
    out.sort(key=lambda x: -(x["amount"] or 0))
    return {"currencies": C.KNOWN, "flagged": out}


@app.post("/api/resolve/currency")
def resolve_currency(
    request: Request,
    entity_id: str = Query(...),
    currency: str = Query(...),
    session_id: str | None = Cookie(None, alias="eventgraph_session"),
):
    """Assign a currency to a flagged amount.

    In DEMO_READONLY mode, the write goes to a per-visitor copy of the bundle.
    Visitors without a session get one automatically — no upload required.
    """
    from .pipeline import currency as C

    if currency not in C.KNOWN:
        raise HTTPException(400, f"unknown currency {currency!r}; use one of {C.KNOWN}")

    if DEMO_READONLY:
        b, sid = _cow_session(session_id)
    else:
        b = STATE["bundle"]
        sid = None

    e = b.graph.entity(entity_id)
    if not e or e.type.value != "money":
        raise HTTPException(404, "money entity not found")

    e.attrs.update({"currency": currency, "currency_resolved": True,
                    "currency_ambiguous": False, "needs_review": False, "resolved_by": "user"})
    e.label = C.label(e.attrs["amount"], e.attrs)
    for m in e.mentions:
        m.text = e.label

    if DEMO_READONLY:
        SESSION_STORE.put(sid, b)
    else:
        from . import store
        store.save_bundle(b)

    resp = JSONResponse({"entity_id": entity_id, "label": e.label, "currency": currency})
    if DEMO_READONLY and sid != session_id:
        is_secure = request.url.scheme == "https"
        resp.set_cookie(
            "eventgraph_session", sid,
            max_age=1800, samesite="lax", httponly=True, secure=is_secure,
        )
    return resp


@app.get("/api/query/{query_id}")
def run_query(query_id: str, b: Bundle = Depends(get_bundle)):
    try:
        return Q.run(query_id, b)
    except KeyError:
        raise HTTPException(404, "unknown query")


_MAX_Q_LEN = 500


@app.get("/api/ask")
def ask(
    request: Request,
    q: str = Query(..., min_length=2, max_length=_MAX_Q_LEN),
    b: Bundle = Depends(get_bundle),
):
    """Free-form question answered by the LLM over the graph.

    Rate-limited: per-IP and global daily cap.
    """
    from .llm.client import llm_available
    if not llm_available():
        return JSONResponse(
            status_code=503,
            content={"error": "llm_unavailable",
                     "message": "Free-form questions need an API key. "
                     "The structured queries still work."},
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
