"""Domain routes: /api/domains, /api/domains/auto-renew, /api/domains/bulk-auto-renew."""

import time


def get_domains(**kwargs):
    from dashboard import api_domains
    return api_domains()


def post_auto_renew(body, handler, **kwargs):
    from dashboard import porkbun_set_auto_renew, spaceship_set_auto_renew
    domain = body.get("domain", "")
    registrar = body.get("registrar", "").lower()
    enabled = body.get("enabled", False)
    if not domain or not registrar:
        return {"error": "domain and registrar required"}
    if registrar == "porkbun":
        return porkbun_set_auto_renew(domain, enabled)
    elif registrar == "spaceship":
        return spaceship_set_auto_renew(domain, enabled)
    return {"success": False, "message": f"{registrar} auto-renew toggle not supported via API"}


def post_bulk_auto_renew(body, handler, **kwargs):
    from dashboard import porkbun_set_auto_renew, spaceship_set_auto_renew
    domains = body.get("domains", [])
    enabled = body.get("enabled", False)
    if not domains:
        return {"error": "domains list required"}

    results = {"success": 0, "failed": 0, "errors": []}
    for d in domains:
        name = d.get("domain", "")
        reg = d.get("registrar", "").lower()
        try:
            if reg == "spaceship":
                r = spaceship_set_auto_renew(name, enabled)
            elif reg == "porkbun":
                r = porkbun_set_auto_renew(name, enabled)
            else:
                r = {"success": False, "message": "unsupported"}
            if r["success"]:
                results["success"] += 1
            else:
                results["failed"] += 1
                if len(results["errors"]) < 5:
                    results["errors"].append(f"{name}: {r.get('message', '')}")
        except Exception as e:
            results["failed"] += 1
            if len(results["errors"]) < 5:
                results["errors"].append(f"{name}: {e}")
        time.sleep(0.5)
    return results


GET_ROUTES = [
    ("/api/domains", get_domains),
]

POST_ROUTES = [
    ("/api/domains/auto-renew", post_auto_renew),
    ("/api/domains/bulk-auto-renew", post_bulk_auto_renew),
]
