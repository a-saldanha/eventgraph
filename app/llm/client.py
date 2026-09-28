"""LLM client behind one interface (Anthropic | mock).

The pipeline never imports the vendor SDK directly — it calls `get_llm_client()`.
This keeps the model swappable and lets tests / offline runs use the mock.
"""
from __future__ import annotations

import os
import time
from typing import Protocol


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        ...


class MockLLM:
    """Deterministic offline stand-in — returns an empty extraction so the pipeline
    still runs without a key (callers treat empty as 'no LLM entities')."""

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        return '{"items": []}'


class AnthropicLLM:
    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise LLMError("LLM_PROVIDER=anthropic but no API key set")
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> str:
        import anthropic

        last_err = None
        for attempt in range(4):  # backoff on rate limits / transient errors
            try:
                msg = self._client.messages.create(
                    model=self._model, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user}],
                )
                return msg.content[0].text
            except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
                last_err = e
                time.sleep(2 ** attempt)  # 1,2,4,8s
        raise LLMError(f"Anthropic failed after retries: {last_err}")


def _read_key() -> str:
    # env first; fall back to the sibling invoice project's .env for local dev
    key = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        return key
    from pathlib import Path
    import re

    # this project's own .env first, then the sibling invoice project's (legacy)
    candidates = (
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parents[3] / "backend" / ".env",
    )
    for p in candidates:
        if p.exists():
            m = re.search(r"(?:LLM_API_KEY|ANTHROPIC_API_KEY)=(\S+)", p.read_text())
            if m and m.group(1).startswith("sk-ant"):
                return m.group(1)
    return ""


def get_llm_client(model: str | None = None) -> LLMClient:
    """Model tiering: pass an explicit model (e.g. a smarter one for querying);
    otherwise falls back to LLM_MODEL (the cheaper extraction default)."""
    provider = os.getenv("LLM_PROVIDER", "anthropic")
    model = model or os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    if provider == "mock":
        return MockLLM()
    key = _read_key()
    if not key:
        return MockLLM()
    return AnthropicLLM(key, model)


def get_query_client() -> LLMClient:
    """The smarter model used for answering natural-language questions."""
    return get_llm_client(os.getenv("LLM_QUERY_MODEL", "claude-opus-4-8"))


def llm_available() -> bool:
    return not isinstance(get_llm_client(), MockLLM)
