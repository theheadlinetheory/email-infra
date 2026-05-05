"""Base API client with retry, backoff, and caching."""

import time
import requests
from server.cache import cache
from server.errors import APIError


class BaseAPIClient:
    def __init__(self, base_url, name, max_retries=3, backoff_base=1.0):
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    def _request(self, method, path, cache_key=None, cache_ttl=0, **kwargs):
        """Make an HTTP request with retry and optional caching.

        Returns (parsed_json, meta_dict). Raises APIError on failure.
        """
        if cache_key and cache_ttl > 0:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}

        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        last_error = None

        for attempt in range(self.max_retries):
            try:
                resp = requests.request(method, url, timeout=30, **kwargs)
                if resp.status_code >= 500:
                    last_error = f"{self.name} returned {resp.status_code}: {resp.text[:200]}"
                    if attempt < self.max_retries - 1:
                        time.sleep(self.backoff_base * (2 ** attempt))
                        continue
                    raise self._make_error(last_error)
                if resp.status_code >= 400:
                    raise self._make_error(
                        f"{self.name} returned {resp.status_code}: {resp.text[:200]}",
                        status=resp.status_code
                    )
                data = resp.json() if resp.text else {}
                if cache_key and cache_ttl > 0:
                    cache.set(cache_key, data, cache_ttl)
                return data, {"cached": False, "stale_seconds": 0}
            except requests.RequestException as e:
                last_error = f"{self.name} request failed: {e}"
                if attempt < self.max_retries - 1:
                    time.sleep(self.backoff_base * (2 ** attempt))
                    continue
                raise self._make_error(last_error)

        raise self._make_error(last_error or f"{self.name} failed after {self.max_retries} retries")

    def get(self, path="", cache_key=None, cache_ttl=0, **kwargs):
        return self._request("GET", path, cache_key=cache_key, cache_ttl=cache_ttl, **kwargs)

    def post(self, path="", cache_key=None, cache_ttl=0, **kwargs):
        return self._request("POST", path, cache_key=cache_key, cache_ttl=cache_ttl, **kwargs)

    def put(self, path="", **kwargs):
        return self._request("PUT", path, **kwargs)

    def delete(self, path="", **kwargs):
        return self._request("DELETE", path, **kwargs)

    def _make_error(self, message, status=502):
        return APIError(f"{self.name.upper()}_ERROR", message, status)
