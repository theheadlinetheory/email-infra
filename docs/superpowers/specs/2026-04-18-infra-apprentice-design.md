# Infrastructure Apprentice — Design Spec

## Goal

Formalize a learning layer on top of existing Claude Code sessions so that every infrastructure decision is captured, patterns are codified into a playbook, and the system proactively surfaces recommendations based on observed patterns. Phase C (advisory only) — no autonomous actions.

## Architecture

### 1. Playbook (`/memory/infra_playbook.md`)

Living rulebook built from observed decisions. Sections:

- **Decision Rules** — Codified if/then patterns with thresholds
  - "When deliverability drops below 85% on 3+ accounts in a group, recommend rotation"
  - "When warmup hits 100% on a B group, recommend swapping to active"
  - "When an account goes offline (smtp_failure), flag immediately"
- **Operational Patterns** — Standard procedures
  - "Always check warmup % before assigning a generic group to a new client"
  - "Always verify Zapmail domain is ACTIVE before exporting to SmartLead"
  - "Never assign accounts to a campaign if any account in the group is below 14 days warmup"
- **Red Flags** — Anomalies to watch for
  - "Accounts changing client_id without a corresponding inbox_history dashboard event = external change"
  - "Campaign with 0 accounts = something got unassigned unexpectedly"
  - "Domain health score below 70 = investigate immediately"
- **Client-Specific Knowledge** — Per-client rules and state
  - "Kay's Landscaping: A/B rotation model, 14 domains per group"
  - "Borja/Canopy: generic group infrastructure, turf*/yardcare* domains"
  - "SR Acquisition: 13 subgroups (A-M), domain-to-group mapping in sr_groups.json"

### 2. Decision Log (`/memory/infra_decision_log.md`)

Timestamped append-only record of observed decisions:

```
### 2026-04-18 — Borja/Canopy Account Recovery
- **Situation:** Borja (350067) and Canopy (350068) showed 0 accounts. Campaigns running on wrong client_id (328152).
- **Investigation:** Checked campaign accounts, found all 102 accounts tagged correctly but client_id was Acquisition Inboxes instead of Borja/Canopy.
- **Action:** Reassigned 51 accounts to Borja, 51 to Canopy via save-management-details, preserving existing tags.
- **Root cause:** Previous bulk operation overwrote client_id. Exact script unknown.
- **Rule learned:** Always verify client_id matches tags after any bulk operation. → Added to playbook.
```

### 3. Session Protocol

Every infrastructure session follows this flow:

**Start:**
- Read `infra_playbook.md` and recent entries in `infra_decision_log.md`
- Check inbox_history for any snapshot-detected anomalies since last session
- Surface any pending recommendations

**During:**
- Log all infrastructure actions silently (no extra output unless relevant)
- When a new pattern emerges, note it for playbook update
- Proactively recommend based on learned patterns: "Last time this happened you did X — want me to do that?"

**End:**
- Append new decisions to decision log with situation/action/reasoning
- Update playbook if new rules solidified
- Flag anything that needs follow-up next session

### 4. Dashboard Integration

Already built:
- `inbox_history` table logs all dashboard mutations (client changes, campaign assign/unassign, deletes)
- Daily snapshot comparison catches external changes
- `/api/inbox-history` and `/api/snapshot` endpoints

To add:
- Snapshot anomaly detection: when diffs are found, classify severity and queue a notification
- Dashboard notification panel (deferred — Slack first)

### 5. Slack Notifications

Channel: `#infra-bot`

Message types:
- **Anomaly alerts:** "Snapshot detected 3 accounts changed client_id outside the dashboard"
- **Recommendations:** "Kay's A group warmup hit 100% — recommend rotating to active"
- **Health warnings:** "Domain yardcarepoint.co deliverability dropped to 72%"
- **Session summaries:** "Session ended — 2 new rules added to playbook, 1 client reassignment logged"

Implementation: Use existing Slack MCP tools to post messages. Triggered by snapshot comparisons and health checks.

## What This Is NOT

- Not an autonomous agent (Phase C — advisory only)
- Not a separate hosted process (runs within Claude Code sessions)
- Not replacing the dashboard (augments it with intelligence)
- Not making decisions (recommends, you approve)

## Graduation Path

Once the playbook is comprehensive and recommendations are consistently correct:
1. **Phase B:** Bot executes routine operations autonomously (rotations, health responses) with approval for high-impact actions
2. **Phase A:** Full autonomy with daily summary. Requires a hosted always-on agent (separate design).

## Implementation Scope

Phase 1 (now):
- Create playbook and decision log memory files
- Seed playbook with all known rules from existing memory files and this session's learnings
- Start following session protocol immediately
- Hook Slack notifications into snapshot anomaly detection

Phase 2 (next):
- Dashboard notification panel for recommendations
- Automated health score monitoring with Slack alerts
- Pattern matching on decision log to auto-suggest playbook rules
