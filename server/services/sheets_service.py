"""Google Sheets wrapper with retry and caching for domain inventory."""

import os
import time
from server.cache import cache
from server.errors import SheetsError


class SheetsService:
    """Wraps sheets.py operations with caching and error handling."""

    def __init__(self):
        self._sheets_module = None

    def _sheets(self):
        """Lazy-load the sheets module to avoid import-time side effects."""
        if self._sheets_module is None:
            import sheets
            self._sheets_module = sheets
        return self._sheets_module

    def get_all_master_domains(self, cache_ttl=300):
        """Get all domains from the master sheet. Returns (data, meta)."""
        cache_key = "sheets:master_domains"
        if cache_ttl > 0:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}
        try:
            result = self._sheets().get_all_master_domains()
            if cache_ttl > 0:
                cache.set(cache_key, result, cache_ttl)
            return result, {"cached": False, "stale_seconds": 0}
        except Exception as e:
            raise SheetsError(f"Failed to read master domains: {e}")

    def get_available_domains(self, cache_ttl=300):
        """Get available (unassigned) client domains."""
        cache_key = "sheets:available"
        if cache_ttl > 0:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}
        try:
            result = self._sheets().get_available_domains()
            if cache_ttl > 0:
                cache.set(cache_key, result, cache_ttl)
            return result, {"cached": False, "stale_seconds": 0}
        except Exception as e:
            raise SheetsError(f"Failed to read available domains: {e}")

    def get_acquisition_domains(self, cache_ttl=300):
        """Get available acquisition pool domains."""
        cache_key = "sheets:acquisition"
        if cache_ttl > 0:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}
        try:
            result = self._sheets().get_acquisition_domains()
            if cache_ttl > 0:
                cache.set(cache_key, result, cache_ttl)
            return result, {"cached": False, "stale_seconds": 0}
        except Exception as e:
            raise SheetsError(f"Failed to read acquisition domains: {e}")

    def claim_domains(self, domains_to_claim, client_name):
        """Mark domains as in-use for a client."""
        try:
            self._sheets().claim_domains(domains_to_claim, client_name)
            cache.bust("sheets:")
            return True
        except Exception as e:
            raise SheetsError(f"Failed to claim domains: {e}")

    def mark_domains_in_use(self, domains_with_rows, client_name):
        """Batch mark domains as in-use."""
        try:
            self._sheets().mark_domains_in_use_batch(domains_with_rows, client_name)
            cache.bust("sheets:")
            return True
        except Exception as e:
            raise SheetsError(f"Failed to mark domains in use: {e}")

    def setup_client_tab(self, client_name, domains, setup_date=None):
        """Create or update a client tab with domain list."""
        try:
            self._sheets().setup_client_tab(client_name, domains, setup_date)
            return True
        except Exception as e:
            raise SheetsError(f"Failed to setup client tab: {e}")

    def write_range(self, tab, range_str, values):
        """Write values to a sheet range."""
        try:
            self._sheets().write_range(tab, range_str, values)
            cache.bust("sheets:")
            return True
        except Exception as e:
            raise SheetsError(f"Failed to write to sheet: {e}")

    def get_domain_summary(self, cache_ttl=300):
        """Get aggregated domain inventory summary."""
        cache_key = "sheets:summary"
        if cache_ttl > 0:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}
        try:
            result = self._sheets().get_domain_summary()
            if cache_ttl > 0:
                cache.set(cache_key, result, cache_ttl)
            return result, {"cached": False, "stale_seconds": 0}
        except Exception as e:
            raise SheetsError(f"Failed to get domain summary: {e}")


sheets_service = SheetsService()
