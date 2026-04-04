"""New ZapMail API operations for the autonomous dashboard.

Wraps endpoints not already in setup.py: subscriptions, wallet,
domain health, profile photos, placement tests, remove-on-renewal,
retry-failed, export status, cleanup.
"""

from setup import zm_get, zm_post, zm_put


def zm_get_subscriptions():
    """Get all subscriptions (active, cancelled, expired) with billing details."""
    return zm_get("/v2/subscriptions")


def zm_get_subscription_mailboxes(subscription_id):
    """Get mailboxes tied to a specific subscription."""
    return zm_get(f"/v2/subscriptions/{subscription_id}/mailboxes")


def zm_cancel_subscription(subscription_id, revert=False):
    """Cancel a subscription or revert cancellation.
    Body: {revert: true} to undo cancellation.
    """
    body = {"revert": revert} if revert else {}
    return zm_put(f"/v2/subscriptions/{subscription_id}/cancel", body)


def zm_get_wallet_balance():
    """Get current ZapMail wallet balance."""
    return zm_get("/v2/wallet/balance")


def zm_get_domain_health(domain_id):
    """Get domain health/reputation score based on NS reputation."""
    return zm_get(f"/v2/domains/{domain_id}/health-score")


def zm_update_mailboxes(mailbox_data):
    """Update mailboxes (profile photo, etc).
    mailbox_data: list of {mailboxId, profilePicture} dicts.
    Endpoint: PUT /v2/mailboxes
    """
    return zm_put("/v2/mailboxes", {"mailboxData": mailbox_data})


def zm_remove_on_renewal(mailbox_ids):
    """Schedule mailbox removal at next renewal (no immediate deletion).
    Body: {ids: [mailboxId, ...]}
    """
    return zm_post("/v2/mailboxes/remove-on-renewal", {"ids": mailbox_ids})


def zm_delete_mailboxes(mailbox_ids):
    """Instantly remove mailboxes.
    Uses DELETE /v2/mailboxes with body {ids: [...]}.
    """
    import requests
    from setup import ZAPMAIL_API, zm_headers
    r = requests.delete(
        f"{ZAPMAIL_API}/v2/mailboxes",
        headers=zm_headers(),
        json={"ids": mailbox_ids},
        timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}


def zm_retry_failed_mailboxes():
    """Retry creation of failed mailboxes."""
    return zm_post("/v2/mailboxes/retry-failed")


def zm_get_export_status():
    """Get current export operation status."""
    return zm_get("/v2/export/status")


def zm_verify_nameservers(domain_names):
    """Verify nameservers are set correctly before connecting.
    Body: {domainNames: [...]}
    """
    return zm_post("/v2/domains/verify-nameservers", {"domainNames": domain_names})


def zm_delete_unused_domains():
    """Remove all domains with no mailboxes."""
    import requests
    from setup import ZAPMAIL_API, zm_headers
    r = requests.delete(
        f"{ZAPMAIL_API}/v2/domains/unused",
        headers=zm_headers(),
        timeout=30,
    )
    try:
        return r.json()
    except Exception:
        return {"_raw_status": r.status_code, "_raw_text": r.text[:500]}


def zm_run_placement_test(mailbox_ids):
    """Purchase/run placement tests for given mailboxes.
    Body: {mailboxIds: [...]}
    """
    return zm_post("/v2/placement-test/purchase", {"mailboxIds": mailbox_ids})


def zm_get_placement_results():
    """Get placement test orders with results."""
    return zm_get("/v2/placement-test/orders")


def zm_get_placement_report(cart_order_id):
    """Get detailed placement report for a specific test order."""
    return zm_get(f"/v2/placement-test/orders/{cart_order_id}/report")


def zm_get_placement_eligible_mailboxes():
    """Get mailboxes eligible for placement testing."""
    return zm_get("/v2/placement-test/mailboxes/eligible")


def zm_get_placement_credits():
    """Get available placement test credits."""
    return zm_get("/v2/placement-test/credits/available")
