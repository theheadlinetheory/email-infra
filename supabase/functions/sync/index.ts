import { jsonResponse, errorResponse } from "../_shared/cors.ts";
import { getServiceClient } from "../_shared/supabase.ts";
import { slGetClients, slGetAllAccounts, slGet, slInternalGet } from "../_shared/smartlead.ts";

// --- Types ---

interface Account {
  id: number;
  from_email: string;
  from_name: string;
  client_id: number | null;
  warmup_details?: { status: string; warmup_reputation: number | string; total_sent_count?: number; total_spam_count?: number; blocked_reason?: string };
  is_smtp_success?: boolean;
  is_imap_success?: boolean;
  campaign_count?: number;
  daily_sent_count?: number;
  message_sent_count?: number;
  total_warmup_sent_per_day?: number;
  [key: string]: unknown;
}

interface Client {
  id: number;
  name: string;
  [key: string]: unknown;
}

interface HealthMetric {
  from_email: string;
  sent?: number;
  bounced?: number;
  replied?: number;
  bounce_rate?: string | number;
  reply_rate?: string | number;
}

// --- Helpers ---

function parseRate(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const s = String(value).trim().replace("%", "");
  const n = parseFloat(s);
  return isNaN(n) ? null : n;
}

function calculateHealthScore(
  account: Account,
  healthData: Record<string, HealthMetric>,
  inWarmupPeriod: boolean,
): { score: number; flags: string[] } {
  const email = account.from_email || "";
  const h = healthData[email] || {};
  const wd = account.warmup_details || {};
  const flags: string[] = [];

  // Warmup reputation score
  let repScore = 100;
  const repRaw = wd.warmup_reputation;
  if (repRaw !== undefined && repRaw !== "?") {
    const rep = typeof repRaw === "number" ? repRaw : parseFloat(String(repRaw));
    if (!isNaN(rep)) {
      if (rep >= 99) {
        repScore = 100;
      } else if (rep <= 95) {
        repScore = 0;
        flags.push("reputation");
      } else {
        repScore = ((rep - 95) / 4) * 100;
        flags.push("reputation");
      }
    }
  }

  const totalSent = (h.sent || 0) as number;
  if (inWarmupPeriod || totalSent < 100) {
    return { score: Math.round(repScore), flags };
  }

  // Reply rate score (20% weight)
  let replyScore = 100;
  const rr = parseRate(h.reply_rate);
  if (rr !== null) {
    if (rr >= 2) replyScore = 100;
    else if (rr <= 0.5) {
      replyScore = 0;
      flags.push("reply");
    } else {
      replyScore = ((rr - 0.5) / 1.5) * 100;
    }
  }

  return { score: Math.round(repScore * 0.8 + replyScore * 0.2), flags };
}

// --- Main sync ---

Deno.serve(async (_req) => {
  const sb = getServiceClient();

  try {
    console.log("[sync] Starting SmartLead → Supabase sync");

    // Fetch all data in parallel
    const [accounts, clients] = await Promise.all([
      slGetAllAccounts() as Promise<Account[]>,
      slGetClients() as Promise<Client[]>,
    ]);

    // Fetch health metrics from internal API
    const now = new Date();
    const end = now.toISOString().slice(0, 10);
    const start = new Date(now.getTime() - 7 * 86400000).toISOString().slice(0, 10);
    let healthData: Record<string, HealthMetric> = {};
    try {
      const hResp = (await slInternalGet(
        `/analytics/mailbox/name-wise-health-metrics?start_date=${start}&end_date=${end}&timezone=America/New_York&full_data=true`,
      )) as Record<string, unknown>;
      const metrics = ((hResp?.data as Record<string, unknown>)?.email_health_metrics as HealthMetric[]) || [];
      for (const m of metrics) healthData[m.from_email] = m;
    } catch (e) {
      console.warn("[sync] Health metrics fetch failed:", e);
    }

    // Fetch campaign counts
    const campaignCounts: Record<string, number> = {};
    try {
      const campaigns = (await slGet("/campaigns")) as Array<Record<string, unknown>>;
      for (const camp of campaigns) {
        if (!["ACTIVE", "PAUSED"].includes(camp.status as string)) continue;
        try {
          const campAccounts = (await slGet(`/campaigns/${camp.id}/email-accounts`)) as Array<Record<string, unknown>>;
          if (Array.isArray(campAccounts)) {
            for (const ca of campAccounts) {
              const email = (ca.from_email as string) || "";
              if (email) campaignCounts[email] = (campaignCounts[email] || 0) + 1;
            }
          }
        } catch {
          continue;
        }
      }
    } catch (e) {
      console.warn("[sync] Campaign counts fetch failed:", e);
    }

    // Enrich accounts with campaign counts
    for (const a of accounts) {
      a.campaign_count = campaignCounts[a.from_email || ""] || 0;
    }

    // Load warmup start dates from client_configs
    let warmupDates: Record<string, string> = {};
    try {
      const { data: configs } = await sb.from("client_configs").select("data");
      for (const row of configs || []) {
        const d = typeof row.data === "string" ? JSON.parse(row.data) : row.data;
        const name = d?.client_name || "";
        const ws = d?.infrastructure?.warmup_start_date || "";
        if (name && ws) warmupDates[name.toLowerCase()] = ws;
      }
    } catch (e) {
      console.warn("[sync] Warmup dates fetch failed:", e);
    }

    // Load paused clients
    let pausedClients: string[] = [];
    try {
      const { data: pausedRow } = await sb.from("state").select("data").eq("key", "paused_clients").single();
      if (pausedRow?.data) {
        const parsed = typeof pausedRow.data === "string" ? JSON.parse(pausedRow.data) : pausedRow.data;
        pausedClients = parsed.clients || [];
      }
    } catch { /* no paused clients */ }

    // Load target volumes
    let targetVolumes: Record<string, number> = {};
    try {
      const { data: volRows } = await sb.from("state").select("key,data").like("key", "target_volume:%");
      for (const row of volRows || []) {
        const name = row.key.replace("target_volume:", "");
        const d = typeof row.data === "string" ? JSON.parse(row.data) : row.data;
        targetVolumes[name] = d?.target_volume || 0;
      }
    } catch { /* no volumes */ }

    // Build client map
    const clientMap = new Map<number, Client>();
    for (const c of clients) clientMap.set(c.id, c);

    // Helper to classify clients
    const isAcquisitionGroup = (name: string) => {
      const nl = name.toLowerCase();
      return nl.includes("group") && (name.includes("/") || nl.includes("day"));
    };
    const isGenericGroup = (name: string) => name.toLowerCase().startsWith("generic");

    // Build overview
    const total = accounts.length;
    const inCampaign = accounts.filter((a) => (a.campaign_count || 0) > 0).length;
    const smtpFail = accounts.filter((a) => !a.is_smtp_success).length;
    const imapFail = accounts.filter((a) => !a.is_imap_success).length;
    const unassigned = accounts.filter((a) => !a.client_id).length;
    const blocked = accounts
      .filter((a) => {
        const wd = a.warmup_details || {};
        return wd.status !== "ACTIVE" && wd.status !== undefined && wd.blocked_reason;
      })
      .slice(0, 20)
      .map((a) => ({
        email: a.from_email,
        reason: a.warmup_details?.blocked_reason || "Unknown",
      }));

    // Client summaries
    const clientSummaries: Array<Record<string, unknown>> = [];
    for (const cl of clients) {
      if (isAcquisitionGroup(cl.name) || isGenericGroup(cl.name)) continue;
      const clAccounts = accounts.filter((a) => a.client_id === cl.id);
      if (!clAccounts.length) continue;

      const wsDate = warmupDates[cl.name.toLowerCase()] || "";
      let daysLeft: number | null = null;
      let readyDate = "";
      let rotationDate = "";
      let rotationDays: number | null = null;

      if (wsDate) {
        const ws = new Date(wsDate);
        const ready = new Date(ws.getTime() + 14 * 86400000);
        readyDate = ready.toISOString().slice(0, 10);
        daysLeft = Math.ceil((ready.getTime() - now.getTime()) / 86400000);
        const rot = new Date(ws.getTime() + 42 * 86400000);
        rotationDate = rot.toISOString().slice(0, 10);
        rotationDays = Math.ceil((rot.getTime() - now.getTime()) / 86400000);
      }

      const clStillWarming = daysLeft !== null && daysLeft > 0 ? clAccounts.length : 0;
      const clCampaigns = clAccounts.filter((a) => (a.campaign_count || 0) > 0).length;
      const clSmtpFail = clAccounts.filter((a) => !a.is_smtp_success).length;
      const clBlocked = clAccounts.filter((a) => {
        const wd = a.warmup_details || {};
        return wd.status !== "ACTIVE" && wd.status !== undefined;
      }).length;

      const warmupComplete = daysLeft !== null && daysLeft <= 0;
      const clIdle = warmupComplete ? clAccounts.filter((a) => (a.campaign_count || 0) === 0).length : 0;

      // Health aggregates
      let clSent = 0, clBounced = 0, clReplied = 0, clHealthCount = 0;
      const clBounceRates: number[] = [];
      const clReplyRates: number[] = [];
      for (const a of clAccounts) {
        const h = healthData[a.from_email || ""];
        if (h) {
          clHealthCount++;
          clSent += (h.sent || 0) as number;
          clBounced += (h.bounced || 0) as number;
          clReplied += (h.replied || 0) as number;
          const br = parseRate(h.bounce_rate);
          if (br !== null) clBounceRates.push(br);
          const rr = parseRate(h.reply_rate);
          if (rr !== null) clReplyRates.push(rr);
        }
      }

      const avgBounce = clBounceRates.length ? Math.round((clBounceRates.reduce((a, b) => a + b, 0) / clBounceRates.length) * 10) / 10 : null;
      const avgReply = clReplyRates.length ? Math.round((clReplyRates.reduce((a, b) => a + b, 0) / clReplyRates.length) * 10) / 10 : null;

      // Health scores
      const clInWarmup = daysLeft !== null && daysLeft > 0;
      const clScores: number[] = [];
      const flaggedDomains = new Set<string>();
      for (const a of clAccounts) {
        const hs = calculateHealthScore(a, healthData, clInWarmup);
        clScores.push(hs.score);
        if (hs.flags.length) {
          const domain = (a.from_email || "").split("@").pop() || "";
          flaggedDomains.add(domain);
        }
      }

      const allClDomains = new Set(clAccounts.map((a) => (a.from_email || "").split("@").pop() || ""));
      const totalDomains = allClDomains.size;
      const flaggedPct = totalDomains > 0 ? (flaggedDomains.size / totalDomains) * 100 : 0;
      const avgHealth = clScores.length ? Math.round(clScores.reduce((a, b) => a + b, 0) / clScores.length) : 0;

      // Warmup progress
      let warmupProgress = "—";
      let warmupDaysDone: number | null = null;
      if (daysLeft !== null && daysLeft > 0) {
        warmupDaysDone = 14 - daysLeft;
        warmupProgress = `Day ${warmupDaysDone}/14`;
      } else if (wsDate) {
        warmupDaysDone = 14;
        warmupProgress = "Complete";
      }

      // Capacity
      const healthy = clAccounts.length - clSmtpFail - clBlocked;
      const capacity = healthy * 15;
      const target = targetVolumes[cl.name] || 0;
      let inboxesNeeded = 0;
      let capacityStatus = "no_target";
      if (target > 0) {
        const shortfall = target - capacity;
        inboxesNeeded = Math.max(0, Math.ceil(shortfall / 15));
        capacityStatus = capacity >= target ? "on_track" : "need_more";
      }

      clientSummaries.push({
        id: cl.id,
        name: cl.name,
        accounts: clAccounts.length,
        warming: clStillWarming,
        in_campaign: clCampaigns,
        smtp_failures: clSmtpFail,
        blocked: clBlocked,
        warmup_start: wsDate,
        ready_date: readyDate,
        days_until_ready: daysLeft,
        rotation_date: rotationDate,
        days_until_rotation: rotationDays,
        health_accounts: clHealthCount,
        total_sent: clSent,
        total_bounced: clBounced,
        total_replied: clReplied,
        avg_bounce_rate: avgBounce,
        avg_reply_rate: avgReply,
        health_score: avgHealth,
        total_domains: totalDomains,
        flagged_domains: flaggedDomains.size,
        flagged_pct: Math.round(flaggedPct * 10) / 10,
        needs_attention: flaggedPct >= 15,
        warmup_progress: warmupProgress,
        warmup_days_done: warmupDaysDone,
        idle_inboxes: clIdle,
        healthy_inboxes: healthy,
        daily_capacity: capacity,
        target_volume: target,
        inboxes_needed: inboxesNeeded,
        capacity_status: capacityStatus,
      });
    }

    // Sort: needs attention first, then failures, then alpha
    clientSummaries.sort((a, b) => {
      const aKey = [
        a.needs_attention ? 0 : 1,
        (a.blocked as number) > 0 || (a.smtp_failures as number) > 0 ? 0 : 1,
        (a.name as string).toLowerCase(),
      ];
      const bKey = [
        b.needs_attention ? 0 : 1,
        (b.blocked as number) > 0 || (b.smtp_failures as number) > 0 ? 0 : 1,
        (b.name as string).toLowerCase(),
      ];
      for (let i = 0; i < 3; i++) {
        if (aKey[i] < bKey[i]) return -1;
        if (aKey[i] > bKey[i]) return 1;
      }
      return 0;
    });

    const attentionCount = clientSummaries.filter((c) => c.needs_attention).length;
    const totalWarming = clientSummaries.reduce((s, c) => s + (c.warming as number), 0);
    const totalIdle = clientSummaries.reduce((s, c) => s + (c.idle_inboxes as number), 0);
    const idleClients = clientSummaries.filter((c) => (c.idle_inboxes as number) > 0).length;

    const overview = {
      total_accounts: total,
      warming: totalWarming,
      in_campaign: inCampaign,
      unassigned,
      smtp_failures: smtpFail,
      imap_failures: imapFail,
      blocked_accounts: blocked,
      clients: clientSummaries,
      attention_count: attentionCount,
      paused_clients: pausedClients,
      idle_inboxes: totalIdle,
      idle_clients: idleClients,
      generated_at: now.toISOString(),
    };

    // Write overview cache
    const ts = now.toISOString();
    await sb.from("state").upsert({ key: "cache:overview", data: JSON.stringify(overview), updated_at: ts });

    // Write per-client account caches
    for (const cl of clients) {
      if (isAcquisitionGroup(cl.name) || isGenericGroup(cl.name)) continue;
      const clAccounts = accounts.filter((a) => a.client_id === cl.id);
      if (!clAccounts.length) continue;

      const wsDate = warmupDates[cl.name.toLowerCase()] || "";
      let inWarmup = false;
      let warmupDaysElapsed: number | null = null;
      if (wsDate) {
        const ws = new Date(wsDate);
        const ready = new Date(ws.getTime() + 14 * 86400000);
        inWarmup = ready > now;
        warmupDaysElapsed = Math.floor((now.getTime() - ws.getTime()) / 86400000);
      }

      const enriched = clAccounts.map((a) => {
        const wd = a.warmup_details || {};
        const email = a.from_email || "";
        const h = healthData[email] || {};
        const hs = calculateHealthScore(a, healthData, inWarmup);
        return {
          id: a.id,
          email,
          domain: email.split("@").pop() || "",
          warmup_status: wd.status || "UNKNOWN",
          warmup_sent: wd.total_sent_count || 0,
          warmup_spam: wd.total_spam_count || 0,
          warmup_reputation: wd.warmup_reputation ?? "?",
          blocked_reason: wd.blocked_reason || null,
          campaign_count: campaignCounts[email] || 0,
          daily_sent: a.daily_sent_count || 0,
          smtp_ok: a.is_smtp_success || false,
          imap_ok: a.is_imap_success || false,
          bounce_rate: h.bounce_rate ?? null,
          reply_rate: h.reply_rate ?? null,
          health_sent: (h.sent || 0) as number,
          health_bounced: (h.bounced || 0) as number,
          health_replied: (h.replied || 0) as number,
          health_score: hs.score,
          health_flags: hs.flags,
          warmup_days: warmupDaysElapsed,
        };
      });

      // Domain flagging
      const byDomain: Record<string, typeof enriched> = {};
      for (const acc of enriched) {
        if (!byDomain[acc.domain]) byDomain[acc.domain] = [];
        byDomain[acc.domain].push(acc);
      }
      const flaggedDomains = Object.entries(byDomain)
        .filter(([_, accs]) => accs.some((a) => a.health_flags.length > 0))
        .map(([d]) => d);
      const flaggedInboxCount = flaggedDomains.reduce((s, d) => s + (byDomain[d]?.length || 0), 0);

      const clientData = {
        client_id: cl.id,
        client_name: cl.name,
        accounts: enriched,
        flagged_domains: flaggedDomains,
        flagged_inbox_count: flaggedInboxCount,
        replacement_domains_needed: flaggedDomains.length,
        replacement_inboxes: flaggedDomains.length * 3,
      };

      await sb.from("state").upsert({
        key: `cache:client_accounts_${cl.id}`,
        data: JSON.stringify(clientData),
        updated_at: ts,
      });
    }

    console.log(`[sync] Complete — ${clientSummaries.length} clients cached`);
    return jsonResponse({
      ok: true,
      clients_cached: clientSummaries.length,
      total_accounts: total,
    });
  } catch (e) {
    console.error("[sync] Error:", e);
    return errorResponse(`Sync error: ${(e as Error).message}`, 500);
  }
});
