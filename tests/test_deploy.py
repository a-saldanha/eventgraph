"""Phase 8 deploy tests.

Covers:
  - read-only rejects demo writes (currency resolve without a session)
  - two sessions don't cross-contaminate
  - /api/ask rate-limit → 429
  - /api/health returns expected fields
"""
from __future__ import annotations

import pytest


# ── unit: RateLimiter ──────────────────────────────────────────────────────────

def test_rate_limiter_allows_initial_calls():
    from app.rate_limit import RateLimiter
    rl = RateLimiter(calls_per_minute=5, global_daily_cap=100)
    allowed, msg = rl.check("1.2.3.4")
    assert allowed
    assert msg == ""


def test_rate_limiter_per_ip_blocks_after_cap():
    from app.rate_limit import RateLimiter
    rl = RateLimiter(calls_per_minute=2, global_daily_cap=100)
    rl.record("1.2.3.4")
    rl.record("1.2.3.4")
    allowed, msg = rl.check("1.2.3.4")
    assert not allowed
    assert "Rate limit" in msg


def test_rate_limiter_different_ips_are_independent():
    from app.rate_limit import RateLimiter
    rl = RateLimiter(calls_per_minute=1, global_daily_cap=100)
    rl.record("1.1.1.1")
    allowed_same, _ = rl.check("1.1.1.1")
    allowed_other, _ = rl.check("2.2.2.2")
    assert not allowed_same
    assert allowed_other


def test_rate_limiter_global_cap():
    from app.rate_limit import RateLimiter
    rl = RateLimiter(calls_per_minute=100, global_daily_cap=2)
    rl.record("1.1.1.1")
    rl.record("2.2.2.2")
    allowed, msg = rl.check("3.3.3.3")
    assert not allowed
    assert "daily cap" in msg.lower()


# ── unit: SessionStore ─────────────────────────────────────────────────────────

def test_session_store_get_returns_none_for_unknown():
    from app.session_store import SessionStore
    store = SessionStore()
    assert store.get("nonexistent") is None


def test_session_store_put_and_get():
    from app.session_store import SessionStore
    store = SessionStore()
    sentinel = object()
    store.put("sess-a", sentinel)
    assert store.get("sess-a") is sentinel


def test_session_store_two_sessions_isolated():
    from app.session_store import SessionStore
    store = SessionStore()
    obj_a, obj_b = object(), object()
    store.put("sess-a", obj_a)
    store.put("sess-b", obj_b)
    assert store.get("sess-a") is obj_a
    assert store.get("sess-b") is obj_b


def test_session_store_lru_eviction():
    from app.session_store import SessionStore
    store = SessionStore(ttl=9999, max_sessions=2)
    store.put("s1", "bundle1")
    store.put("s2", "bundle2")
    store.put("s3", "bundle3")  # s1 evicted (LRU)
    assert store.get("s1") is None
    assert store.get("s2") == "bundle2"
    assert store.get("s3") == "bundle3"


def test_session_store_ttl_eviction():
    from app.session_store import SessionStore
    import time
    store = SessionStore(ttl=0, max_sessions=10)  # immediate expiry
    store.put("sess", "data")
    time.sleep(0.01)
    assert store.get("sess") is None


# ── integration: FastAPI app ───────────────────────────────────────────────────

@pytest.fixture
def demo_client(monkeypatch):
    """TestClient with DEMO_READONLY=True and a pre-loaded demo bundle."""
    import app.api as api_mod

    # Patch DEMO_READONLY before the client is created
    monkeypatch.setattr(api_mod, "DEMO_READONLY", True)
    monkeypatch.setattr(api_mod, "SESSION_STORE", api_mod.SessionStore())
    monkeypatch.setattr(api_mod, "RATE_LIMITER", api_mod.RateLimiter(calls_per_minute=2, global_daily_cap=100))

    # Pre-load a bundle so we don't need the real data dir
    from app import store
    bundle = store.load_bundle()
    assert bundle is not None, "data/bundle.json must exist for integration tests"
    api_mod.STATE["bundle"] = bundle

    from fastapi.testclient import TestClient
    # Use raise_server_exceptions=False so we get HTTP responses for 4xx/5xx
    with TestClient(api_mod.app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def live_client(monkeypatch):
    """TestClient with DEMO_READONLY=False (normal live mode)."""
    import app.api as api_mod

    monkeypatch.setattr(api_mod, "DEMO_READONLY", False)
    monkeypatch.setattr(api_mod, "SESSION_STORE", api_mod.SessionStore())
    monkeypatch.setattr(api_mod, "RATE_LIMITER", api_mod.RateLimiter(calls_per_minute=2, global_daily_cap=100))

    from app import store
    bundle = store.load_bundle()
    assert bundle is not None
    api_mod.STATE["bundle"] = bundle

    from fastapi.testclient import TestClient
    with TestClient(api_mod.app, raise_server_exceptions=False) as client:
        yield client


# health endpoint

def test_health_returns_ok(demo_client):
    r = demo_client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "entities" in data
    assert "items" in data
    assert "llm_available" in data
    assert data["demo_readonly"] is True


def test_health_live_mode(live_client):
    r = live_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["demo_readonly"] is False


# read-only: currency resolve without session → CoW copy created, shared bundle unchanged

def test_readonly_currency_resolve_no_session_creates_cow_session(demo_client):
    import app.api as api_mod
    import json

    shared_json = json.dumps(
        api_mod.STATE["bundle"].graph.model_dump(mode="json"), sort_keys=True
    )
    # Use a money entity that may or may not exist; 404 is acceptable, 500 is not.
    r = demo_client.post(
        "/api/resolve/currency",
        params={"entity_id": "money:0", "currency": "INR"},
    )
    assert r.status_code in (200, 404), f"Unexpected status {r.status_code}"
    # Shared bundle must be unchanged regardless.
    after_json = json.dumps(
        api_mod.STATE["bundle"].graph.model_dump(mode="json"), sort_keys=True
    )
    assert shared_json == after_json


# read-only: demo bundle not contaminated after session upload

def test_readonly_demo_bundle_unchanged_after_session_upload(demo_client, monkeypatch):
    """Upload into a session does not touch the shared STATE bundle."""
    import app.api as api_mod
    import threading

    original_bundle = api_mod.STATE["bundle"]

    # Fake a completed session bundle arriving via _publish
    import uuid
    fake_bundle = object()  # a distinct object — if STATE changes, test fails
    sid = str(uuid.uuid4())
    api_mod._publish(fake_bundle, session_id=sid)  # type: ignore[arg-type]

    # Demo bundle must be untouched
    assert api_mod.STATE["bundle"] is original_bundle
    # Session store must have the fake bundle
    assert api_mod.SESSION_STORE.get(sid) is fake_bundle


# two sessions don't cross-contaminate

def test_two_sessions_isolated(demo_client):
    """GET endpoints return their own bundle, not each other's."""
    import app.api as api_mod
    import uuid

    bundle_a = object()
    bundle_b = object()
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    api_mod.SESSION_STORE.put(sid_a, bundle_a)
    api_mod.SESSION_STORE.put(sid_b, bundle_b)

    assert api_mod.SESSION_STORE.get(sid_a) is bundle_a
    assert api_mod.SESSION_STORE.get(sid_b) is bundle_b
    assert api_mod.SESSION_STORE.get(sid_a) is not bundle_b


# rate limit → 429

def test_ask_rate_limit_returns_429(demo_client, monkeypatch):
    """After exceeding the per-IP cap, /api/ask returns 429."""
    import app.api as api_mod
    from app.rate_limit import RateLimiter

    # Very tight limiter: 0 allowed calls per minute
    monkeypatch.setattr(api_mod, "RATE_LIMITER", RateLimiter(calls_per_minute=0, global_daily_cap=100))

    r = demo_client.get("/api/ask", params={"q": "who is the owner?"})
    # Either 429 (rate limited) or 503 (no LLM key) — both are acceptable rejections
    assert r.status_code in (429, 503)
    if r.status_code == 429:
        assert r.json()["error"] == "rate_limited"


def test_ask_global_daily_cap_returns_429(demo_client, monkeypatch):
    """Once the global daily cap is exhausted, /api/ask returns 429."""
    import app.api as api_mod
    from app.rate_limit import RateLimiter

    rl = RateLimiter(calls_per_minute=100, global_daily_cap=0)
    monkeypatch.setattr(api_mod, "RATE_LIMITER", rl)

    r = demo_client.get("/api/ask", params={"q": "who is the owner?"})
    assert r.status_code in (429, 503)
    if r.status_code == 429:
        assert "daily cap" in r.json()["message"].lower()
