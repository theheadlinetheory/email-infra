import { corsHeaders, corsResponse, jsonResponse, errorResponse } from "../_shared/cors.ts";
import { slGet, slPost, slGetClients, slGetAllAccounts, slGetAllTags, slInternalGet } from "../_shared/smartlead.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return corsResponse();

  const url = new URL(req.url);
  const action = url.searchParams.get("action");

  try {
    if (action === "clients") {
      return jsonResponse(await slGetClients());
    }

    if (action === "clients-list") {
      const clients = (await slGetClients()) as Array<Record<string, unknown>>;
      return jsonResponse(clients.map((c) => ({ id: c.id, name: c.name })));
    }

    if (action === "all-accounts") {
      return jsonResponse(await slGetAllAccounts());
    }

    if (action === "tags") {
      return jsonResponse(await slGetAllTags());
    }

    if (action === "inbox-campaigns") {
      const email = url.searchParams.get("email");
      if (!email) return errorResponse("email required");
      const campaigns = (await slGet("/campaigns")) as Array<Record<string, unknown>>;
      const results: Array<Record<string, unknown>> = [];
      for (const camp of campaigns) {
        if (!["ACTIVE", "PAUSED"].includes(camp.status as string)) continue;
        try {
          const accounts = (await slGet(`/campaigns/${camp.id}/email-accounts`)) as Array<Record<string, unknown>>;
          if (accounts.some((a) => a.from_email === email)) {
            results.push({ id: camp.id, name: camp.name, status: camp.status });
          }
        } catch {
          continue;
        }
      }
      return jsonResponse(results);
    }

    if (action === "trends") {
      const clientId = url.searchParams.get("client_id");
      const days = parseInt(url.searchParams.get("days") || "30");
      if (!clientId) return errorResponse("client_id required");
      const campaigns = (await slGet("/campaigns", { client_id: clientId })) as Array<Record<string, unknown>>;
      const trends: Array<Record<string, unknown>> = [];
      for (const camp of campaigns) {
        try {
          const analytics = await slGet(`/campaigns/${camp.id}/analytics`);
          trends.push({ campaign_id: camp.id, campaign_name: camp.name, analytics });
        } catch {
          /* skip */
        }
      }
      return jsonResponse({ client_id: clientId, days, campaigns: trends });
    }

    if (action === "health-metrics") {
      const days = url.searchParams.get("days") || "7";
      const data = await slInternalGet(`/email-account/health-metrics?days=${days}`);
      return jsonResponse(data);
    }

    if (action === "unassigned") {
      const accounts = (await slGetAllAccounts()) as Array<Record<string, unknown>>;
      const unassigned = accounts
        .filter((a) => !a.client_id)
        .map((a) => {
          const wd = (a.warmup_details as Record<string, unknown>) || {};
          const email = (a.from_email as string) || "";
          return {
            id: a.id,
            email,
            domain: email.split("@").pop() || "",
            warmup_status: wd.status || "UNKNOWN",
            warmup_reputation: wd.warmup_reputation ?? "?",
            campaign_count: a.campaign_count || 0,
            smtp_ok: a.is_smtp_success || false,
          };
        });
      return jsonResponse({ accounts: unassigned, count: unassigned.length });
    }

    if (action === "generic-groups") {
      const [clients, accounts] = await Promise.all([
        slGetClients() as Promise<Array<Record<string, unknown>>>,
        slGetAllAccounts() as Promise<Array<Record<string, unknown>>>,
      ]);

      // Fetch health metrics
      const now = new Date();
      const end = now.toISOString().slice(0, 10);
      const start = new Date(now.getTime() - 7 * 86400000).toISOString().slice(0, 10);
      let healthData: Record<string, Record<string, unknown>> = {};
      try {
        const hResp = (await slInternalGet(
          `/analytics/mailbox/name-wise-health-metrics?start_date=${start}&end_date=${end}&timezone=America/New_York&full_data=true`,
        )) as Record<string, unknown>;
        const metrics = ((hResp?.data as Record<string, unknown>)?.email_health_metrics as Array<Record<string, unknown>>) || [];
        for (const m of metrics) healthData[m.from_email as string] = m;
      } catch { /* no health data */ }

      const genericClients = clients
        .filter((c) => ((c.name as string) || "").toLowerCase().startsWith("generic"))
        .sort((a, b) => ((a.name as string) || "").localeCompare((b.name as string) || ""));

      // Load pipelines from Supabase for pipeline IDs
      let allPipelines: Array<Record<string, unknown>> = [];
      try {
        const pResp = await fetch(
          `${Deno.env.get("SUPABASE_URL")}/rest/v1/pipelines?select=data`,
          {
            headers: {
              apikey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "",
              Authorization: `Bearer ${Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || ""}`,
            },
          },
        );
        const rows = (await pResp.json()) as Array<Record<string, unknown>>;
        allPipelines = rows.map((r) => typeof r.data === "string" ? JSON.parse(r.data) : r.data);
      } catch { /* no pipelines */ }

      const groups: Array<Record<string, unknown>> = [];
      let totalAccounts = 0;
      let totalCapacity = 0;

      for (const cl of genericClients) {
        const clAccounts = accounts.filter((a) => a.client_id === cl.id);
        if (!clAccounts.length) continue;

        totalAccounts += clAccounts.length;
        const domains = new Set<string>();
        let smtpFail = 0;
        let dailyCap = 0;
        const warmupDates: string[] = [];
        const healthScores: number[] = [];

        for (const acc of clAccounts) {
          const email = (acc.from_email as string) || "";
          const domain = email.split("@").pop() || "";
          if (domain) domains.add(domain);
          dailyCap += (acc.message_per_day as number) || 0;
          if (!acc.is_smtp_success) smtpFail++;

          // Warmup start date from warmup_details
          const wd = (acc.warmup_details as Record<string, unknown>) || {};
          const warmupCreated = (wd.warmup_created_at as string) || "";
          if (warmupCreated) warmupDates.push(warmupCreated.slice(0, 10));

          // Health score
          const repRaw = wd.warmup_reputation;
          let repScore = 100;
          if (repRaw !== undefined && repRaw !== "?") {
            const rep = typeof repRaw === "number" ? repRaw : parseFloat(String(repRaw));
            if (!isNaN(rep)) {
              if (rep >= 99) repScore = 100;
              else if (rep <= 95) repScore = 0;
              else repScore = ((rep - 95) / 4) * 100;
            }
          }
          healthScores.push(Math.round(repScore));
        }

        totalCapacity += dailyCap;
        const avgHealth = healthScores.length ? Math.round(healthScores.reduce((a, b) => a + b, 0) / healthScores.length) : 100;

        // Calculate warmup progress
        const earliestWarmup = warmupDates.length ? warmupDates.sort()[0] : null;
        let daysWarming = 0;
        let daysLeft = 0;
        let readyDateStr = "";
        let warmupStartStr = "";
        let status = "warming";

        if (earliestWarmup) {
          const ws = new Date(earliestWarmup);
          warmupStartStr = `${ws.getMonth() + 1}/${ws.getDate()}`;
          daysWarming = Math.floor((now.getTime() - ws.getTime()) / 86400000);
          const ready = new Date(ws.getTime() + 14 * 86400000);
          readyDateStr = `${ready.getMonth() + 1}/${ready.getDate()}`;
          daysLeft = Math.max(0, Math.ceil((ready.getTime() - now.getTime()) / 86400000));
          if (daysLeft <= 0) status = "ready";
        }

        // Find matching pipeline
        let pipelineId = "";
        const clNameLower = ((cl.name as string) || "").toLowerCase().trim();
        for (const p of allPipelines) {
          if (((p.client_name as string) || "").toLowerCase().trim() === clNameLower) {
            pipelineId = (p.id as string) || "";
            break;
          }
        }

        groups.push({
          name: cl.name,
          client_id: cl.id,
          pipeline_id: pipelineId,
          accounts: clAccounts.length,
          domains: domains.size,
          daily_capacity: dailyCap,
          smtp_failures: smtpFail,
          health_score: avgHealth,
          warmup_start: warmupStartStr,
          ready_date: readyDateStr,
          days_warming: daysWarming,
          days_left: daysLeft,
          status,
        });
      }

      return jsonResponse({
        groups,
        total_accounts: totalAccounts,
        total_daily_capacity: totalCapacity,
        generated_at: now.toISOString(),
      });
    }

    if (action === "acquisition") {
      const now = new Date();
      const end = now.toISOString().slice(0, 10);
      const start = new Date(now.getTime() - 7 * 86400000).toISOString().slice(0, 10);

      const [clients, accounts] = await Promise.all([
        slGetClients() as Promise<Array<Record<string, unknown>>>,
        slGetAllAccounts() as Promise<Array<Record<string, unknown>>>,
      ]);

      // Fetch health metrics from internal API
      let healthData: Record<string, Record<string, unknown>> = {};
      try {
        const hResp = (await slInternalGet(
          `/analytics/mailbox/name-wise-health-metrics?start_date=${start}&end_date=${end}&timezone=America/New_York&full_data=true`,
        )) as Record<string, unknown>;
        const metrics = ((hResp?.data as Record<string, unknown>)?.email_health_metrics as Array<Record<string, unknown>>) || [];
        for (const m of metrics) healthData[m.from_email as string] = m;
      } catch { /* no health data */ }

      // Find acquisition group clients (e.g. "A Group (250/day)")
      const groupClients = clients
        .filter((c) => {
          const name = (c.name as string) || "";
          const nl = name.toLowerCase();
          return nl.includes("group") && (name.includes("/") || nl.includes("day"));
        })
        .sort((a, b) => ((a.name as string) || "").localeCompare((b.name as string) || ""));

      const groups: Array<Record<string, unknown>> = [];
      let totalAccounts = 0;

      for (const cl of groupClients) {
        const clAccounts = accounts.filter((a) => a.client_id === cl.id);
        if (!clAccounts.length) continue;

        totalAccounts += clAccounts.length;
        let warming = 0;
        let inCampaign = 0;
        let smtpFail = 0;
        let clSent = 0;
        let clBounced = 0;
        let clReplied = 0;
        const clScores: number[] = [];
        const clBounceRates: number[] = [];
        const clReplyRates: number[] = [];
        const flaggedDomains = new Set<string>();
        const allDomains = new Set<string>();

        for (const acc of clAccounts) {
          const email = (acc.from_email as string) || "";
          const domain = email.split("@").pop() || "";
          if (domain) allDomains.add(domain);

          const wd = (acc.warmup_details as Record<string, unknown>) || {};
          if (wd.status === "ACTIVE") warming++;
          if ((acc.campaign_count as number) > 0) inCampaign++;
          if (!acc.is_smtp_success) smtpFail++;

          // Health score calculation (matches dashboard.py calculate_health_score)
          const h = healthData[email] || {};
          const flags: string[] = [];

          // Reputation score: 100 at >=99%, linear 95-99%, 0 at <=95%
          const repRaw = wd.warmup_reputation;
          let repScore = 100;
          if (repRaw !== undefined && repRaw !== "?") {
            const rep = typeof repRaw === "number" ? repRaw : parseFloat(String(repRaw));
            if (!isNaN(rep)) {
              if (rep >= 99) repScore = 100;
              else if (rep <= 95) { repScore = 0; flags.push("reputation"); }
              else { repScore = ((rep - 95) / 4) * 100; flags.push("reputation"); }
            }
          }

          // Sent/bounced/replied aggregation
          const sent = (h.sent as number) || 0;
          const bounced = (h.bounced as number) || 0;
          const replied = (h.replied as number) || 0;
          clSent += sent;
          clBounced += bounced;
          clReplied += replied;

          // Bounce/reply rate parsing
          const brVal = h.bounce_rate != null ? parseFloat(String(h.bounce_rate).replace("%", "")) : NaN;
          if (!isNaN(brVal)) clBounceRates.push(brVal);
          const rrVal = h.reply_rate != null ? parseFloat(String(h.reply_rate).replace("%", "")) : NaN;
          if (!isNaN(rrVal)) clReplyRates.push(rrVal);

          // Reply rate component (only with 100+ sent)
          let score: number;
          if (sent < 100) {
            score = Math.round(repScore);
          } else {
            let replyScore = 100;
            if (!isNaN(rrVal)) {
              if (rrVal >= 2) replyScore = 100;
              else if (rrVal <= 0.5) { replyScore = 0; flags.push("reply"); }
              else replyScore = ((rrVal - 0.5) / 1.5) * 100;
            }
            score = Math.round(repScore * 0.8 + replyScore * 0.2);
          }

          clScores.push(score);
          if (flags.length > 0 && domain) flaggedDomains.add(domain);
        }

        const avgHealth = clScores.length ? Math.round(clScores.reduce((a, b) => a + b, 0) / clScores.length) : 100;
        const avgBounce = clBounceRates.length ? Math.round((clBounceRates.reduce((a, b) => a + b, 0) / clBounceRates.length) * 100) / 100 : 0;
        const avgReply = clReplyRates.length ? Math.round((clReplyRates.reduce((a, b) => a + b, 0) / clReplyRates.length) * 100) / 100 : 0;
        const totalDomains = allDomains.size;

        groups.push({
          id: cl.id,
          name: cl.name,
          accounts: clAccounts.length,
          warming,
          in_campaign: inCampaign,
          smtp_failures: smtpFail,
          total_domains: totalDomains,
          total_sent: clSent,
          total_bounced: clBounced,
          total_replied: clReplied,
          avg_bounce_rate: avgBounce,
          avg_reply_rate: avgReply,
          health_score: avgHealth,
          flagged_domains: flaggedDomains.size,
          flagged_pct: totalDomains ? Math.round(flaggedDomains.size / totalDomains * 100) : 0,
          needs_attention: totalDomains ? flaggedDomains.size / totalDomains >= 0.15 : false,
        });
      }

      return jsonResponse({
        groups,
        total_accounts: totalAccounts,
        total_groups: groups.length,
        generated_at: now.toISOString(),
      });
    }

    return errorResponse(
      "Unknown action. Valid: clients, clients-list, all-accounts, tags, inbox-campaigns, trends, health-metrics, unassigned, generic-groups, acquisition",
      400,
    );
  } catch (e) {
    return errorResponse(`SmartLead proxy error: ${(e as Error).message}`, 500);
  }
});
