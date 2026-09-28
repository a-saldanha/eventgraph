"""Phase 3: LLM extraction + verification tests.

FakeLLM is keyed by sha256 of the user input so the same prompt always returns
the same response — no network, fully deterministic, cache replays identically.

Tests:
  1. Hallucinated surface (not in source text) is rejected by verify().
  2. Unknown message id is rejected by verify().
  3. Cache replays byte-identically (second call hits cache, same result).
  4. 400 client error fails fast without retry.
  5. 429 rate limit is retried.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.llm.client import LLMClientError, LLMRateLimitError
from app.llm.chunking import Chunk, chunk_items
from app.llm.extract import _chunk_cache_key, _extract_chunk, extract_chunks
from app.llm.verify import verify
from app.schema import Participant, Provenance, SourceItem, SourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(iid: str, body: str, conv: str = "c1", sender: str = "alice@example.com",
          ts: datetime | None = None) -> SourceItem:
    return SourceItem(
        id=iid,
        source_type=SourceType.WHATSAPP,
        conversation_id=conv,
        body=body,
        content_hash=iid,
        timestamp=ts or datetime(2025, 1, 1, 12, 0, 0),
        participants=[
            Participant(display_name="Alice", id_type="email", id_value=sender,
                        raw=sender, role="sender", kind="person"),
            Participant(display_name="Bob", id_type="email", id_value="bob@example.com",
                        raw="bob@example.com", role="recipient", kind="person"),
        ],
        provenance=Provenance(batch_file="test", item_index=1),
    )


def _make_chunk(items: list[SourceItem]) -> Chunk:
    """Build one chunk from the given items."""
    chunks = chunk_items(items)
    assert chunks, "chunk_items returned no chunks"
    return chunks[0]


class FakeLLM:
    """Deterministic fake: response is keyed by sha256(user_content).

    Build a mapping of (content_sha -> response_dict) in the test; the fake
    returns the matching response or an empty extraction.
    """

    def __init__(self, responses: dict[str, dict] | None = None):
        self.responses = responses or {}
        self.call_count = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        self.call_count += 1
        k = self._key(user)
        return json.dumps(self.responses.get(k, {"messages": [], "mentions": [], "relations": []}))

    def complete_tool(self, system: str, user: str, tool: dict, max_tokens: int = 4096) -> str:
        return self.complete_json(system, user, max_tokens)


# ---------------------------------------------------------------------------
# Test 1: Hallucinated surface is rejected
# ---------------------------------------------------------------------------

def test_hallucinated_surface_rejected(tmp_path):
    """Surface that does not appear verbatim in the message must be dropped."""
    items = [_item("i1", "Hello from Acme Corp today")]
    chunk = _make_chunk(items)

    # The first message local id.
    mid = list(chunk.id_map.keys())[0]

    # Response claims a surface "Globocorp" which is NOT in the body.
    fake_response = {
        "messages": [{"id": mid, "topics": ["greeting"], "reason": "hello"}],
        "mentions": [
            {
                "message_id": mid,
                "surface": "Globocorp",   # hallucinated — not in text
                "type": "org",
                "local_entity": "globocorp",
            }
        ],
        "relations": [],
    }

    result = verify(chunk, fake_response)
    assert result.rejected["surface_not_found"] == 1
    assert len(result.mentions) == 0


# ---------------------------------------------------------------------------
# Test 2: Unknown message id is rejected
# ---------------------------------------------------------------------------

def test_unknown_message_id_rejected(tmp_path):
    """A mention referencing a message id not in the chunk must be dropped."""
    items = [_item("i1", "Invoice from Acme Corp for 100 USD")]
    chunk = _make_chunk(items)

    fake_response = {
        "messages": [],
        "mentions": [
            {
                "message_id": "m999",   # does not exist in this chunk
                "surface": "Acme Corp",
                "type": "org",
                "local_entity": "acme",
            }
        ],
        "relations": [],
    }

    result = verify(chunk, fake_response)
    assert result.rejected["bad_message_id"] >= 1
    assert len(result.mentions) == 0


# ---------------------------------------------------------------------------
# Test 3: Cache replays identically
# ---------------------------------------------------------------------------

def test_cache_replays_identically(tmp_path):
    """The second extraction call must hit the cache and return the same result."""
    items = [_item("i1", "Meeting at Acme HQ on Monday")]
    chunk = _make_chunk(items)
    mid = list(chunk.id_map.keys())[0]

    # A valid response: surface "Acme HQ" appears in the message text.
    response_dict = {
        "messages": [{"id": mid, "topics": ["meeting"], "reason": "meeting mention"}],
        "mentions": [
            {
                "message_id": mid,
                "surface": "Acme HQ",
                "type": "org",
                "local_entity": "acme_hq",
            }
        ],
        "relations": [],
    }

    fake = FakeLLM({hashlib.sha256(chunk.full_text.encode()).hexdigest(): response_dict})

    # Patch CACHE_DIR to use tmp_path.
    import app.llm.extract as ext
    with patch.object(ext, "CACHE_DIR", tmp_path):
        result1 = _extract_chunk(chunk, fake, model="mock-model", use_cache=True)
        call_count_after_first = fake.call_count

        result2 = _extract_chunk(chunk, fake, model="mock-model", use_cache=True)
        call_count_after_second = fake.call_count

    # Only one real LLM call; second hits cache.
    assert call_count_after_first == 1
    assert call_count_after_second == 1  # no additional call

    # Results must be identical.
    assert len(result1.mentions) == len(result2.mentions)
    assert result1.from_cache is False
    assert result2.from_cache is True
    if result1.mentions:
        assert result1.mentions[0].surface == result2.mentions[0].surface


# ---------------------------------------------------------------------------
# Test 4: 400 fails fast (no retry)
# ---------------------------------------------------------------------------

def test_400_fails_fast():
    """A 400 error must raise LLMClientError immediately, without retrying."""
    import anthropic
    from app.llm.client import AnthropicLLM

    call_count = [0]

    def mock_create(**kwargs):
        call_count[0] += 1
        err = anthropic.APIStatusError(
            "Bad request",
            response=MagicMock(status_code=400),
            body={"error": {"type": "invalid_request_error"}},
        )
        err.status_code = 400
        raise err

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = mock_create
        MockAnthropic.return_value = mock_client

        llm = AnthropicLLM("sk-ant-fake-key", "claude-haiku-4-5", max_retries=4)

    with pytest.raises(LLMClientError):
        llm.complete_json("system", "user")

    # Only one call — no retries on 400.
    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Test 5: 429 is retried
# ---------------------------------------------------------------------------

def test_429_is_retried():
    """A 429 rate-limit error must be retried (up to max_retries) then raise."""
    import anthropic
    from app.llm.client import AnthropicLLM

    call_count = [0]

    def mock_create(**kwargs):
        call_count[0] += 1
        raise anthropic.RateLimitError(
            "Rate limit exceeded",
            response=MagicMock(status_code=429),
            body={},
        )

    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = mock_create
        MockAnthropic.return_value = mock_client

        llm = AnthropicLLM("sk-ant-fake-key", "claude-haiku-4-5", max_retries=3)

    with patch("time.sleep"):  # don't actually sleep in tests
        with pytest.raises(LLMRateLimitError):
            llm.complete_json("system", "user")

    # Should have tried max_retries times.
    assert call_count[0] == 3
