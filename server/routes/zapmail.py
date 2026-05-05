"""Zapmail routes: /api/zapmail, /api/zapmail/sync, /api/zapmail/cancel, /api/wallet, /api/subscriptions, /api/placement-tests."""


def get_zapmail(**kwargs):
    from dashboard import api_zapmail
    return api_zapmail()


def get_zapmail_sync(**kwargs):
    from dashboard import api_zapmail_sync
    return api_zapmail_sync()


def get_wallet(**kwargs):
    from dashboard import api_wallet
    return api_wallet()


def get_placement_tests(**kwargs):
    from dashboard import api_placement_tests
    return api_placement_tests()


def get_subscriptions(**kwargs):
    from dashboard import api_subscriptions
    return api_subscriptions()


def post_zapmail_cancel(body, handler, **kwargs):
    from dashboard import zm_delete_domains
    domain_ids = body.get("domain_ids", [])
    if not domain_ids:
        return {"error": "domain_ids required"}
    return zm_delete_domains(domain_ids)


GET_ROUTES = [
    ("/api/zapmail", get_zapmail),
    ("/api/zapmail/sync", get_zapmail_sync),
    ("/api/wallet", get_wallet),
    ("/api/placement-tests", get_placement_tests),
    ("/api/subscriptions", get_subscriptions),
]

POST_ROUTES = [
    ("/api/zapmail/cancel", post_zapmail_cancel),
]
