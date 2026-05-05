"""SmartLead API client with retry, caching, and structured errors.

Covers three API surfaces:
- Public v1 API (api_key auth)
- Internal API (JWT Bearer auth)
- Campaign/account management
"""

import os
import time
import requests
from server.services.base import BaseAPIClient
from server.cache import cache
from server.errors import SmartLeadError


SMARTLEAD_API = "https://server.smartlead.ai/api/v1"
SMARTLEAD_INTERNAL_API = "https://server.smartlead.ai/api"


class SmartLeadClient(BaseAPIClient):
    def __init__(self):
        super().__init__(SMARTLEAD_API, "SmartLead")
        self.api_key = os.environ.get("SMARTLEAD_API_KEY", "")
        self.jwt = os.environ.get("SMARTLEAD_JWT", "").strip().replace("\n", "").replace(" ", "")
        self.internal_url = SMARTLEAD_INTERNAL_API

    def _auth_params(self):
        return {"api_key": self.api_key}

    def _internal_headers(self):
        return {"Authorization": f"Bearer {self.jwt}", "Content-Type": "application/json"}

    # --- Public API (v1) ---

    def list_accounts(self, offset=0, limit=100, client_id=None):
        """List email accounts with pagination."""
        params = {**self._auth_params(), "offset": offset, "limit": limit}
        if client_id:
            params["client_id"] = client_id
        try:
            data, meta = self.get("/email-accounts/", params=params)
            return (data if isinstance(data, list) else []), meta
        except SmartLeadError:
            return [], {"cached": False, "stale_seconds": 0}

    def get_all_accounts(self, client_id=None, cache_key=None, cache_ttl=120):
        """Fetch all accounts with pagination. Returns (list, meta)."""
        if cache_key:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}

        accounts = []
        offset = 0
        while True:
            batch, _ = self.list_accounts(offset=offset, limit=100, client_id=client_id)
            accounts.extend(batch)
            if len(batch) < 100:
                break
            offset += 100

        if cache_key and accounts:
            cache.set(cache_key, accounts, cache_ttl)
        return accounts, {"cached": False, "stale_seconds": 0}

    def get_clients(self, cache_ttl=120):
        """Fetch all SmartLead clients."""
        return self.get("/client", cache_key="sl:clients", cache_ttl=cache_ttl,
                        params=self._auth_params())

    def create_client(self, name, email):
        """Create a new SmartLead client."""
        data, meta = self.post("/client/save", params=self._auth_params(),
                               json={"name": name, "email": email})
        cache.bust("sl:clients")
        return data, meta

    def list_campaigns(self, cache_ttl=120):
        """Fetch all campaigns."""
        return self.get("/campaigns", cache_key="sl:campaigns", cache_ttl=cache_ttl,
                        params=self._auth_params())

    def get_campaign_accounts(self, campaign_id):
        """Get email accounts for a specific campaign."""
        try:
            data, meta = self.get(f"/campaigns/{campaign_id}/email-accounts",
                                  params=self._auth_params())
            return (data if isinstance(data, list) else []), meta
        except SmartLeadError:
            return [], {"cached": False, "stale_seconds": 0}

    def add_accounts_to_campaign(self, campaign_id, account_ids):
        """Add email accounts to a campaign. Retries on 429."""
        for attempt in range(3):
            try:
                return self.post(
                    f"/campaigns/{campaign_id}/email-accounts",
                    params=self._auth_params(),
                    json={"email_account_ids": account_ids},
                )
            except SmartLeadError as e:
                if "429" in e.message and attempt < 2:
                    time.sleep(30)
                    continue
                raise

    def remove_accounts_from_campaign(self, campaign_id, account_ids):
        """Remove email accounts from a campaign. Retries on 429."""
        for attempt in range(3):
            try:
                return self.delete(
                    f"/campaigns/{campaign_id}/email-accounts",
                    params=self._auth_params(),
                    json={"email_account_ids": account_ids},
                )
            except SmartLeadError as e:
                if "429" in e.message and attempt < 2:
                    time.sleep(30)
                    continue
                raise

    def remove_account_from_campaign(self, campaign_id, account_id):
        """Remove a single account from a campaign by account ID."""
        return self.delete(
            f"/campaigns/{campaign_id}/email-accounts/{account_id}",
            params=self._auth_params(),
        )

    def delete_account(self, account_id):
        """Delete an email account."""
        return self.delete(f"/email-accounts/{account_id}", params=self._auth_params())

    def get_account(self, account_id):
        """Get a single account by ID."""
        return self.get(f"/email-accounts/{account_id}/", params=self._auth_params())

    # --- Internal API (JWT) ---

    def assign_account_to_client(self, account_id, client_id):
        """Assign an email account to a client (internal API)."""
        resp = requests.post(
            f"{self.internal_url}/email-account/save-management-details",
            headers=self._internal_headers(),
            json={"id": account_id, "clientId": client_id},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SmartLeadError(f"assign account failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def get_health_metrics(self, days=7, cache_ttl=120):
        """Get per-inbox health metrics (internal API)."""
        cache_key = f"sl:health:{days}"
        cached, stale = cache.get(cache_key)
        if cached is not None:
            return cached, {"cached": True, "stale_seconds": stale}

        from datetime import datetime, timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            resp = requests.get(
                f"{self.internal_url}/analytics/mailbox/name-wise-health-metrics",
                headers=self._internal_headers(),
                params={"start_date": start, "end_date": end,
                        "timezone": "America/New_York", "full_data": "true"},
                timeout=15,
            )
            if resp.status_code != 200:
                raise SmartLeadError(f"health metrics: {resp.status_code}")
            data = resp.json()
            metrics = data.get("data", {}).get("email_health_metrics", [])
            result = {m["from_email"]: m for m in metrics}
            cache.set(cache_key, result, cache_ttl)
            return result, {"cached": False, "stale_seconds": 0}
        except requests.RequestException as e:
            raise SmartLeadError(f"health metrics request failed: {e}")

    def get_day_wise_stats(self, campaign_ids, start_date, end_date):
        """Get day-wise overall stats for campaigns (internal API)."""
        resp = requests.get(
            f"{self.internal_url}/analytics/day-wise-overall-stats",
            headers=self._internal_headers(),
            params={
                "campaign_ids": ",".join(str(cid) for cid in campaign_ids),
                "start_date": start_date,
                "end_date": end_date,
                "timezone": "America/New_York",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise SmartLeadError(f"day-wise stats: {resp.status_code}")
        return resp.json()

    def delete_client(self, client_id):
        """Delete a SmartLead client (internal API)."""
        resp = requests.post(
            f"{self.internal_url}/client/delete",
            headers=self._internal_headers(),
            json={"id": client_id},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SmartLeadError(f"delete client failed: {resp.status_code}")
        cache.bust("sl:clients")
        return resp.json()

    def _make_error(self, message, status=502):
        return SmartLeadError(message, status)


smartlead = SmartLeadClient()
