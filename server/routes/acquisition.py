"""Acquisition routes: /api/acquisition, /api/acquisition-campaigns, /api/rotation/*, /api/generic-groups."""

import db as store


def get_acquisition(**kwargs):
    from dashboard import api_acquisition
    return api_acquisition()


def get_acquisition_campaigns(**kwargs):
    from dashboard import api_acquisition_campaigns
    return api_acquisition_campaigns()


def get_generic_groups(**kwargs):
    from dashboard import api_generic_groups
    return api_generic_groups()


def get_rotation_status(**kwargs):
    from dashboard import api_rotation_status
    return api_rotation_status()


def post_assign_campaign(body, handler, **kwargs):
    from dashboard import api_assign_group_campaign
    return api_assign_group_campaign(body)


def post_rotation_swap(body, handler, **kwargs):
    from dashboard import swap_client_group
    client_name = body.get("client_name", "")
    if not client_name:
        return {"error": "client_name required"}
    return swap_client_group(client_name)


def post_rotation_swap_all(body, handler, **kwargs):
    from dashboard import swap_client_group
    rotations = store.get_all_rotations()
    try:
        arch_state = store.get_state("archived_clients") or {"clients": []}
        arch_set = set(arch_state.get("clients", []))
    except Exception:
        arch_set = set()
    results = []
    for rot in rotations:
        if rot["client_name"] in arch_set:
            continue
        result = swap_client_group(rot["client_name"])
        results.append(result)
    return {"results": results}


GET_ROUTES = [
    ("/api/acquisition", get_acquisition),
    ("/api/acquisition-campaigns", get_acquisition_campaigns),
    ("/api/generic-groups", get_generic_groups),
    ("/api/rotation/status", get_rotation_status),
]

POST_ROUTES = [
    ("/api/acquisition/assign-campaign", post_assign_campaign),
    ("/api/rotation/swap", post_rotation_swap),
    ("/api/rotation/swap-all", post_rotation_swap_all),
]
