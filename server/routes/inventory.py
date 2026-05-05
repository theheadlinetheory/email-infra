"""Inventory routes: /api/domain-inventory, /api/unassigned, /api/inbox/*/campaigns, /api/inbox/remove-*."""

from dashboard import invalidate_cache


def _match_inbox_campaigns(path):
    if path.startswith("/api/inbox/") and path.endswith("/campaigns"):
        email = path.split("/")[3]
        return True, {"email": email}
    return False, {}


def get_domain_inventory(**kwargs):
    from dashboard import api_domain_inventory
    return api_domain_inventory()


def get_unassigned(**kwargs):
    from dashboard import api_unassigned
    return api_unassigned()


def get_inbox_campaigns(email, **kwargs):
    from dashboard import api_inbox_campaigns
    return api_inbox_campaigns(email)


def get_inbox_history(**kwargs):
    import inbox_history
    params = kwargs.get("_params", {})
    return inbox_history.query_history(params)


def get_debug_supabase(**kwargs):
    from dashboard import api_debug_supabase
    return api_debug_supabase()


def post_remove_from_campaign(body, handler, **kwargs):
    from dashboard import api_remove_from_campaign
    result = api_remove_from_campaign(body)
    invalidate_cache()
    return result


def post_remove_from_all_campaigns(body, handler, **kwargs):
    from dashboard import api_remove_from_all_campaigns
    result = api_remove_from_all_campaigns(body)
    invalidate_cache()
    return result


GET_ROUTES = [
    ("/api/domain-inventory", get_domain_inventory),
    ("/api/unassigned", get_unassigned),
    (_match_inbox_campaigns, get_inbox_campaigns),
    ("/api/inbox-history", get_inbox_history),
    ("/api/debug/supabase", get_debug_supabase),
]

POST_ROUTES = [
    ("/api/inbox/remove-from-campaign", post_remove_from_campaign),
    ("/api/inbox/remove-from-all-campaigns", post_remove_from_all_campaigns),
]
