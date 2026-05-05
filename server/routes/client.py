"""Client routes: /api/client/{id}/accounts, /api/client/{id}/trends, archive, pause, target volume."""

import db as store
from dashboard import invalidate_cache


def _match_client_accounts(path):
    if path.startswith("/api/client/") and path.endswith("/accounts"):
        client_id = path.split("/")[3]
        return True, {"client_id": client_id}
    return False, {}


def _match_client_trends_debug(path):
    if path.startswith("/api/client/") and path.endswith("/trends-debug"):
        client_id = path.split("/")[3]
        return True, {"client_id": client_id}
    return False, {}


def _match_client_trends(path):
    if path.startswith("/api/client/") and path.endswith("/trends"):
        client_id = path.split("/")[3]
        return True, {"client_id": client_id}
    return False, {}


def get_client_accounts(client_id, **kwargs):
    from dashboard import api_client_accounts
    return api_client_accounts(client_id)


def get_client_trends_debug(client_id, **kwargs):
    from dashboard import debug_client_trends
    return debug_client_trends(client_id)


def get_client_trends(client_id, **kwargs):
    from dashboard import api_client_trends
    params = kwargs.get("_params", {})
    days = int(params.get("days", [30])[0])
    return api_client_trends(client_id, days)


def post_assign(body, handler, **kwargs):
    from dashboard import assign_accounts_to_client
    account_ids = body.get("account_ids", [])
    client_id = body.get("client_id")
    if not account_ids or not client_id:
        return {"error": "account_ids and client_id required"}
    return assign_accounts_to_client(account_ids, client_id)


def post_pause_monitor(body, handler, **kwargs):
    client_name = body.get("client_name", "")
    paused = body.get("paused", True)
    if not client_name:
        return {"error": "client_name required"}
    state = store.get_state("paused_clients") or {"clients": []}
    clients_list = state.get("clients", [])
    if paused and client_name not in clients_list:
        clients_list.append(client_name)
    elif not paused and client_name in clients_list:
        clients_list.remove(client_name)
    store.set_state("paused_clients", {"clients": clients_list})
    return {"ok": True, "paused_clients": clients_list}


def post_archive(body, handler, **kwargs):
    client_name = body.get("client_name", "")
    archived = body.get("archived", True)
    if not client_name:
        return {"error": "client_name required"}
    state = store.get_state("archived_clients") or {"clients": []}
    clients_list = state.get("clients", [])
    if archived and client_name not in clients_list:
        clients_list.append(client_name)
        pause_state = store.get_state("paused_clients") or {"clients": []}
        pause_list = pause_state.get("clients", [])
        if client_name not in pause_list:
            pause_list.append(client_name)
            store.set_state("paused_clients", {"clients": pause_list})
    elif not archived and client_name in clients_list:
        clients_list.remove(client_name)
    store.set_state("archived_clients", {"clients": clients_list})
    invalidate_cache()
    return {"ok": True, "archived_clients": clients_list}


def post_set_target_volume(body, handler, **kwargs):
    client_name = body.get("client_name", "")
    volume = body.get("target_volume", 0)
    if not client_name:
        return {"error": "client_name required"}
    targets = store.get_state("target_volumes") or {}
    targets[client_name] = int(volume)
    store.set_state("target_volumes", targets)
    return {"ok": True, "client_name": client_name, "target_volume": int(volume)}


GET_ROUTES = [
    (_match_client_accounts, get_client_accounts),
    (_match_client_trends_debug, get_client_trends_debug),
    (_match_client_trends, get_client_trends),
]

POST_ROUTES = [
    ("/api/assign", post_assign),
    ("/api/client/pause-monitor", post_pause_monitor),
    ("/api/client/archive", post_archive),
    ("/api/client/set-target-volume", post_set_target_volume),
]
