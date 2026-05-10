"""Marsha — THT Infrastructure Apprentice.

Friendly infrastructure watchdog that monitors inbox health, detects anomalies,
and posts observations to Slack. Phase C: advisory only, no autonomous actions.

Personality: Big, warm, middle-aged Black woman who keeps the infrastructure
running smooth. Speaks plainly, celebrates wins, flags concerns early.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import db as store
import inbox_history

log = logging.getLogger("marsha")

# Slack config — set MARSHA_SLACK_CHANNEL to the channel ID
SLACK_WEBHOOK = os.environ.get("MARSHA_SLACK_WEBHOOK", "")
MARSHA_CHANNEL_ID = os.environ.get("MARSHA_SLACK_CHANNEL", "")


# ---------------------------------------------------------------------------
# Personality
# ---------------------------------------------------------------------------

def _greet():
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning, baby!"
    elif hour < 17:
        return "Hey there, sugar!"
    return "Evening, hon!"


def _format_anomaly_message(events):
    """Format snapshot anomalies into a Marsha-style Slack message."""
    if not events:
        return None

    client_changes = [e for e in events if e["event_type"] == "client_change"]
    new_accounts = [e for e in events if e["event_type"] == "snapshot_new"]
    deleted = [e for e in events if e["event_type"] == "snapshot_deleted"]

    lines = [f"{_greet()} Marsha here with an update.\n"]

    if client_changes:
        lines.append(f":rotating_light: **{len(client_changes)} account(s) changed client** outside the dashboard:")
        for e in client_changes[:10]:
            old_cid = e.get("old_value", {}).get("client_id", "?")
            new_cid = e.get("new_value", {}).get("client_id", "?")
            lines.append(f"  - `{e['email']}` moved from client `{old_cid}` to `{new_cid}`")
        if len(client_changes) > 10:
            lines.append(f"  ...and {len(client_changes) - 10} more")
        lines.append("")

    if new_accounts:
        lines.append(f":new: **{len(new_accounts)} new account(s)** showed up:")
        for e in new_accounts[:5]:
            lines.append(f"  - `{e['email']}` (client `{e.get('new_value', {}).get('client_id', '?')}`)")
        if len(new_accounts) > 5:
            lines.append(f"  ...and {len(new_accounts) - 5} more")
        lines.append("")

    if deleted:
        lines.append(f":wastebasket: **{len(deleted)} account(s) disappeared:**")
        for e in deleted[:5]:
            lines.append(f"  - `{e['email']}` (was client `{e.get('old_value', {}).get('client_id', '?')}`)")
        if len(deleted) > 5:
            lines.append(f"  ...and {len(deleted) - 5} more")
        lines.append("")

    if len(lines) <= 1:
        return None

    lines.append("Check the inbox history if you need the full picture. I'm keeping an eye on things. :eyes:")
    return "\n".join(lines)


def _format_health_alert(issues):
    """Format health issues into a Marsha-style message."""
    lines = [f"{_greet()} I noticed some health concerns:\n"]
    for issue in issues[:10]:
        lines.append(f":warning: `{issue['email']}` — {issue['detail']}")
    if len(issues) > 10:
        lines.append(f"...and {len(issues) - 10} more")
    lines.append("\nMight want to take a look when you get a chance, sugar.")
    return "\n".join(lines)


def _format_session_summary(decisions):
    """Format end-of-session summary."""
    lines = [f"{_greet()} Here's what happened this session:\n"]
    for d in decisions:
        lines.append(f"- {d}")
    lines.append("\nEverything's logged in the decision log. Have a good one! :wave:")
    return "\n".join(lines)


def _format_all_clear(account_count):
    """When snapshot finds no issues."""
    return f"{_greet()} Just ran my check — all **{account_count}** accounts looking good. Nothing out of place. :white_check_mark:"


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------

def post_to_slack(message):
    """Post a message to Marsha's Slack channel. Returns True on success."""
    if not MARSHA_CHANNEL_ID:
        log.warning("MARSHA_SLACK_CHANNEL not set — message not sent")
        return False

    # Use the Slack MCP integration via dashboard API
    # For now, store messages for retrieval; Slack posting happens via MCP tools
    # in Claude Code sessions or via webhook
    if SLACK_WEBHOOK:
        try:
            r = requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
            return r.status_code == 200
        except Exception as e:
            log.warning("Slack webhook failed: %s", e)
            return False

    # Fallback: store message for next session to pick up
    store.set_state("marsha_pending_messages", {
        "messages": _get_pending_messages() + [{"text": message, "ts": datetime.now().isoformat()}],
    })
    return True


def _get_pending_messages():
    """Get messages queued for Slack delivery."""
    state = store.get_state("marsha_pending_messages")
    if state:
        return state.get("messages", [])
    return []


def flush_pending_messages():
    """Get and clear any pending messages (called from Claude Code sessions)."""
    msgs = _get_pending_messages()
    if msgs:
        store.set_state("marsha_pending_messages", {"messages": []})
    return msgs


# ---------------------------------------------------------------------------
# Core checks
# ---------------------------------------------------------------------------

def run_snapshot_check(accounts):
    """Run snapshot, post to Slack if anomalies found."""
    result = inbox_history.snapshot_inboxes(accounts)
    diffs = result.get("diffs", 0)
    account_count = result.get("accounts", 0)

    if diffs > 0:
        # Fetch the events that were just logged
        recent = store.get_inbox_history(limit=diffs + 5)
        snapshot_events = [e for e in recent if e.get("source") == "snapshot"][:diffs]
        msg = _format_anomaly_message(snapshot_events)
        if msg:
            post_to_slack(msg)
            log.info("Marsha posted anomaly alert: %d diffs", diffs)
    else:
        log.info("Marsha snapshot clean: %d accounts, 0 diffs", account_count)

    return result


def post_health_alerts(issues):
    """Post health concerns to Slack."""
    if issues:
        msg = _format_health_alert(issues)
        post_to_slack(msg)


def post_session_summary(decisions):
    """Post end-of-session summary to Slack."""
    if decisions:
        msg = _format_session_summary(decisions)
        post_to_slack(msg)


def post_custom(message):
    """Post a custom Marsha-voiced message."""
    post_to_slack(f"{_greet()} {message}")


# ---------------------------------------------------------------------------
# Campaign-tag audit
# ---------------------------------------------------------------------------

# Client tag name → SmartLead tag ID (must match reference_smartlead_tags.md)
CLIENT_TAGS = {
    "Tropical Landscaping": 373673,
    "Dallas Land Care": 317210,
    "Lightning Lawn Care": 300713,
    "Coastal Lawn Care": 300714,
    "Kay's Landscaping B": 374181,
    "Timesavers Landscaping": 348097,
    "Pioneer Landscaping": 356894,
    "GM Landscaping & Design": 356893,
    "Canopy Land Solutions": 370043,
    "Borja Landscaping Construction": 370045,
    "Denair HVAC": 382356,
    "Lawnvalue": 392880,
}

SMARTLEAD_API_KEY = os.environ.get("SMARTLEAD_API_KEY", "")
SMARTLEAD_JWT = os.environ.get("SMARTLEAD_JWT", "")
GQL_URL = os.environ.get("SMARTLEAD_GQL", "https://fe-gql.smartlead.ai/v1/graphql")
SL_BASE = "https://server.smartlead.ai/api"


def _gql_tagged_ids(tag_id):
    """Get all email_account_ids with a given tag via GraphQL."""
    query = (
        "{ email_account_tag_mappings(where: {tag_id: {_eq: %d}}) "
        "{ email_account_id } }" % tag_id
    )
    r = requests.post(
        GQL_URL,
        headers={"Authorization": f"Bearer {SMARTLEAD_JWT}"},
        json={"query": query},
        timeout=30,
    )
    data = r.json()
    return set(
        m["email_account_id"]
        for m in data.get("data", {}).get("email_account_tag_mappings", [])
    )


def _campaign_account_ids(campaign_id):
    """Get all email_account_ids on a campaign."""
    r = requests.get(
        f"{SL_BASE}/v1/campaigns/{campaign_id}/email-accounts",
        params={"api_key": SMARTLEAD_API_KEY},
        timeout=30,
    )
    return set(a["id"] for a in r.json())


def _active_client_campaigns():
    """Return list of active campaigns whose name contains 'client'."""
    r = requests.get(
        f"{SL_BASE}/v1/campaigns",
        params={"api_key": SMARTLEAD_API_KEY},
        timeout=30,
    )
    return [
        c for c in r.json()
        if c.get("status") == "ACTIVE" and "client" in c.get("name", "").lower()
    ]


def _match_campaign_to_tag(campaign_name):
    """Match a campaign name to a client tag name. Returns (tag_name, tag_id) or None."""
    name_lower = campaign_name.lower()
    for tag_name, tag_id in CLIENT_TAGS.items():
        if tag_name.lower().split()[0] in name_lower:
            return tag_name, tag_id
    return None


MAX_CLIENT_VOLUME = 750  # Max emails/day per client campaign before flagging
WARMUP_DAYS = 14  # Days required before an account is campaign-ready


def _get_warmup_ready_ids(tag_id):
    """Get account IDs with this tag that have completed warmup (14+ days).

    Uses the date tag (M/D/YY format) to determine warmup start date.
    Accounts without a parseable date tag are assumed ready.
    """
    query = (
        "{ email_account_tag_mappings(where: {tag_id: {_eq: %d}}) {"
        " email_account_id email_account {"
        " email_account_tag_mappings { tag { name } } } } }" % tag_id
    )
    r = requests.post(
        GQL_URL,
        headers={"Authorization": f"Bearer {SMARTLEAD_JWT}"},
        json={"query": query},
        timeout=30,
    )
    data = r.json()
    mappings = data.get("data", {}).get("email_account_tag_mappings", [])

    cutoff = datetime.now() - timedelta(days=WARMUP_DAYS)
    ready = set()
    not_ready = set()

    for m in mappings:
        aid = m["email_account_id"]
        all_tags = m.get("email_account", {}).get("email_account_tag_mappings", [])
        found_date = False
        for t in all_tags:
            try:
                dt = datetime.strptime(t["tag"]["name"], "%m/%d/%y")
                found_date = True
                if dt <= cutoff:
                    ready.add(aid)
                else:
                    not_ready.add(aid)
                break
            except ValueError:
                pass
        if not found_date:
            ready.add(aid)  # No date tag = assume ready

    return ready, not_ready


def run_campaign_audit(fix=False):
    """Audit all active client campaigns for tag-account alignment.

    Only adds accounts whose warmup is complete (14+ days).
    Never removes accounts without flagging for manual review.
    Flags campaigns exceeding MAX_CLIENT_VOLUME.
    """
    campaigns = _active_client_campaigns()
    issues = []
    fixes = []
    volume_warnings = []
    untagged_issues = []
    warmup_on_campaign = []

    # Caches to avoid redundant API calls
    ready_cache = {}
    not_ready_cache = {}
    all_tagged_cache = {}
    campaign_acct_cache = {}

    for c in campaigns:
        cid = c["id"]
        cname = c["name"]
        match = _match_campaign_to_tag(cname)
        if not match:
            log.warning("Campaign '%s' has no matching client tag", cname)
            continue

        tag_name, tag_id = match

        if tag_id not in ready_cache:
            ready, not_ready = _get_warmup_ready_ids(tag_id)
            ready_cache[tag_id] = ready
            not_ready_cache[tag_id] = not_ready
            all_tagged_cache[tag_id] = ready | not_ready

        ready = ready_cache[tag_id]
        not_ready = not_ready_cache[tag_id]
        all_tagged = all_tagged_cache[tag_id]

        if cid not in campaign_acct_cache:
            time.sleep(0.5)  # Rate limit protection
            campaign_acct_cache[cid] = _campaign_account_ids(cid)
        on_campaign = campaign_acct_cache[cid]

        missing_ready = ready - on_campaign
        volume = len(on_campaign) * 15

        if missing_ready:
            new_volume = (len(on_campaign) + len(missing_ready)) * 15
            issues.append({
                "campaign": cname,
                "campaign_id": cid,
                "client_tag": tag_name,
                "on_campaign": len(on_campaign),
                "ready_total": len(ready),
                "not_ready_total": len(not_ready),
                "missing": len(missing_ready),
                "volume_before": volume,
                "volume_after": new_volume,
            })

            if fix:
                try:
                    r = requests.post(
                        f"{SL_BASE}/v1/campaigns/{cid}/email-accounts",
                        params={"api_key": SMARTLEAD_API_KEY},
                        json={"email_account_ids": list(missing_ready)},
                        timeout=60,
                    )
                    if r.status_code == 200:
                        fixes.append(
                            f"{cname}: added {len(missing_ready)} warmed accounts "
                            f"({volume}/day → {new_volume}/day)"
                        )
                    else:
                        fixes.append(
                            f"{cname}: FAILED to add accounts ({r.status_code})"
                        )
                except Exception as e:
                    fixes.append(f"{cname}: ERROR — {e}")

        # Flag if volume exceeds cap
        current_or_new = (len(on_campaign) + len(missing_ready)) * 15 if missing_ready else volume
        if current_or_new > MAX_CLIENT_VOLUME:
            volume_warnings.append({
                "campaign": cname,
                "volume": current_or_new,
                "accounts": len(on_campaign) + (len(missing_ready) if missing_ready else 0),
            })

        # Check for untagged accounts on this campaign
        extra = on_campaign - all_tagged
        if extra:
            untagged_issues.append({
                "campaign": cname,
                "campaign_id": cid,
                "client_tag": tag_name,
                "untagged_count": len(extra),
                "untagged_ids": list(extra)[:10],
            })

        # Check for not-ready accounts on this campaign
        still_warming = on_campaign & not_ready
        if still_warming:
            warmup_on_campaign.append({
                "campaign": cname,
                "campaign_id": cid,
                "count": len(still_warming),
                "ids": list(still_warming)[:10],
            })

    return {
        "issues": issues,
        "fixes": fixes,
        "untagged": untagged_issues,
        "volume_warnings": volume_warnings,
        "warmup_on_campaign": warmup_on_campaign,
    }


def _format_campaign_audit(result):
    """Format campaign audit results into a Marsha-style message."""
    issues = result["issues"]
    fixes = result["fixes"]

    if not issues and not fixes:
        return (
            f"{_greet()} Just finished my campaign audit — "
            "every client campaign has all its tagged accounts assigned. "
            "We're running at full capacity, baby! :white_check_mark:"
        )

    lines = [f"{_greet()} I ran my campaign audit and found some gaps:\n"]

    if fixes:
        lines.append(":wrench: *Fixed automatically:*")
        for f in fixes:
            lines.append(f"  • {f}")
        lines.append("")
    elif issues:
        lines.append(":warning: *Campaigns with missing accounts:*")
        for i in issues:
            lines.append(
                f"  • *{i['campaign']}* — {i['on_campaign']}/{i['tagged_total']} "
                f"accounts ({i['volume_before']}/day, should be {i['volume_after']}/day)"
            )
        lines.append("")

    untagged = result.get("untagged", [])
    if untagged:
        lines.append(":mag: *Accounts on campaigns WITHOUT the client tag:*")
        for u in untagged:
            lines.append(
                f"  • *{u['campaign']}* — {u['untagged_count']} account(s) missing the "
                f"`{u['client_tag']}` tag"
            )
        lines.append("")

    volume_warnings = result.get("volume_warnings", [])
    if volume_warnings:
        lines.append(f":chart_with_upwards_trend: *Campaigns above {MAX_CLIENT_VOLUME}/day:*")
        for v in volume_warnings:
            lines.append(
                f"  • *{v['campaign']}* — {v['accounts']} accounts, "
                f"{v['volume']}/day"
            )
        lines.append("")

    warmup = result.get("warmup_on_campaign", [])
    if warmup:
        lines.append(":hourglass_flowing_sand: *Still-warming accounts on live campaigns (should not be sending):*")
        for w in warmup:
            lines.append(
                f"  • *{w['campaign']}* — {w['count']} account(s) still in warmup"
            )
        lines.append("")

    lines.append("Tag your accounts, assign your campaigns — that's the rule, sugar. :eyes:")
    return "\n".join(lines)


def sync_b_group_state():
    """Refresh b_group_assignments.json from live SmartLead data."""
    state_path = Path(__file__).parent / "clients" / "b_group_assignments.json"
    if not state_path.exists():
        return
    assignments = json.loads(state_path.read_text())
    updated = False

    for group_name, info in assignments.items():
        tag_id = info.get("generic_tag_id")
        if not tag_id:
            continue
        tagged_ids = _gql_tagged_ids(tag_id)
        new_tags_applied = len(tagged_ids)
        if new_tags_applied != info.get("tags_applied"):
            info["tags_applied"] = new_tags_applied
            updated = True

        warmup_count = 0
        for acc_id in tagged_ids:
            try:
                r = requests.get(
                    f"{SL_BASE}/email-account/fetch-warmup-details-by-email-account-id/{acc_id}",
                    headers={"Authorization": f"Bearer {SMARTLEAD_JWT}"},
                    timeout=15,
                )
                if r.status_code == 200:
                    wd = r.json().get("message", {})
                    if wd.get("status") == "ACTIVE":
                        warmup_count += 1
            except Exception:
                pass
            time.sleep(0.05)

        if warmup_count != info.get("warmup_enabled"):
            info["warmup_enabled"] = warmup_count
            updated = True

    if updated:
        state_path.write_text(json.dumps(assignments, indent=2))
        log.info("Synced b_group_assignments.json from live SmartLead data")


def run_daily_audit(fix=False):
    """Run the full daily audit and post results to Slack."""
    log.info("Marsha daily audit starting")
    try:
        sync_b_group_state()
    except Exception as e:
        log.warning("State sync failed (non-fatal): %s", e)
    result = run_campaign_audit(fix=fix)
    msg = _format_campaign_audit(result)
    post_to_slack(msg)
    log.info("Marsha daily audit complete: %d issues, %d fixes", len(result["issues"]), len(result["fixes"]))
    return result
