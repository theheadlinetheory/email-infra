"""Overview routes: /api/overview, /api/clients, /api/clients/list, /api/untagged-count, /api/snapshot."""


def get_overview(**kwargs):
    from dashboard import api_overview
    return api_overview()


def get_clients(**kwargs):
    from dashboard import get_clients
    return get_clients()


def get_clients_list(**kwargs):
    from dashboard import api_clients_list
    return api_clients_list()


def get_untagged_count(**kwargs):
    from dashboard import api_untagged_count
    return api_untagged_count()


def get_snapshot(**kwargs):
    from dashboard import get_all_accounts
    import marsha
    return marsha.run_snapshot_check(get_all_accounts())


def get_supabase_config(**kwargs):
    import db as store
    return {"url": store.SUPABASE_URL, "key": store.SUPABASE_KEY}


GET_ROUTES = [
    ("/api/overview", get_overview),
    ("/api/clients", get_clients),
    ("/api/clients/list", get_clients_list),
    ("/api/untagged-count", get_untagged_count),
    ("/api/snapshot", get_snapshot),
    ("/api/supabase-config", get_supabase_config),
]

POST_ROUTES = []
