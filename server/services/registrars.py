"""Unified registrar client for Porkbun and Spaceship with retry and caching."""

import os
import requests
from server.services.base import BaseAPIClient
from server.cache import cache
from server.errors import RegistrarError


PORKBUN_API = "https://api.porkbun.com/api/json/v3"
SPACESHIP_API = "https://spaceship.dev/api/v1"


class PorkbunClient(BaseAPIClient):
    def __init__(self):
        super().__init__(PORKBUN_API, "Porkbun")
        self.api_key = os.environ.get("PORKBUN_API_KEY", "")
        self.secret_key = os.environ.get("PORKBUN_SECRET_KEY", "")

    def _auth_body(self):
        return {"apikey": self.api_key, "secretapikey": self.secret_key}

    def is_configured(self):
        return bool(self.api_key and self.secret_key)

    def list_domains(self, cache_ttl=300):
        """List all Porkbun domains with expiry dates."""
        if not self.is_configured():
            return [], {"cached": False, "stale_seconds": 0}
        data, meta = self.post("/domain/listAll",
                               cache_key="pb:domains", cache_ttl=cache_ttl,
                               json=self._auth_body())
        if data.get("status") != "SUCCESS":
            return [], meta
        result = []
        for d in data.get("domains", []):
            result.append({
                "domain": d.get("domain", ""),
                "registrar": "porkbun",
                "status": d.get("status", "UNKNOWN"),
                "expires": d.get("expireDate", "")[:10],
                "auto_renew": d.get("autoRenew") == "1",
                "created": d.get("createDate", "")[:10],
            })
        return result, meta

    def set_auto_renew(self, domain, enabled):
        """Toggle auto-renew on a domain."""
        body = {**self._auth_body(), "status": "on" if enabled else "off"}
        data, meta = self.post(f"/domain/updateAutoRenew/{domain}", json=body)
        cache.bust("pb:domains")
        return {"success": data.get("status") == "SUCCESS",
                "message": data.get("message", "")}, meta

    def _make_error(self, message, status=502):
        return RegistrarError(message, status)


class SpaceshipClient(BaseAPIClient):
    def __init__(self):
        super().__init__(SPACESHIP_API, "Spaceship")
        self.api_key = os.environ.get("SPACESHIP_API_KEY", "")
        self.secret_key = os.environ.get("SPACESHIP_SECRET_KEY", "")

    def _auth_headers(self):
        return {
            "X-API-Key": self.api_key,
            "X-API-Secret": self.secret_key,
            "Content-Type": "application/json",
        }

    def is_configured(self):
        return bool(self.api_key and self.secret_key)

    def list_domains(self, cache_ttl=300):
        """List all Spaceship domains with pagination."""
        if not self.is_configured():
            return [], {"cached": False, "stale_seconds": 0}

        cache_key = "ss:domains"
        if cache_ttl > 0:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}

        all_domains = []
        skip = 0
        while True:
            try:
                resp = requests.get(
                    f"{self.base_url}/domains",
                    headers=self._auth_headers(),
                    params={"take": 100, "skip": skip},
                    timeout=30,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                items = data.get("items", []) if isinstance(data, dict) else []
                if not items:
                    break
                for d in items:
                    all_domains.append({
                        "domain": d.get("name", ""),
                        "registrar": "spaceship",
                        "status": d.get("lifecycleStatus", "UNKNOWN"),
                        "expires": d.get("expirationDate", "")[:10],
                        "auto_renew": d.get("autoRenew", False),
                        "created": d.get("registrationDate", "")[:10],
                    })
                if len(items) < 100:
                    break
                skip += 100
            except requests.RequestException as e:
                raise RegistrarError(f"Spaceship domain list failed: {e}")

        if cache_ttl > 0:
            cache.set(cache_key, all_domains, cache_ttl)
        return all_domains, {"cached": False, "stale_seconds": 0}

    def set_auto_renew(self, domain, enabled):
        """Toggle auto-renew on a domain."""
        try:
            resp = requests.put(
                f"{self.base_url}/domains/{domain}/autorenew",
                headers=self._auth_headers(),
                json={"isEnabled": enabled},
                timeout=15,
            )
            cache.bust("ss:domains")
            if resp.status_code in (200, 204):
                return {"success": True,
                        "message": f"Auto-renew {'enabled' if enabled else 'disabled'}"}, {}
            return {"success": False, "message": resp.text[:200]}, {}
        except requests.RequestException as e:
            raise RegistrarError(f"Spaceship auto-renew failed: {e}")

    def _make_error(self, message, status=502):
        return RegistrarError(message, status)


class RegistrarService:
    """Unified interface for all registrar operations."""

    def __init__(self):
        self.porkbun = PorkbunClient()
        self.spaceship = SpaceshipClient()

    def list_all_domains(self, cache_ttl=300):
        """List domains from all registrars. Returns (combined_list, meta)."""
        pb_domains, pb_meta = self.porkbun.list_domains(cache_ttl=cache_ttl)
        ss_domains, ss_meta = self.spaceship.list_domains(cache_ttl=cache_ttl)
        combined = pb_domains + ss_domains
        meta = {"cached": pb_meta.get("cached", False) and ss_meta.get("cached", False),
                "stale_seconds": max(pb_meta.get("stale_seconds", 0),
                                     ss_meta.get("stale_seconds", 0))}
        return combined, meta

    def set_auto_renew(self, domain, registrar, enabled):
        """Toggle auto-renew on a domain by registrar name."""
        if registrar == "porkbun":
            return self.porkbun.set_auto_renew(domain, enabled)
        elif registrar == "spaceship":
            return self.spaceship.set_auto_renew(domain, enabled)
        raise RegistrarError(f"Unknown registrar: {registrar}")


registrars = RegistrarService()
