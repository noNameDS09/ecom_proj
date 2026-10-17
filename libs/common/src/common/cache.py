"""A tiny in-process TTL cache.

Used for hot read paths (single product lookups) to take load off Postgres.
It is deliberately per-replica: entries are invalidated on every write in the
same process and expire after a short TTL, which is acceptable for catalogue
reads. Anything that must be strongly consistent (stock, orders, payments) is
never cached.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Hashable
from typing import Any, Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    def __init__(self, *, maxsize: int = 1024, ttl: float = 30.0) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    async def get(self, key: K) -> V | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    async def set(self, key: K, value: V) -> None:
        async with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
                self.evictions += 1

    async def invalidate(self, key: K) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    @property
    def size(self) -> int:
        return len(self._data)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "size": self.size,
            "maxsize": self.maxsize,
            "ttl_seconds": self.ttl,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_ratio": round(self.hits / total, 4) if total else 0.0,
        }


__all__ = ["TTLCache"]
