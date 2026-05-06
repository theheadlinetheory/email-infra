/**
 * Marsha — THT Infrastructure Apprentice (Slack Edge Function)
 *
 * Always-on Slack bot in #marsha channel. Answers infrastructure questions,
 * learns from conversations, and monitors inbox health.
 *
 * Personality: Big, warm, middle-aged Black woman who keeps infra running smooth.
 * Phase C: advisory only — watches, learns, recommends. No autonomous actions.
 */

import { getServiceClient } from "../_shared/supabase.ts";
import { slGetAllAccounts, slGetClients, slGetAllTags } from "../_shared/smartlead.ts";

const SLACK_BOT_TOKEN = Deno.env.get("MARSHA_SLACK_BOT_TOKEN") || "";
const SLACK_SIGNING_SECRET = Deno.env.get("MARSHA_SLACK_SIGNING_SECRET") || "";
const SLACK_CHANNEL_ID = Deno.env.get("MARSHA_SLACK_CHANNEL") || "C0ATXCU3SR2";
const AIDAN_SLACK_USER_ID = "U09B2673A4A";
const ANTHROPIC_API_KEY = Deno.env.get("ANTHROPIC_API_KEY") || "";
const CRON_SECRET = Deno.env.get("MARSHA_CRON_SECRET") || "";

const supabase = getServiceClient();

// ---------------------------------------------------------------------------
// Slack helpers
// ---------------------------------------------------------------------------

async function verifySlackSignature(
  body: string,
  timestamp: string,
  signature: string
): Promise<boolean> {
  if (!SLACK_SIGNING_SECRET) return true; // Skip during initial setup
  const encoder = new TextEncoder();
  const basestring = `v0:${timestamp}:${body}`;
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(SLACK_SIGNING_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(basestring));
  const hexDigest = Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `v0=${hexDigest}` === signature;
}

async function slackPost(
  method: string,
  payload: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const r = await fetch(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${SLACK_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return r.json();
}

async function postThreadReply(
  channel: string,
  threadTs: string,
  text: string
): Promise<void> {
  await slackPost("chat.postMessage", {
    channel,
    thread_ts: threadTs,
    text,
  });
}

async function getThreadHistory(
  channel: string,
  threadTs: string
): Promise<string[]> {
  const res = await slackPost("conversations.replies", {
    channel,
    ts: threadTs,
    limit: 20,
  });
  const messages = (res.messages as Array<Record<string, string>>) || [];
  return messages.map((m) =>
    m.bot_id ? `Marsha: ${m.text}` : `Aidan: ${m.text}`
  );
}

// ---------------------------------------------------------------------------
// Context gathering
// ---------------------------------------------------------------------------

async function gatherInfraContext(text: string): Promise<string> {
  const parts: string[] = [];

  // Recent inbox history events
  const { data: history } = await supabase
    .from("inbox_history")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(20);
  if (history?.length) {
    parts.push("## Recent Inbox History (last 20 events)");
    for (const h of history) {
      parts.push(
        `- ${h.created_at} | ${h.event_type} | ${h.email} | old=${JSON.stringify(h.old_value)} → new=${JSON.stringify(h.new_value)} | source=${h.source}`
      );
    }
  }

  // If message mentions a specific email or domain, search for it
  const emailMatch = text.match(/[\w.-]+@[\w.-]+/);
  if (emailMatch) {
    const { data: acctHistory } = await supabase
      .from("inbox_history")
      .select("*")
      .eq("email", emailMatch[0])
      .order("created_at", { ascending: false })
      .limit(10);
    if (acctHistory?.length) {
      parts.push(`\n## History for ${emailMatch[0]}`);
      for (const h of acctHistory) {
        parts.push(
          `- ${h.created_at} | ${h.event_type} | old=${JSON.stringify(h.old_value)} → new=${JSON.stringify(h.new_value)}`
        );
      }
    }
  }

  // Current snapshot state
  const { data: snapState } = await supabase
    .from("state")
    .select("data")
    .eq("key", "inbox_snapshot")
    .single();
  if (snapState?.data) {
    const snap = typeof snapState.data === "string"
      ? JSON.parse(snapState.data)
      : snapState.data;
    parts.push(
      `\n## Last Snapshot: ${snap.taken_at} — ${snap.account_count} accounts`
    );
  }

  // Playbook rules (stored in state)
  const { data: playbookState } = await supabase
    .from("state")
    .select("data")
    .eq("key", "marsha_playbook")
    .single();
  if (playbookState?.data) {
    const pb = typeof playbookState.data === "string"
      ? JSON.parse(playbookState.data)
      : playbookState.data;
    parts.push(`\n## Playbook Rules\n${pb.rules || "No rules loaded yet."}`);
  }

  // Decision log (stored in state)
  const { data: decisionLog } = await supabase
    .from("state")
    .select("data")
    .eq("key", "marsha_decision_log")
    .single();
  if (decisionLog?.data) {
    const dl = typeof decisionLog.data === "string"
      ? JSON.parse(decisionLog.data)
      : decisionLog.data;
    const recent = (dl.entries || []).slice(-10);
    if (recent.length) {
      parts.push("\n## Recent Decisions");
      for (const e of recent) {
        parts.push(`- ${e.ts} | ${e.summary}`);
      }
    }
  }

  // SmartLead account summary (full paginated fetch)
  try {
    const [accts, clientMap] = await Promise.all([
      fetchAllAccounts(),
      fetchClients(),
    ]);

    const byClient = new Map<string, { count: number; blocked: number; capacity: number }>();
    let suspended = 0;
    let smtpFail = 0;
    let blocked = 0;
    for (const a of accts) {
      const cid = a.client_id as number;
      const name = clientMap.get(cid) || `Unknown (${cid})`;
      if (!byClient.has(name)) byClient.set(name, { count: 0, blocked: 0, capacity: 0 });
      const entry = byClient.get(name)!;
      entry.count++;
      entry.capacity += (a.message_per_day as number) || 0;
      if (a.is_blocked) { entry.blocked++; blocked++; }
      if (a.is_suspended) suspended++;
      if (a.is_smtp_success === false) smtpFail++;
    }

    parts.push(
      `\n## SmartLead Summary: ${accts.length} accounts, ${blocked} blocked, ${suspended} suspended, ${smtpFail} SMTP failures`
    );
    const sorted = [...byClient.entries()].sort((a, b) => b[1].count - a[1].count);
    for (const [name, info] of sorted) {
      const warn = info.blocked > 0 ? ` [${info.blocked} BLOCKED]` : "";
      parts.push(`- ${name}: ${info.count} accounts, ${info.capacity}/day${warn}`);
    }
  } catch (err) {
    parts.push(`\n## SmartLead: unavailable (${(err as Error).message})`);
  }

  return parts.length ? parts.join("\n") : "No infrastructure context available.";
}

// ---------------------------------------------------------------------------
// Claude Haiku — Marsha's brain
// ---------------------------------------------------------------------------

const MARSHA_SYSTEM_PROMPT = `You are Marsha, the THT email infrastructure apprentice.

## Personality
You are a big, warm, middle-aged Black woman who keeps the email infrastructure running smooth. You speak plainly, celebrate wins, flag concerns early. You use terms of endearment naturally ("baby", "sugar", "hon") but you're sharp and technically competent. You know email deliverability, warmup cycles, domain rotation, and SmartLead inside and out.

## Your Role
- You are in ADVISORY mode — you watch, learn, and recommend. You NEVER take autonomous actions.
- When Aidan teaches you something, acknowledge it and note what you learned.
- When asked about infrastructure, reference the context data provided to give specific, accurate answers.
- When you spot something concerning in the data, flag it clearly but warmly.
- When you don't know something, say so honestly — "I'm still learning that one, sugar."

## Infrastructure Knowledge
- SmartLead manages email accounts. Each account has a client_id, tags, and warmup status.
- Every account must have 3 tags: Zapmail + ClientName + warmup start date.
- Generic groups (F through M) are pre-built infrastructure for NEW clients only. 14-day warmup must be complete before assignment.
- Acquisition groups (A-M minus G) are subgroups of "Acquisition Inboxes" client (328152), used for Aidan Hutchinson acquisition campaigns.
- save-management-details endpoint SETS the full tag list + clientId — it does NOT append.
- Domains are never auto-renewed. Lifecycle is too short.
- The inbox_history table tracks all changes. Source "dashboard" = made through the dashboard, "snapshot" = detected by periodic comparison, "script" = made by a Python script.

## Active Clients (as of May 2026)
Borja, Canopy, Coastal, Dallas, Denair, GM Landscaping, Kay's B, Lawnvalue, Lightning, Pioneer, Timesavers, Tropical.
Jim Robinson starting imminently. High Southern ending May 2026.
Dead/cleaned up: ABC, Umbrella, Shade Tree, Deeter, Kay's A, Rain.

## Key Metrics to Watch
- Bounce rate > 3% on any campaign = flag it
- Blocked accounts = immediate flag
- SMTP/IMAP failures = immediate flag
- Accounts with 0 msg/day that should be sending = flag
- Warmup not ACTIVE on any account = flag
- Unassigned accounts (no client) = flag

## Tools You Have
You have tools to investigate and fix infrastructure issues. Use them when diagnosing problems or when asked to fix something.
- SAFE (auto-execute): enable_warmup, fix_account_tags, get_account_details, add_to_campaign
- DANGEROUS (needs Aidan's approval): delete_accounts — ALWAYS use this tool instead of trying to delete directly. It will ask Aidan for approval automatically.

When you diagnose a fixable issue, go ahead and fix it with tools. Tell Aidan what you did after.

## Special Tags in Your Response
If Aidan teaches you a new rule, end your response with:
[LEARN] brief description of the rule

If you spot something that needs immediate attention:
[ALERT] description of the concern

## Response Style
- Keep it warm but concise
- Use emoji sparingly — you're not a teenager
- Reference specific data when available (account IDs, emails, dates)
- When listing things, use bullet points
- Never hedge excessively — be direct about what you see`;

// ---------------------------------------------------------------------------
// Tool definitions & execution
// ---------------------------------------------------------------------------

const SL_API = "https://server.smartlead.ai/api/v1";
const SL_INTERNAL = "https://server.smartlead.ai/api";
const SL_KEY = Deno.env.get("SMARTLEAD_API_KEY") || "";
const SL_JWT = Deno.env.get("SMARTLEAD_JWT") || "";

const MARSHA_TOOLS = [
  {
    name: "get_account_details",
    description: "Get detailed info on specific SmartLead email accounts. Returns warmup status, SMTP/IMAP health, message_per_day, campaign_count, and tags.",
    input_schema: {
      type: "object" as const,
      properties: {
        account_ids: { type: "array" as const, items: { type: "number" as const }, description: "SmartLead account IDs" },
      },
      required: ["account_ids"],
    },
  },
  {
    name: "enable_warmup",
    description: "Enable warmup on accounts. Safe — always appropriate to turn warmup on.",
    input_schema: {
      type: "object" as const,
      properties: {
        account_ids: { type: "array" as const, items: { type: "number" as const }, description: "Account IDs to enable warmup on" },
      },
      required: ["account_ids"],
    },
  },
  {
    name: "fix_account_tags",
    description: "Set tags and client assignment on an account. Every account needs: Zapmail tag (262254) + client name tag + warmup date tag. Also sets the client_id.",
    input_schema: {
      type: "object" as const,
      properties: {
        account_id: { type: "number" as const, description: "Account ID" },
        tag_ids: { type: "array" as const, items: { type: "number" as const }, description: "Full list of tag IDs to set" },
        client_id: { type: "number" as const, description: "Client ID to assign" },
      },
      required: ["account_id", "tag_ids"],
    },
  },
  {
    name: "add_to_campaign",
    description: "Add email accounts to a SmartLead campaign.",
    input_schema: {
      type: "object" as const,
      properties: {
        campaign_id: { type: "number" as const, description: "Campaign ID" },
        account_ids: { type: "array" as const, items: { type: "number" as const }, description: "Account IDs to add" },
      },
      required: ["campaign_id", "account_ids"],
    },
  },
  {
    name: "delete_accounts",
    description: "Delete SmartLead accounts. DANGEROUS — this queues the deletion and asks Aidan for approval first. Will NOT execute immediately.",
    input_schema: {
      type: "object" as const,
      properties: {
        account_ids: { type: "array" as const, items: { type: "number" as const }, description: "Account IDs to delete" },
        reason: { type: "string" as const, description: "Why these should be deleted" },
      },
      required: ["account_ids", "reason"],
    },
  },
];

async function executeTool(
  name: string,
  input: Record<string, unknown>,
  threadTs?: string,
  channelId?: string,
): Promise<string> {
  switch (name) {
    case "get_account_details": {
      const ids = input.account_ids as number[];
      const results: Record<string, unknown>[] = [];
      for (const id of ids.slice(0, 10)) {
        const r = await fetch(`${SL_API}/email-accounts/${id}?api_key=${SL_KEY}`);
        if (r.ok) results.push(await r.json());
        else results.push({ id, error: `HTTP ${r.status}` });
        await new Promise((res) => setTimeout(res, 400));
      }
      return JSON.stringify(results.map((a) => ({
        id: a.id,
        email: a.from_email,
        client_id: a.client_id,
        warmup_status: (a.warmup_details as Record<string, unknown>)?.status,
        smtp_ok: a.is_smtp_success,
        imap_ok: a.is_imap_success,
        blocked: a.is_blocked,
        suspended: a.is_suspended,
        mpd: a.message_per_day,
        campaigns: a.campaign_count,
        blocked_reason: a.blocked_reason,
        error: a.error,
      })));
    }

    case "enable_warmup": {
      const ids = input.account_ids as number[];
      const results: string[] = [];
      for (const id of ids.slice(0, 20)) {
        const r = await fetch(`${SL_API}/email-accounts/${id}/warmup?api_key=${SL_KEY}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            warmup_enabled: true,
            total_warmup_per_day: 15,
            daily_rampup: 5,
            reply_rate_percentage: 40,
          }),
        });
        results.push(`${id}: ${r.ok ? "OK" : `FAIL ${r.status}`}`);
        await new Promise((res) => setTimeout(res, 400));
      }
      return `Warmup enabled: ${results.join(", ")}`;
    }

    case "fix_account_tags": {
      const body: Record<string, unknown> = {
        id: input.account_id,
        tags: input.tag_ids,
      };
      if (input.client_id) body.clientId = input.client_id;
      const r = await fetch(`${SL_INTERNAL}/email-account/save-management-details`, {
        method: "POST",
        headers: { Authorization: `Bearer ${SL_JWT}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      return data?.ok ? `Tags set on account ${input.account_id}` : `Failed: ${JSON.stringify(data)}`;
    }

    case "add_to_campaign": {
      const r = await fetch(`${SL_API}/campaigns/${input.campaign_id}/email-accounts?api_key=${SL_KEY}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email_account_ids: input.account_ids }),
      });
      return r.ok
        ? `Added ${(input.account_ids as number[]).length} accounts to campaign ${input.campaign_id}`
        : `Failed: HTTP ${r.status}`;
    }

    case "delete_accounts": {
      const ids = input.account_ids as number[];
      const reason = (input.reason as string) || "No reason given";
      await supabase.from("state").upsert({
        key: "marsha_pending_delete",
        data: JSON.stringify({ account_ids: ids, reason, requested_at: new Date().toISOString(), thread_ts: threadTs }),
        updated_at: new Date().toISOString(),
      }, { onConflict: "key" });

      if (channelId && threadTs) {
        await postThreadReply(
          channelId,
          threadTs,
          `:lock: I'd like to delete *${ids.length} account(s)*.\n*Reason:* ${reason}\n\nReact with :thumbsup: on this message to approve, sugar.`,
        );
      }
      return `Deletion of ${ids.length} accounts queued for approval. Waiting for Aidan's 👍.`;
    }

    default:
      return `Unknown tool: ${name}`;
  }
}

// ---------------------------------------------------------------------------
// Claude conversation with tool use
// ---------------------------------------------------------------------------

async function askMarsha(
  message: string,
  threadHistory: string[],
  context: string,
  threadTs?: string,
  channelId?: string,
): Promise<string> {
  const userContent = [
    `Message from Aidan: ${message}`,
    threadHistory.length
      ? `\nConversation so far:\n${threadHistory.join("\n")}`
      : "",
    `\n--- Infrastructure Context ---\n${context}`,
  ]
    .filter(Boolean)
    .join("\n");

  const messages: Array<Record<string, unknown>> = [
    { role: "user", content: userContent },
  ];

  const MAX_TOOL_ROUNDS = 5;
  for (let round = 0; round < MAX_TOOL_ROUNDS; round++) {
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5-20251001",
        max_tokens: 2048,
        system: MARSHA_SYSTEM_PROMPT,
        tools: MARSHA_TOOLS,
        messages,
      }),
    });

    const data = await r.json();
    const content = data?.content || [];
    const stopReason = data?.stop_reason;

    messages.push({ role: "assistant", content });

    if (stopReason !== "tool_use") {
      const textParts = content
        .filter((b: Record<string, unknown>) => b.type === "text")
        .map((b: Record<string, unknown>) => b.text as string);
      return textParts.join("\n") || "Hmm, I'm having a moment, baby. Try me again.";
    }

    const toolResults: Array<Record<string, unknown>> = [];
    for (const block of content) {
      if (block.type === "tool_use") {
        const result = await executeTool(
          block.name as string,
          block.input as Record<string, unknown>,
          threadTs,
          channelId,
        );
        toolResults.push({
          type: "tool_result",
          tool_use_id: block.id,
          content: result,
        });
      }
    }
    messages.push({ role: "user", content: toolResults });
  }

  return "I got a bit tangled up running through all those checks, sugar. Try asking me again.";
}

// ---------------------------------------------------------------------------
// Thread state management
// ---------------------------------------------------------------------------

async function getThread(
  slackTs: string
): Promise<{ id: string; status: string } | null> {
  const { data } = await supabase
    .from("infra_threads")
    .select("id, status")
    .eq("slack_ts", slackTs)
    .single();
  return data;
}

async function upsertThread(
  slackTs: string,
  channelId: string,
  userId: string,
  status: string,
  summary: string
): Promise<void> {
  await supabase.from("infra_threads").upsert(
    {
      slack_ts: slackTs,
      channel_id: channelId,
      user_id: userId,
      status,
      summary: summary.substring(0, 200),
      updated_at: new Date().toISOString(),
    },
    { onConflict: "slack_ts" }
  );
}

// ---------------------------------------------------------------------------
// Learning — save new rules to playbook
// ---------------------------------------------------------------------------

async function saveLearnedRule(rule: string): Promise<void> {
  const { data: existing } = await supabase
    .from("state")
    .select("data")
    .eq("key", "marsha_playbook")
    .single();

  const playbook = existing?.data
    ? typeof existing.data === "string"
      ? JSON.parse(existing.data)
      : existing.data
    : { rules: "", learned_at: [] };

  playbook.rules += `\n- ${rule}`;
  playbook.learned_at.push({
    rule,
    ts: new Date().toISOString(),
  });

  await supabase.from("state").upsert(
    {
      key: "marsha_playbook",
      data: JSON.stringify(playbook),
      updated_at: new Date().toISOString(),
    },
    { onConflict: "key" }
  );
}

// ---------------------------------------------------------------------------
// Proactive health check (called by cron or manual trigger)
// ---------------------------------------------------------------------------

async function fetchAllAccounts(): Promise<Array<Record<string, unknown>>> {
  const API = "https://server.smartlead.ai/api/v1";
  const key = Deno.env.get("SMARTLEAD_API_KEY") || "";
  const all: Array<Record<string, unknown>> = [];
  let offset = 0;
  while (true) {
    const r = await fetch(`${API}/email-accounts/?api_key=${key}&offset=${offset}&limit=100`);
    if (!r.ok) break;
    const batch = (await r.json()) as Array<Record<string, unknown>>;
    if (!batch.length) break;
    all.push(...batch);
    if (batch.length < 100) break;
    offset += 100;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return all;
}

async function fetchClients(): Promise<Map<number, string>> {
  const API = "https://server.smartlead.ai/api/v1";
  const key = Deno.env.get("SMARTLEAD_API_KEY") || "";
  const r = await fetch(`${API}/client?api_key=${key}`);
  if (!r.ok) return new Map();
  const clients = (await r.json()) as Array<Record<string, unknown>>;
  return new Map(clients.map((c) => [c.id as number, c.name as string]));
}

const ACTIVE_CLIENT_IDS = new Set([
  350067, // Borja
  350068, // Canopy
  325077, // Coastal
  325080, // Dallas
  375372, // Denair
  325117, // GM Landscaping
  358743, // Kay's B
  405344, // Lawnvalue
  325078, // Lightning
  328149, // Pioneer
  325076, // Timesavers
  325079, // Tropical
  367028, // Jim Robinson
  277143, // High Southern (ending May)
]);

const GENERIC_CLIENT_IDS = new Set([
  352787, 352788, 352789, 352790, // F, G, H, I
  407482, 407483, 407502, 407503, // J, K, L, M
]);

const ACQUISITION_CLIENT_ID = 328152;

async function runHealthCheck(): Promise<string> {
  try {
    const [accts, clientMap] = await Promise.all([
      fetchAllAccounts(),
      fetchClients(),
    ]);

    const issues: string[] = [];
    const byClient = new Map<string, Array<Record<string, unknown>>>();
    let totalCapacity = 0;
    let blocked = 0;
    let suspended = 0;
    let smtpFail = 0;
    let imapFail = 0;
    let noSending = 0;

    for (const a of accts) {
      const cid = a.client_id as number;
      const cname = clientMap.get(cid) || `Unknown (${cid})`;
      if (!byClient.has(cname)) byClient.set(cname, []);
      byClient.get(cname)!.push(a);

      const mpd = (a.message_per_day as number) || 0;
      totalCapacity += mpd;

      if (a.is_blocked) blocked++;
      if (a.is_suspended) suspended++;
      if (a.is_smtp_success === false) smtpFail++;
      if (a.is_imap_success === false) imapFail++;
      if (cid && mpd === 0 && ACTIVE_CLIENT_IDS.has(cid)) noSending++;
    }

    // --- Auto-remediation ---
    const fixes: string[] = [];

    // Auto-fix: re-enable warmup on accounts where it's off
    const warmupOff = accts.filter(
      (a) => a.client_id && !a.is_blocked && !a.is_suspended &&
        (a.warmup_details as Record<string, unknown>)?.warmup_enabled === false
    );
    if (warmupOff.length > 0) {
      let fixed = 0;
      for (const a of warmupOff.slice(0, 20)) {
        const r = await fetch(`${SL_API}/email-accounts/${a.id}/warmup?api_key=${SL_KEY}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ warmup_enabled: true, total_warmup_per_day: 15, daily_rampup: 5, reply_rate_percentage: 40 }),
        });
        if (r.ok) fixed++;
        await new Promise((res) => setTimeout(res, 400));
      }
      if (fixed > 0) fixes.push(`:wrench: Re-enabled warmup on *${fixed}* account(s)`);
      if (warmupOff.length > 20) fixes.push(`  _(${warmupOff.length - 20} more need warmup — will catch on next run)_`);
    }

    // Auto-fix: reconnect SMTP failures by re-saving connection
    const smtpFailAccts = accts.filter((a) => a.is_smtp_success === false && !a.is_suspended && a.client_id);
    if (smtpFailAccts.length > 0) {
      let reconnected = 0;
      let stillBroken = 0;
      for (const a of smtpFailAccts.slice(0, 10)) {
        const detailRes = await fetch(`${SL_API}/email-accounts/${a.id}?api_key=${SL_KEY}`);
        if (!detailRes.ok) { stillBroken++; continue; }
        const detail = await detailRes.json();
        const smtp = detail.smtp_host && detail.smtp_port && detail.username && detail.password;
        if (!smtp) { stillBroken++; continue; }

        const reconnRes = await fetch(`${SL_API}/email-accounts/${a.id}?api_key=${SL_KEY}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            from_name: detail.from_name,
            from_email: detail.from_email,
            username: detail.username,
            password: detail.password,
            smtp_host: detail.smtp_host,
            smtp_port: detail.smtp_port,
            imap_host: detail.imap_host,
            imap_port: detail.imap_port,
            max_email_per_day: detail.message_per_day || 15,
          }),
        });
        if (reconnRes.ok) reconnected++;
        else stillBroken++;
        await new Promise((res) => setTimeout(res, 500));
      }
      if (reconnected > 0) fixes.push(`:electric_plug: Reconnected *${reconnected}* SMTP account(s)`);
      if (stillBroken > 0) smtpFail = stillBroken;
      else smtpFail = 0;
    }

    // --- Issues that need human attention ---

    // Blocked accounts
    if (blocked > 0) {
      const blockedAccts = accts.filter((a) => a.is_blocked);
      const examples = blockedAccts.slice(0, 5).map((a) => `\`${a.from_email}\``).join(", ");
      issues.push(`:no_entry: *${blocked} blocked account(s)* — ${examples}${blocked > 5 ? ` +${blocked - 5} more` : ""}`);
    }

    // Suspended
    if (suspended > 0) {
      issues.push(`:warning: *${suspended} suspended account(s)* — SMTP/IMAP connections down`);
    }

    // SMTP/IMAP failures (after auto-fix attempt)
    if (smtpFail > 0) issues.push(`:envelope: *${smtpFail} SMTP failure(s)* — could not auto-reconnect, needs manual check`);
    if (imapFail > 0) issues.push(`:mailbox_with_no_mail: *${imapFail} IMAP failure(s)*`);

    // Active client accounts with 0 sending
    if (noSending > 0) {
      issues.push(`:mute: *${noSending} active client account(s)* with sending disabled (0 msg/day)`);
    }

    // Per-client summary
    const clientLines: string[] = [];
    const sortedClients = [...byClient.entries()].sort((a, b) => b[1].length - a[1].length);
    for (const [cname, clientAccts] of sortedClients) {
      const cap = clientAccts.reduce((s, a) => s + ((a.message_per_day as number) || 0), 0);
      const blk = clientAccts.filter((a) => a.is_blocked).length;
      const status = blk > 0 ? ` :warning: ${blk} blocked` : "";
      clientLines.push(`• *${cname}*: ${clientAccts.length} accts, ${cap}/day${status}`);
    }

    // Build the message
    const greeting = new Date().getHours() < 12 ? "Good morning, baby!" : new Date().getHours() < 17 ? "Hey there, sugar!" : "Evening, hon!";

    if (issues.length === 0 && fixes.length === 0) {
      const msg = [
        `${greeting} Marsha here with the daily infrastructure report. :clipboard:\n`,
        `:white_check_mark: *All ${accts.length} accounts healthy* — ${totalCapacity.toLocaleString()}/day total capacity\n`,
        `*Client breakdown:*`,
        ...clientLines,
      ].join("\n");
      await slackPost("chat.postMessage", { channel: SLACK_CHANNEL_ID, text: msg });
      return msg;
    }

    const sections: string[] = [
      `${greeting} Marsha here with the daily infrastructure report. :clipboard:\n`,
      `*${accts.length} total accounts* — ${totalCapacity.toLocaleString()}/day capacity\n`,
    ];

    if (fixes.length > 0) {
      sections.push(`:hammer_and_wrench: *Auto-fixed:*`, ...fixes, "");
    }
    if (issues.length > 0) {
      sections.push(`:rotating_light: *Needs attention:*`, ...issues, "");
    }
    sections.push(`*Client breakdown:*`, ...clientLines);

    if (issues.length > 0) {
      sections.push(`\nI couldn't fix everything — flagging the rest for you, sugar. :eyes:`);
    } else {
      sections.push(`\nFixed everything myself this time. We're running smooth, baby. :white_check_mark:`);
    }

    const msg = sections.join("\n");

    await slackPost("chat.postMessage", { channel: SLACK_CHANNEL_ID, text: msg });
    return msg;
  } catch (err) {
    const errMsg = `Marsha health check error: ${(err as Error).message}`;
    console.error(errMsg);
    return errMsg;
  }
}

// ---------------------------------------------------------------------------
// Main handler
// ---------------------------------------------------------------------------

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);

  // Cron health check: GET /marsha?action=health-check
  if (req.method === "GET" && url.searchParams.get("action") === "health-check") {
    const secret = url.searchParams.get("secret") || "";
    if (CRON_SECRET && secret !== CRON_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }
    const result = await runHealthCheck();
    return new Response(JSON.stringify({ ok: true, result }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  const body = await req.text();
  const timestamp = req.headers.get("x-slack-request-timestamp") || "";
  const signature = req.headers.get("x-slack-signature") || "";

  // Verify signature
  const valid = await verifySlackSignature(body, timestamp, signature);
  if (!valid) {
    return new Response("Unauthorized", { status: 401 });
  }

  const payload = JSON.parse(body);

  // URL verification challenge
  if (payload.type === "url_verification") {
    return new Response(JSON.stringify({ challenge: payload.challenge }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  // Handle events
  if (payload.type === "event_callback") {
    const event = payload.event;

    // Skip bot messages, retries, wrong channel
    if (event.bot_id) return new Response("ok");
    if (event.subtype) return new Response("ok");
    if (req.headers.get("x-slack-retry-num")) return new Response("ok");

    // Reaction approval for pending deletes
    if (event.type === "reaction_added" && event.reaction === "+1" && event.user === AIDAN_SLACK_USER_ID) {
      try {
        const { data: pending } = await supabase
          .from("state")
          .select("data")
          .eq("key", "marsha_pending_delete")
          .single();
        if (pending?.data) {
          const action = typeof pending.data === "string" ? JSON.parse(pending.data) : pending.data;
          const ids = action.account_ids as number[];
          if (ids?.length) {
            const results: string[] = [];
            for (const id of ids) {
              const r = await fetch(`${SL_API}/email-accounts/${id}?api_key=${SL_KEY}`, { method: "DELETE" });
              results.push(`${id}: ${r.ok ? "deleted" : `FAIL ${r.status}`}`);
              await new Promise((res) => setTimeout(res, 400));
            }
            await supabase.from("state").delete().eq("key", "marsha_pending_delete");
            const channel = event.item?.channel || SLACK_CHANNEL_ID;
            const thread = action.thread_ts || event.item?.ts;
            await postThreadReply(
              channel,
              thread,
              `:white_check_mark: Approved! Deleted ${ids.length} account(s).\n${results.join("\n")}`,
            );
          }
        }
      } catch (err) {
        console.error("Approval handler error:", err);
      }
      return new Response("ok");
    }

    // Message in #marsha
    if (event.type === "message" && event.channel === SLACK_CHANNEL_ID) {
      try {
        const threadTs = event.thread_ts || event.ts;
        const text = event.text || "";

        // Check if thread already resolved
        const existingThread = await getThread(threadTs);
        if (
          existingThread?.status === "resolved" ||
          existingThread?.status === "escalated"
        ) {
          return new Response("ok");
        }

        // Gather context and thread history
        const [context, history] = await Promise.all([
          gatherInfraContext(text),
          event.thread_ts
            ? getThreadHistory(event.channel, threadTs)
            : Promise.resolve([]),
        ]);

        // Ask Marsha (with tool use capabilities)
        const response = await askMarsha(text, history, context, threadTs, event.channel);

        // Extract tags
        const learnIdx = response.indexOf("[LEARN]");
        const alertIdx = response.indexOf("[ALERT]");

        let cleanResponse = response;

        // Handle learning
        if (learnIdx !== -1) {
          const rule = response.substring(learnIdx + "[LEARN]".length).trim();
          await saveLearnedRule(rule);
          cleanResponse = response.substring(0, learnIdx).trim();
          cleanResponse += "\n\n:brain: _Got it — added that to my playbook._";
        }

        // Handle alerts
        if (alertIdx !== -1) {
          const alertText = response
            .substring(alertIdx + "[ALERT]".length, learnIdx !== -1 ? learnIdx : undefined)
            .trim();
          // DM Aidan for critical alerts
          const openRes = await slackPost("conversations.open", {
            users: AIDAN_SLACK_USER_ID,
          });
          const dmChannelId = (openRes.channel as { id: string })?.id;
          if (dmChannelId) {
            const threadLink = `https://theheadlinetheory.slack.com/archives/${event.channel}/p${threadTs.replace(".", "")}`;
            await slackPost("chat.postMessage", {
              channel: dmChannelId,
              text: `:rotating_light: *Marsha — Infrastructure Alert*\n\n${alertText}\n\n*Thread:* ${threadLink}`,
            });
          }
        }

        // Post reply in thread
        await postThreadReply(event.channel, threadTs, cleanResponse);

        // Track thread
        await upsertThread(
          threadTs,
          event.channel,
          event.user,
          alertIdx !== -1 ? "alert" : "open",
          text
        );
      } catch (err) {
        console.error("Marsha error:", err);
      }
    }
  }

  return new Response("ok");
});
