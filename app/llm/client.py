"""LLM client behind one interface (Anthropic | mock).

The pipeline calls get_llm_client() / get_resolve_client() / get_query_client();
it never imports the vendor SDK directly.

Three model variables (all overridable via env):
  LLM_EXTRACT_MODEL  default claude-haiku-4-5
  LLM_RESOLVE_MODEL  default claude-sonnet-4-6
  LLM_QUERY_MODEL    default claude-sonnet-4-6

Retry policy: 429, 5xx, and connection errors use exponential backoff with jitter.
Other 4xx errors fail fast.
"""
from __future__ import annotations

import os
import random
import re
import time
from pathlib import Path
from typing import Protocol


class LLMError(RuntimeError):
    pass


class LLMRateLimitError(LLMError):
    """Raised when we exhaust retries on a 429."""
    pass


class LLMClientError(LLMError):
    """Raised immediately on non-retriable 4xx (e.g. 400, 401, 403)."""
    pass


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        ...


class MockLLM:
    """Deterministic offline stand-in — returns an empty extraction so the pipeline
    still runs without a key (callers treat empty as 'no LLM entities')."""

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        return '{"messages": [], "mentions": [], "relations": []}'

    def complete_tool(self, system: str, user: str, tool: dict, max_tokens: int = 4096) -> str:
        return '{"messages": [], "mentions": [], "relations": []}'


class AnthropicLLM:
    """Thin wrapper around the Anthropic SDK.

    Retry policy:
      - 429 RateLimitError   → retry with backoff (up to max_retries).
      - 5xx APIStatusError   → retry with backoff.
      - APIConnectionError   → retry with backoff.
      - Other 4xx (400, etc) → raise LLMClientError immediately (no retry).
    """

    def __init__(self, api_key: str, model: str, max_retries: int = 4):
        if not api_key:
            raise LLMError("LLM_PROVIDER=anthropic but no API key set")
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_retries = max_retries

    def _backoff(self, attempt: int) -> None:
        base = 2 ** attempt  # 1, 2, 4, 8 seconds
        jitter = random.uniform(0, base * 0.2)
        time.sleep(base + jitter)

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        import anthropic
        last_err = None
        for attempt in range(self._max_retries):
            try:
                msg = self._client.messages.create(
                    model=self._model, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return msg.content[0].text
            except anthropic.RateLimitError as e:
                last_err = e
                self._backoff(attempt)
            except anthropic.APIStatusError as e:
                if e.status_code and e.status_code < 500:
                    # Non-retriable 4xx — fail fast.
                    raise LLMClientError(f"Anthropic API error {e.status_code}: {e}") from e
                last_err = e
                self._backoff(attempt)
            except anthropic.APIConnectionError as e:
                last_err = e
                self._backoff(attempt)
        raise LLMRateLimitError(f"Anthropic failed after {self._max_retries} retries: {last_err}")

    def complete_tool(self, system: str, user: str, tool: dict, max_tokens: int = 4096) -> str:
        """Call with tool_choice=tool so the model fills the schema."""
        import anthropic
        last_err = None
        for attempt in range(self._max_retries):
            try:
                msg = self._client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool["name"]},
                    messages=[{"role": "user", "content": user}],
                )
                # The response is in a tool_use block.
                for block in msg.content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        return json_dumps(block.input)
                return "{}"
            except anthropic.RateLimitError as e:
                last_err = e
                self._backoff(attempt)
            except anthropic.APIStatusError as e:
                if e.status_code and e.status_code < 500:
                    raise LLMClientError(f"Anthropic API error {e.status_code}: {e}") from e
                last_err = e
                self._backoff(attempt)
            except anthropic.APIConnectionError as e:
                last_err = e
                self._backoff(attempt)
        raise LLMRateLimitError(f"Anthropic failed after {self._max_retries} retries: {last_err}")


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj)


def _read_key() -> str:
    """Read the Anthropic key from the environment, or from this project's .env."""
    key = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        m = re.search(r"(?:LLM_API_KEY|ANTHROPIC_API_KEY)=(\S+)", env.read_text())
        if m and m.group(1).startswith("sk-ant"):
            return m.group(1)
    return ""


def get_llm_client(model: str | None = None) -> LLMClient:
    """Return an LLM client for the given model (or the extraction default).

    Model resolution order:
      1. Explicit `model` argument.
      2. LLM_EXTRACT_MODEL env var.
      3. Hard default 'claude-haiku-4-5'.
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic")
    if model is None:
        model = os.getenv("LLM_EXTRACT_MODEL") or "claude-haiku-4-5"
    if provider == "mock":
        return MockLLM()
    key = _read_key()
    if not key:
        return MockLLM()
    return AnthropicLLM(key, model)


def get_resolve_client() -> LLMClient:
    model = os.getenv("LLM_RESOLVE_MODEL") or "claude-sonnet-4-6"
    return get_llm_client(model)


def get_query_client() -> LLMClient:
    model = os.getenv("LLM_QUERY_MODEL") or "claude-sonnet-4-6"
    return get_llm_client(model)


def llm_available() -> bool:
    return not isinstance(get_llm_client(), MockLLM)
