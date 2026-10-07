"""Short-lived cache for heavy, read-only aggregates such as /api/stats."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable

STATS_CACHE_SECONDS = 30.0
# Past this age a cached answer is too old to hand out while a refresh runs.
STATS_MAX_STALE_SECONDS = 600.0


class StaleWhileRefreshCache:
    """One computation at a time per key.

    A fresh value is returned as is. When it goes stale, one request recomputes
    it while concurrent requests keep getting the previous value, so a slow
    aggregate never runs many times at once under load.
    """

    def __init__(
        self,
        ttl: float = STATS_CACHE_SECONDS,
        max_stale: float = STATS_MAX_STALE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 64,
    ):
        self._ttl = ttl
        self._max_stale = max_stale
        self._clock = clock
        self._max_keys = max_keys
        self._guard = threading.Lock()
        self._entries: dict[Hashable, tuple[float, Any]] = {}
        self._locks: dict[Hashable, threading.Lock] = {}

    def _lock_for(self, key: Hashable) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                if len(self._locks) >= self._max_keys:
                    # Drop idle keys so odd query values cannot grow this forever.
                    for k in [k for k, l in self._locks.items() if not l.locked()]:
                        self._locks.pop(k, None)
                        self._entries.pop(k, None)
                lock = self._locks[key] = threading.Lock()
            return lock

    def _peek(self, key: Hashable):
        with self._guard:
            return self._entries.get(key)

    def get(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        entry = self._peek(key)
        now = self._clock()
        if entry is not None and now - entry[0] < self._ttl:
            return entry[1]
        lock = self._lock_for(key)
        if entry is not None and now - entry[0] < self._max_stale:
            # Someone is already refreshing: serve the previous answer.
            if not lock.acquire(blocking=False):
                return entry[1]
        else:
            lock.acquire()
        try:
            entry = self._peek(key)
            if entry is not None and self._clock() - entry[0] < self._ttl:
                return entry[1]
            try:
                value = compute()
            except Exception:
                if entry is not None:
                    return entry[1]
                raise
            with self._guard:
                self._entries[key] = (self._clock(), value)
            return value
        finally:
            lock.release()
