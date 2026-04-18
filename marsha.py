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
from datetime import datetime

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
