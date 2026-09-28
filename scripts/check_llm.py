#!/usr/bin/env python3
"""Smoke-test each configured LLM model with a minimal call.

Prints PASS / FAIL for each model, with a clear error message if the model id
is bad or the API key is missing.

    python -m scripts.check_llm
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check_model(label: str, model: str) -> bool:
    from app.llm.client import AnthropicLLM, LLMClientError, LLMError, _read_key

    key = _read_key()
    if not key:
        print(f"  [{label}] SKIP — no API key (set LLM_API_KEY or ANTHROPIC_API_KEY)")
        return True  # not a failure, just offline

    try:
        client = AnthropicLLM(key, model, max_retries=1)
        result = client.complete_json(
            system="Reply with the exact JSON: {\"ok\": true}",
            user="ping",
            max_tokens=16,
        )
        if "ok" in result:
            print(f"  [{label}] PASS  model={model!r}  response={result[:60]!r}")
            return True
        else:
            print(f"  [{label}] WARN  model={model!r}  unexpected response: {result[:80]!r}")
            return True
    except LLMClientError as e:
        print(f"  [{label}] FAIL  model={model!r}  client error: {e}")
        return False
    except LLMError as e:
        print(f"  [{label}] FAIL  model={model!r}  error: {e}")
        return False
    except Exception as e:
        print(f"  [{label}] FAIL  model={model!r}  unexpected: {type(e).__name__}: {e}")
        return False


def main():
    print("LLM model smoke-test")
    print("=" * 50)

    checks = [
        ("extract",  os.getenv("LLM_EXTRACT_MODEL") or os.getenv("LLM_MODEL") or "claude-haiku-4-5"),
        ("resolve",  os.getenv("LLM_RESOLVE_MODEL") or os.getenv("LLM_MODEL") or "claude-sonnet-4-6"),
        ("query",    os.getenv("LLM_QUERY_MODEL") or os.getenv("LLM_MODEL") or "claude-sonnet-4-6"),
    ]

    all_ok = True
    for label, model in checks:
        ok = check_model(label, model)
        if not ok:
            all_ok = False

    print("=" * 50)
    if all_ok:
        print("All checks passed (or skipped due to no key).")
        sys.exit(0)
    else:
        print("One or more checks FAILED. Check model ids and API key.")
        sys.exit(1)


if __name__ == "__main__":
    main()
