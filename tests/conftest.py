"""Shared pytest fixtures — run before every test module to ensure isolation."""
import pytest


@pytest.fixture(autouse=True)
def reset_api_module_state():
    """Reset all module-level mutable state in app.api between tests.

    Without this, rate-limiter counters and ingest-timestamp dicts bleed across
    tests that share the same process, making the suite order-dependent.
    """
    import app.api as mod

    # Snapshot the original objects so we can restore them.
    orig_ingest_ts = mod._ingest_timestamps
    orig_llm_daily = mod._llm_daily_count
    orig_llm_date = mod._llm_daily_date
    orig_llm_session = mod._llm_session_last

    yield

    # Restore / clear after each test.
    mod._ingest_timestamps = {}
    mod._llm_daily_count = 0
    mod._llm_daily_date = ""
    mod._llm_session_last = {}
