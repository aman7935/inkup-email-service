"""Tiny in-process cache for API access tokens (Shiprocket, Shopify).

The service runs as a single process, so a dict is enough. Losing it on a
restart only costs one extra login.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache
from typing import Protocol


class TokenCache(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ex: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expires = item
            if expires is not None and expires <= time.monotonic():
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        expires = time.monotonic() + ex if ex else None
        with self._lock:
            self._data[key] = (value, expires)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def ttl(self, key: str) -> int:
        """Seconds left (-1 = no expiry, -2 = missing). Mirrors Redis for tests."""
        with self._lock:
            item = self._data.get(key)
        if item is None:
            return -2
        if item[1] is None:
            return -1
        return int(item[1] - time.monotonic())


@lru_cache(maxsize=1)
def get_cache() -> MemoryCache:
    return MemoryCache()
