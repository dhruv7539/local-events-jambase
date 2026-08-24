"""A minimal in-memory TTL cache.

Scope call: no Redis, no database. This is a single-process cache whose only
job is to keep a trial API key alive under repeated identical queries. Its
limitations are real and documented in WRITEUP.md rather than papered over.
"""

from __future__ import annotations

import asyncio
import time
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Async-safe key/value store where every entry expires after `ttl`.

    Uses a monotonic clock so entries do not expire early or late if the
    system wall clock is adjusted.
    """

    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._entries: dict[str, tuple[float, T]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> T | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                # Expired entries are dropped on read. There is no background
                # sweeper, so a key never read again holds its memory until
                # process exit — acceptable for this cardinality, noted in
                # WRITEUP.md as a real limitation.
                del self._entries[key]
                return None
            return value

    async def set(self, key: str, value: T) -> None:
        async with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl, value)
