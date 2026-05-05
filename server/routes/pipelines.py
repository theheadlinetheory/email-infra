"""Pipeline routes: /api/pipeline/*, /api/setup-pipeline/*."""

import json
import os
import db as store
import pipeline_engine


def _match_pipeline_detail(path):
    if path.startswith("/api/pipeline/") and len(path.split("/")) == 4:
        pid = path.split("/")[3]
        if pid not in ("active",):
            return True, {"pipeline_id": pid}
    return False, {}


def _match_setup_pipeline_detail(path):
    if path.startswith("/api/setup-pipeline/") and not path.endswith("/create") and not path.endswith("/retry"):
        pid = path.split("/")[-1]
        return True, {"pipeline_id": pid}
    return False, {}


def get_pipeline_active(**kwargs):
    from dashboard import api_pipeline_active
    return api_pipeline_active()


def get_pipeline_detail(pipeline_id, **kwargs):
    from dashboard import api_pipeline_detail
    return api_pipeline_detail(pipeline_id)


def get_setup_pipelines(**kwargs):
    pipelines = store.list_setup_pipelines()
    return {"pipelines": pipelines}


def get_setup_pipeline_detail(pipeline_id, **kwargs):
    p = store.get_setup_pipeline(pipeline_id)
    if p:
        return p
    return {"error": "not found"}


def get_next_generic_name(**kwargs):
    from dashboard import next_generic_name
    return {"name": next_generic_name()}


def get_generic_groups_status(**kwargs):
    script_dir = os.path.dirname(os.path.dirname(__file__))
    status_file = os.path.join(script_dir, "generic_groups_status.json")
    state_file = os.path.join(script_dir, "generic_groups_state.json")
    result = {"running": False, "step": "unknown", "progress": 0, "detail": "", "completed_steps": []}
    if os.path.exists(state_file):
        with open(state_file) as f:
            state = json.load(f)
        result["completed_steps"] = state.get("completed_steps", [])
    if os.path.exists(status_file):
        with open(status_file) as f:
            status = json.load(f)
        result.update(status)
        result["running"] = status.get("step") != "complete"
    return result


def post_pipeline_new_client(body, handler, **kwargs):
    from dashboard import api_pipeline_new_client
    return api_pipeline_new_client(body)


def post_pipeline_replacement(body, handler, **kwargs):
    from dashboard import api_pipeline_replacement
    return api_pipeline_replacement(body)


def post_pipeline_new_acquisition(body, handler, **kwargs):
    from dashboard import api_pipeline_new_acquisition
    return api_pipeline_new_acquisition(body)


def post_pipeline_retry(body, handler, **kwargs):
    from dashboard import api_pipeline_retry
    return api_pipeline_retry(body)


def post_pipeline_skip_step(body, handler, **kwargs):
    from dashboard import api_pipeline_skip_step
    return api_pipeline_skip_step(body)


def post_setup_pipeline_create(body, handler, **kwargs):
    from dashboard import build_pipeline_config
    config = build_pipeline_config(body)
    pid = pipeline_engine.create_and_start(
        body.get("name", ""), body.get("type", "generic"), config
    )
    return {"id": pid, "status": "running"}


def post_setup_pipeline_retry(body, handler, **kwargs):
    pid = body.get("pipeline_id", "")
    ok = pipeline_engine.retry_failed_step(pid)
    return {"ok": ok}


GET_ROUTES = [
    ("/api/pipeline/active", get_pipeline_active),
    (_match_pipeline_detail, get_pipeline_detail),
    ("/api/setup-pipelines", get_setup_pipelines),
    (_match_setup_pipeline_detail, get_setup_pipeline_detail),
    ("/api/next-generic-name", get_next_generic_name),
    ("/api/generic-groups-status", get_generic_groups_status),
]

POST_ROUTES = [
    ("/api/pipeline/new-client", post_pipeline_new_client),
    ("/api/pipeline/replacement", post_pipeline_replacement),
    ("/api/pipeline/new-acquisition", post_pipeline_new_acquisition),
    ("/api/pipeline/retry", post_pipeline_retry),
    ("/api/pipeline/skip-step", post_pipeline_skip_step),
    ("/api/setup-pipeline/create", post_setup_pipeline_create),
    ("/api/setup-pipeline/retry", post_setup_pipeline_retry),
]
