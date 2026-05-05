"""SSE streaming operation routes: assign-client, delete-infra, transition."""

import json


def post_assign_client(body, handler, **kwargs):
    from dashboard import assign_client_sse
    pipeline_id = body.get("pipeline_id")
    client_name = body.get("client_name", "").strip()
    forwarding_domain = body.get("forwarding_domain", "").strip()
    is_new_client = body.get("is_new_client", False)
    if not pipeline_id or not client_name or not forwarding_domain:
        handler._error(400, "pipeline_id, client_name, and forwarding_domain required")
        return None
    handler.stream_sse(assign_client_sse(pipeline_id, client_name, forwarding_domain, is_new_client))
    return None


def post_delete_infra(body, handler, **kwargs):
    from dashboard import delete_client_infra_sse
    client_id = body.get("client_id")
    client_name = body.get("client_name", "").strip()
    if not client_id or not client_name:
        handler._error(400, "client_id and client_name required")
        return None
    handler.stream_sse(delete_client_infra_sse(client_id, client_name))
    return None


def post_transition(body, handler, **kwargs):
    from dashboard import transition_client_sse
    client_id = body.get("client_id")
    client_name = body.get("client_name", "").strip()
    new_client_name = body.get("new_client_name", "").strip()
    forwarding_domain = body.get("forwarding_domain", "").strip()
    is_new_client = body.get("is_new_client", False)
    if not client_id or not client_name or not new_client_name or not forwarding_domain:
        handler._error(400, "client_id, client_name, new_client_name, and forwarding_domain required")
        return None
    handler.stream_sse(transition_client_sse(client_id, client_name, new_client_name, forwarding_domain, is_new_client))
    return None


GET_ROUTES = []

POST_ROUTES = [
    ("/api/pipeline/assign-client", post_assign_client),
    ("/api/client/delete-infra", post_delete_infra),
    ("/api/client/transition", post_transition),
]
