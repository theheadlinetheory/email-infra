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

const SLACK_BOT_TOKEN = Deno.env.get("MARSHA_SLACK_BOT_TOKEN") || "";
const SLACK_SIGNING_SECRET = Deno.env.get("MARSHA_SLACK_SIGNING_SECRET") || "";
const SLACK_CHANNEL_ID = Deno.env.get("MARSHA_SLACK_CHANNEL") || "C0ATXCU3SR2";
const AIDAN_SLACK_USER_ID = "U09B2673A4A";
const ANTHROPIC_API_KEY = Deno.env.get("ANTHROPIC_API_KEY") || "";

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
- A/B rotation model: each client has an A group (active sending) and B group (warming up). When B hits 100% warmup, swap.
- Generic groups (A-I) are pre-built infrastructure for NEW clients only. 14-day warmup must be complete before assignment.
- SR Acquisition groups (A-M) are subgroups of "Acquisition Inboxes" client (328152), split by domain.
- save-management-details endpoint SETS the full tag list + clientId — it does NOT append.
- Domains are never auto-renewed. Lifecycle is too short.
- Campaign conflicts only matter for campaigns with "acquisition" in the name.
- The inbox_history table tracks all changes. Source "dashboard" = made through the dashboard, "snapshot" = detected by periodic comparison, "script" = made by a Python script.

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

async function askMarsha(
  message: string,
  threadHistory: string[],
  context: string
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

  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: "claude-haiku-4-5-20251001",
      max_tokens: 1024,
      system: MARSHA_SYSTEM_PROMPT,
      messages: [{ role: "user", content: userContent }],
    }),
  });

  const data = await r.json();
  return data?.content?.[0]?.text || "Hmm, I'm having a moment, baby. Try me again.";
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
// Main handler
// ---------------------------------------------------------------------------

Deno.serve(async (req: Request) => {
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

        // Ask Marsha
        const response = await askMarsha(text, history, context);

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
