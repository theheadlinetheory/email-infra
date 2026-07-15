"""Reserve replenishment planner (DRY-RUN).

Keeps the warmed reserve above a target so replacements never run dry. Computes
what to buy and the cost — it does NOT purchase anything. Execution reuses the
existing provisioning (create_generic_groups.py): connect domains -> buy Zapmail
mailbox slots ($3 ea) -> create 3 mailboxes/domain -> export to SmartLead -> tag
-> enable the 14-day warmup. That pipeline is long-running (DNS waits, warmup),
so it runs as a worker/script, not from a serverless request.
"""

from __future__ import annotations

import json
import math
import os

import db as store
import health_replace as hr

INBOXES_PER_DOMAIN = 3
MAILBOX_COST = 3          # $/mailbox/mo (Zapmail Pro plan)
DOMAIN_COST = 3           # ~$ one-time .info registration
DEFAULT_TARGET = 250      # keep the reserve around this many warmed inboxes


def _ready_domains() -> int:
    """Pre-purchased .info/.co domains sitting in the ready pool (no buy needed)."""
    rows = store._request("GET", "/state", params={
        "select": "data", "key": "eq.domain_ready_pool"})
    if not rows:
        return 0
    try:
        pool = json.loads(rows[0]["data"]).get("domains", [])
    except Exception:
        return 0
    return sum(1 for d in pool if str(d.get("domain", "")).endswith((".info", ".co")))


def _wallet_balance():
    import requests
    try:
        h = {"Content-Type": "application/json",
             "x-auth-zapmail": os.environ.get("ZAPMAIL_API_KEY", "").strip(),
             "x-service-provider": "GOOGLE"}
        w = requests.get("https://api.zapmail.ai/api/v2/wallet/balance", headers=h, timeout=20).json()
        return w.get("walletBalance"), w.get("autoRechargeEnabled")
    except Exception:
        return None, None


def plan_replenish(target: int = DEFAULT_TARGET) -> dict:
    """Dry-run plan to bring the warmed reserve up to `target`."""
    rs = hr.reserve_summary()
    reserve = rs["available"]
    deficit = max(0, target - reserve)
    domains_needed = math.ceil(deficit / INBOXES_PER_DOMAIN)
    ready = _ready_domains()
    domains_to_buy = max(0, domains_needed - ready)
    mailbox_cost = deficit * MAILBOX_COST          # recurring, $3/mailbox/mo
    domain_cost = domains_to_buy * DOMAIN_COST      # one-time registration
    wallet, auto = _wallet_balance()

    return {
        "reserve": reserve,
        "target": target,
        "deficit": deficit,
        "inboxes_to_create": deficit,
        "domains_needed": domains_needed,
        "ready_domains": ready,
        "domains_to_buy": domains_to_buy,
        "mailbox_cost_mo": mailbox_cost,
        "domain_cost_onetime": domain_cost,
        "wallet_balance": wallet,
        "auto_recharge": auto,
        "wallet_covers_mailboxes": (wallet is None) or bool(auto) or (wallet >= mailbox_cost),
        "warmup_days": hr.WARMUP_DAYS,
        "note": "DRY-RUN — nothing bought. On confirm, the provisioning pipeline "
                "buys mailbox slots, creates inboxes, and starts the 14-day warmup.",
    }
