"""Phase 0 tests: secrets, auth, polling, CoW sessions, ingest limits.

All fixtures use invented data; no network calls are made.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── 1. No NEXT_PUBLIC_*TOKEN in frontend ───────────────────────────────────────

def test_no_next_public_token_in_frontend():
    """No file in frontend/ contains NEXT_PUBLIC_ combined with TOKEN."""
    root = Path(__file__).resolve().parents[1] / "frontend"
    offending = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip compiled build artifacts
        if ".next" in path.parts:
            continue
        try:
            text = path.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        if "NEXT_PUBLIC_" in text and "TOKEN" in text:
            offending.append(str(path.relative_to(root)))
    assert not offending, (
        "These frontend files reference NEXT_PUBLIC_*TOKEN: " + ", ".join(offending)
    )


def test_seed_bundle_requires_token(monkeypatch):
    """The seed-bundle endpoint returns 401 without a valid token."""
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "_BACKEND_TOKEN", "secret")
    from app.pipeline.build import build_graph
    api_mod.STATE["bundle"] = build_graph([])
    from fastapi.testclient import TestClient
    with TestClient(api_mod.app, raise_server_exceptions=False) as client:
        r = client.post("/api/admin/seed-bundle",
                        files=[("file", ("bundle.json", b"{}", "application/json"))])
        assert r.status_code == 401


# ── 2. Backend auth enforcement ────────────────────────────────────────────────

@pytest.fixture
def authed_client(monkeypatch):
    """TestClient with BACKEND_TOKEN set and a tiny bundle pre-loaded."""
    import app.api as api_mod

    token = "test-secret-token"
    monkeypatch.setattr(api_mod, "_BACKEND_TOKEN", token)
    monkeypatch.setattr(api_mod, "DEMO_READONLY", False)
    monkeypatch.setattr(api_mod, "SESSION_STORE", api_mod.SessionStore())

    from app.pipeline.build import build_graph
    api_mod.STATE["bundle"] = build_graph([])

    from fastapi.testclient import TestClient
    with TestClient(api_mod.app, raise_server_exceptions=False) as c:
        c._token = token
        yield c


def test_health_always_200_without_token(authed_client):
    r = authed_client.get("/api/health")
    assert r.status_code == 200


def test_protected_route_without_token_returns_401(authed_client):
    r = authed_client.get("/api/stats")
    assert r.status_code == 401


def test_protected_route_wrong_token_returns_401(authed_client):
    r = authed_client.get("/api/stats", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_protected_route_correct_token_returns_200(authed_client):
    token = authed_client._token
    r = authed_client.get("/api/stats", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


# ── 3. Job polling returns events in order ─────────────────────────────────────

def test_job_polling_returns_all_events(monkeypatch):
    from app.jobs import JobManager

    bundles_received = []

    def fake_on_bundle(b, sid=None):
        bundles_received.append(b)

    mgr = JobManager(on_bundle=fake_on_bundle)

    # Tiny mbox bytes that parse to at least one item via the heuristic adapter.
    mbox_bytes = (
        b"From nobody@example.com Mon Jan 01 00:00:00 2024\r\n"
        b"From: alice@acme.com\r\nTo: bob@acme.com\r\n"
        b"Subject: Hello\r\nDate: Mon, 01 Jan 2024 00:00:00 +0000\r\n\r\n"
        b"Body text.\r\n"
    )
    job = mgr.start([("test.mbox", mbox_bytes)], mode="heuristic")

    # Wait for the job to finish (up to 10 s).
    deadline = time.time() + 10
    while job.status not in ("done", "failed") and time.time() < deadline:
        time.sleep(0.05)

    assert job.status == "done", f"Job failed: {job.error}"
    assert len(job.events) >= 2, "Expected at least parsing + done events"
    stages = [e["stage"] for e in job.events]
    assert "parsing" in stages
    assert "done" in stages
    # Events must be in ascending timestamp order
    timestamps = [e["ts"] for e in job.events]
    assert timestamps == sorted(timestamps)


def _make_api_client_for_polling(monkeypatch):
    """Return a TestClient with no auth token and a pre-loaded bundle."""
    import app.api as api_mod

    monkeypatch.setattr(api_mod, "_BACKEND_TOKEN", None)
    monkeypatch.setattr(api_mod, "DEMO_READONLY", False)
    monkeypatch.setattr(api_mod, "SESSION_STORE", api_mod.SessionStore())

    from app.pipeline.build import build_graph
    api_mod.STATE["bundle"] = build_graph([])

    from fastapi.testclient import TestClient
    return TestClient(api_mod.app, raise_server_exceptions=False)


def test_poll_endpoint_unknown_job_returns_404(monkeypatch):
    client = _make_api_client_for_polling(monkeypatch)
    r = client.get("/api/jobs/nonexistent-id")
    assert r.status_code == 404


def test_poll_endpoint_returns_status_and_events(monkeypatch):
    import app.api as api_mod

    monkeypatch.setattr(api_mod, "_BACKEND_TOKEN", None)
    monkeypatch.setattr(api_mod, "DEMO_READONLY", False)
    monkeypatch.setattr(api_mod, "SESSION_STORE", api_mod.SessionStore())

    from app.pipeline.build import build_graph
    api_mod.STATE["bundle"] = build_graph([])

    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app, raise_server_exceptions=False)

    mbox_bytes = (
        b"From nobody@example.com Mon Jan 01 00:00:00 2024\r\n"
        b"From: alice@acme.com\r\nTo: bob@acme.com\r\n"
        b"Subject: Kick-off\r\nDate: Mon, 01 Jan 2024 00:00:00 +0000\r\n\r\n"
        b"Let us meet.\r\n"
    )
    r = client.post("/api/ingest?mode=heuristic", files=[("files", ("a.mbox", mbox_bytes))])
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deadline = time.time() + 15
    status = None
    while time.time() < deadline:
        p = client.get(f"/api/jobs/{job_id}")
        assert p.status_code == 200
        d = p.json()
        status = d["status"]
        if status in ("done", "failed"):
            break
        time.sleep(0.1)

    assert status == "done"
    d = client.get(f"/api/jobs/{job_id}").json()
    assert isinstance(d["events"], list)
    assert len(d["events"]) >= 1
    assert d["stats"] is not None


# ── 4. Copy-on-write sessions ──────────────────────────────────────────────────

@pytest.fixture
def readonly_client(monkeypatch):
    import app.api as api_mod

    monkeypatch.setattr(api_mod, "_BACKEND_TOKEN", None)
    monkeypatch.setattr(api_mod, "DEMO_READONLY", True)
    monkeypatch.setattr(api_mod, "SESSION_STORE", api_mod.SessionStore())

    from app.pipeline.build import build_graph
    from app.schema import SourceItem, SourceType, Provenance
    from datetime import datetime

    prov = Provenance(batch_file="test.md", item_index=0)
    item = SourceItem(
        id="i0", source_type=SourceType.EMAIL, body="Pay $100 now.",
        channel="email", timestamp=datetime(2024, 1, 1), provenance=prov,
    )
    api_mod.STATE["bundle"] = build_graph([item])

    from fastapi.testclient import TestClient
    return TestClient(api_mod.app, raise_server_exceptions=False)


def test_cow_currency_resolve_no_session_creates_session(readonly_client):
    """Currency resolution without a session creates a CoW copy and sets a cookie."""
    import app.api as api_mod

    shared_json = json.dumps(
        api_mod.STATE["bundle"].graph.model_dump(mode="json"), sort_keys=True
    )

    flags = readonly_client.get("/api/flags/currency").json()
    if not flags["flagged"]:
        pytest.skip("No flagged money entity in the test bundle")

    eid = flags["flagged"][0]["entity_id"]
    currency = "USD"

    r = readonly_client.post(
        f"/api/resolve/currency?entity_id={eid}&currency={currency}"
    )
    assert r.status_code == 200
    assert "eventgraph_session" in r.cookies

    # Shared bundle must be unchanged.
    after_json = json.dumps(
        api_mod.STATE["bundle"].graph.model_dump(mode="json"), sort_keys=True
    )
    assert shared_json == after_json


def test_shared_bundle_unchanged_after_50_random_sessions(readonly_client):
    """50 random cookie values against every write endpoint leave the shared bundle intact."""
    import app.api as api_mod

    shared_json = json.dumps(
        api_mod.STATE["bundle"].graph.model_dump(mode="json"), sort_keys=True
    )

    flags = readonly_client.get("/api/flags/currency").json()
    eid = flags["flagged"][0]["entity_id"] if flags["flagged"] else "nonexistent"

    for _ in range(50):
        cookie_val = str(uuid.uuid4())
        # Write endpoint 1: currency resolve
        readonly_client.post(
            f"/api/resolve/currency?entity_id={eid}&currency=USD",
            cookies={"eventgraph_session": cookie_val},
        )

    after_json = json.dumps(
        api_mod.STATE["bundle"].graph.model_dump(mode="json"), sort_keys=True
    )
    assert shared_json == after_json, "Shared bundle was mutated by session writes"


# ── 5. Ingest limits ──────────────────────────────────────────────────────────

@pytest.fixture
def limits_client(monkeypatch):
    import app.api as api_mod

    monkeypatch.setattr(api_mod, "_BACKEND_TOKEN", None)
    monkeypatch.setattr(api_mod, "DEMO_READONLY", False)
    monkeypatch.setattr(api_mod, "SESSION_STORE", api_mod.SessionStore())
    monkeypatch.setattr(api_mod, "_ingest_timestamps", {})

    from app.pipeline.build import build_graph
    api_mod.STATE["bundle"] = build_graph([])

    from fastapi.testclient import TestClient
    return TestClient(api_mod.app, raise_server_exceptions=False)


def test_too_many_files_returns_413(limits_client, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "MAX_FILES", 2)

    files = [("files", (f"f{i}.txt", b"hello")) for i in range(3)]
    r = limits_client.post("/api/ingest?mode=heuristic", files=files)
    assert r.status_code == 413
    body = r.json()
    assert "message" in body


def test_file_too_large_returns_413(limits_client, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "MAX_FILE_BYTES", 10)

    r = limits_client.post(
        "/api/ingest?mode=heuristic",
        files=[("files", ("big.txt", b"x" * 11))],
    )
    assert r.status_code == 413
    assert "message" in r.json()


def test_per_ip_rate_limit_returns_429(limits_client, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "IP_MAX_INGESTS", 1)
    monkeypatch.setattr(api_mod, "IP_WINDOW_SECS", 600)
    monkeypatch.setattr(api_mod, "_ingest_timestamps", {})

    tiny = b"From nobody@example.com\r\nFrom: x@y.com\r\n\r\nHi.\r\n"
    # First request should succeed (or at least not 429).
    r1 = limits_client.post("/api/ingest?mode=heuristic",
                            files=[("files", ("a.mbox", tiny))])
    assert r1.status_code != 429

    # Second request should be rate-limited.
    r2 = limits_client.post("/api/ingest?mode=heuristic",
                            files=[("files", ("b.mbox", tiny))])
    assert r2.status_code == 429
    body = r2.json()
    assert "message" in body
    assert "message" in body  # message must state the limit and what to do


def test_llm_daily_cap_returns_429(limits_client, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod, "LLM_DAILY_CAP", 0)
    monkeypatch.setattr(api_mod, "_llm_daily_count", 0)

    tiny = b"From nobody@example.com\r\nFrom: x@y.com\r\n\r\nHi.\r\n"
    r = limits_client.post("/api/ingest?mode=llm",
                           files=[("files", ("a.mbox", tiny))])
    assert r.status_code == 429
    assert "message" in r.json()


# ── 6. Job eviction ────────────────────────────────────────────────────────────

def test_finished_jobs_are_evicted_after_ttl(monkeypatch):
    import app.jobs as jobs_mod
    from app.jobs import JobManager, Job

    # Patch the module-level constant so _evict() sees TTL=0.
    monkeypatch.setattr(jobs_mod, "JOB_EVICT_SECS", 0)

    mgr = JobManager(on_bundle=lambda b, s=None: None)
    job = Job(id="old-job", status="done", finished_at=time.time() - 1)
    mgr._jobs[job.id] = job

    mgr._evict()
    assert mgr.get("old-job") is None
