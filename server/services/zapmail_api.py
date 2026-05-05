"""Zapmail API client with retry, caching, and structured errors.

Covers domains, mailboxes, subscriptions, wallet, exports, and placement tests.
"""

import os
from server.services.base import BaseAPIClient
from server.cache import cache
from server.errors import ZapmailError


ZAPMAIL_API = "https://api.zapmail.ai/api"


class ZapmailClient(BaseAPIClient):
    def __init__(self):
        super().__init__(ZAPMAIL_API, "Zapmail")
        self.api_key = os.environ.get("ZAPMAIL_API_KEY", "")

    def _headers(self):
        return {"x-auth-zapmail": self.api_key, "Content-Type": "application/json"}

    def _request(self, method, path, cache_key=None, cache_ttl=0, **kwargs):
        kwargs.setdefault("headers", self._headers())
        return super()._request(method, path, cache_key=cache_key, cache_ttl=cache_ttl, **kwargs)

    # --- Domains ---

    def list_domains(self, cache_ttl=0):
        """List all domains with pagination."""
        cache_key = "zm:domains" if cache_ttl > 0 else None
        if cache_key:
            cached, stale = cache.get(cache_key)
            if cached is not None:
                return cached, {"cached": True, "stale_seconds": stale}

        all_domains = []
        page = 1
        while True:
            data, _ = self.get(f"/v2/domains?page={page}")
            if isinstance(data, dict) and "data" in data:
                domains = data["data"].get("domains", [])
                all_domains.extend(domains)
                if page >= data["data"].get("totalPages", 1):
                    break
                page += 1
            else:
                break

        if cache_key:
            cache.set(cache_key, all_domains, cache_ttl)
        return all_domains, {"cached": False, "stale_seconds": 0}

    def delete_domains(self, domain_ids):
        """Delete/cancel domains (stops billing)."""
        data, meta = self.delete("/v2/domains", json={"domainIds": domain_ids})
        cache.bust("zm:domains")
        return data, meta

    def get_domain_health(self, domain_id):
        """Get domain health/reputation score."""
        return self.get(f"/v2/domains/{domain_id}/health-score")

    def verify_nameservers(self, domain_names):
        """Verify nameservers are set correctly."""
        return self.post("/v2/domains/verify-nameservers", json={"domainNames": domain_names})

    def list_domain_tags(self, cache_ttl=300):
        """List all domain tags."""
        return self.get("/v2/domains/tags", cache_key="zm:tags", cache_ttl=cache_ttl)

    def create_domain_tag(self, name, color=None):
        """Create a new domain tag."""
        body = {"name": name}
        if color:
            body["color"] = color
        data, meta = self.post("/v2/domains/tags", json=body)
        cache.bust("zm:tags")
        return data, meta

    def assign_domain_tag(self, domain_ids, tag_ids):
        """Assign tags to domains."""
        return self.post("/v2/domains/assign-tag",
                         json={"domainIds": domain_ids, "tagIds": tag_ids})

    def set_forwarding(self, domain_ids, forward_to):
        """Set domain forwarding URL."""
        return self.post("/v2/domains/forwarding",
                         json={"domainIds": domain_ids, "forwardTo": forward_to})

    def delete_unused_domains(self):
        """Remove domains with no mailboxes."""
        data, meta = self.delete("/v2/domains/unused")
        cache.bust("zm:domains")
        return data, meta

    # --- Mailboxes ---

    def list_mailboxes(self, domain_id=None, cache_ttl=0):
        """List mailboxes, optionally filtered by domain."""
        path = f"/v2/mailboxes?domainId={domain_id}" if domain_id else "/v2/mailboxes"
        return self.get(path, cache_ttl=cache_ttl)

    def create_mailboxes(self, domain_id, domain_name, mailbox_specs):
        """Create mailboxes on a domain."""
        body = {"domainId": domain_id, "domainName": domain_name, "mailboxes": mailbox_specs}
        data, meta = self.post("/v2/mailboxes", json=body)
        cache.bust("zm:domains")
        return data, meta

    def update_mailboxes(self, mailbox_data):
        """Batch update mailboxes (profile photo, etc)."""
        return self.put("/v2/mailboxes", json=mailbox_data)

    def delete_mailboxes(self, mailbox_ids):
        """Instantly remove mailboxes."""
        return self.delete("/v2/mailboxes", json={"mailboxIds": mailbox_ids})

    def remove_on_renewal(self, mailbox_ids):
        """Schedule mailbox removal at next renewal."""
        return self.post("/v2/mailboxes/remove-on-renewal", json={"mailboxIds": mailbox_ids})

    def retry_failed_mailboxes(self):
        """Retry creation of failed mailboxes."""
        return self.post("/v2/mailboxes/retry-failed")

    # --- Subscriptions ---

    def get_subscriptions(self, cache_ttl=300):
        """Get all subscriptions with billing details."""
        return self.get("/v2/subscriptions", cache_key="zm:subscriptions", cache_ttl=cache_ttl)

    def get_subscription_mailboxes(self, subscription_id):
        """Get mailboxes for a subscription."""
        return self.get(f"/v2/subscriptions/{subscription_id}/mailboxes")

    def cancel_subscription(self, subscription_id, revert=False):
        """Cancel a subscription or revert cancellation."""
        body = {"revert": revert} if revert else {}
        return self.put(f"/v2/subscriptions/{subscription_id}/cancel", json=body)

    # --- Wallet ---

    def get_wallet_balance(self, cache_ttl=600):
        """Get wallet balance."""
        return self.get("/v2/wallet/balance", cache_key="zm:wallet", cache_ttl=cache_ttl)

    def buy_addon_mailboxes(self, quantity):
        """Buy add-on mailbox slots."""
        return self.post(f"/v2/wallet/buy-addon-mailboxes?quantity={quantity}")

    # --- Exports ---

    def add_third_party_account(self, email, password, app="SMARTLEAD"):
        """Add third-party account for export."""
        return self.post("/v2/exports/accounts/third-party",
                         json={"email": email, "password": password, "app": app})

    def export_mailboxes(self, apps, mailbox_ids=None, contains=None):
        """Export mailboxes to third-party app."""
        body = {"apps": apps}
        if mailbox_ids:
            body["mailboxIds"] = mailbox_ids
        if contains:
            body["contains"] = contains
        return self.post("/v2/exports/mailboxes", json=body)

    def get_export_status(self):
        """Get current export operation status."""
        return self.get("/v2/export/status")

    # --- Placement Tests ---

    def run_placement_test(self, mailbox_ids):
        """Purchase/run placement tests."""
        return self.post("/v2/placement-test/purchase", json={"mailboxIds": mailbox_ids})

    def get_placement_results(self, cache_ttl=300):
        """Get placement test orders with results."""
        return self.get("/v2/placement-test/orders",
                        cache_key="zm:placement", cache_ttl=cache_ttl)

    def get_placement_report(self, cart_order_id):
        """Get detailed placement report."""
        return self.get(f"/v2/placement-test/orders/{cart_order_id}/report")

    def get_eligible_mailboxes(self):
        """Get mailboxes eligible for placement testing."""
        return self.get("/v2/placement-test/mailboxes/eligible")

    def get_placement_credits(self):
        """Get available placement test credits."""
        return self.get("/v2/placement-test/credits/available")

    # --- Workspaces ---

    def list_workspaces(self):
        """List all workspaces."""
        return self.get("/v2/workspaces")

    def _make_error(self, message, status=502):
        return ZapmailError(message, status)


zapmail = ZapmailClient()
