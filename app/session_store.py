"""Session-scoped in-memory bundle store for DEMO_READONLY mode.

In demo mode uploads build a per-session bundle (LRU, TTL-evicted) that
never persists and never replaces the shared demo graph.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional

_SESSION_TTL = 30 * 60   # 30 minutes
_SESSION_MAX = 10         # LRU cap — evict oldest when over this count


class SessionStore:
    def __init__(self, ttl: int = _SESSION_TTL, max_sessions: int = _SESSION_MAX):
        # OrderedDict preserves insertion order for LRU eviction
        self._store: OrderedDict = OrderedDict()
        self._ttl = ttl
        self._max = max_sessions

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, (_, ts) in list(self._store.items()) if now - ts > self._ttl]
        for k in expired:
            del self._store[k]
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def get(self, session_id: str) -> Optional[object]:
        self._evict()
        entry = self._store.get(session_id)
        if entry is None:
            return None
        bundle, _ = entry
        self._store[session_id] = (bundle, time.time())
        self._store.move_to_end(session_id)
        return bundle

    def put(self, session_id: str, bundle: object) -> None:
        self._evict()
        self._store[session_id] = (bundle, time.time())
        self._store.move_to_end(session_id)

    def count(self) -> int:
        self._evict()
        return len(self._store)
