"""In-memory TTL cache for API responses."""

import time
import threading


class TTLCache:
    def __init__(self):
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key):
        """Return (value, stale_seconds) or (None, None) if expired/missing."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, None
            value, expires_at, stored_at = entry
            now = time.time()
            if now > expires_at:
                del self._store[key]
                return None, None
            stale_seconds = int(now - stored_at)
            return value, stale_seconds

    def set(self, key, value, ttl_seconds):
        """Store value with TTL."""
        now = time.time()
        with self._lock:
            self._store[key] = (value, now + ttl_seconds, now)

    def bust(self, *prefixes):
        """Remove all keys starting with any of the given prefixes."""
        with self._lock:
            keys_to_delete = [
                k for k in self._store
                if any(k.startswith(p) for p in prefixes)
            ]
            for k in keys_to_delete:
                del self._store[k]

    def clear(self):
        with self._lock:
            self._store.clear()


cache = TTLCache()
