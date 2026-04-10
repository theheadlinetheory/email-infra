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

    return errorResponse(
      "Unknown action. Valid: clients, clients-list, all-accounts, tags, inbox-campaigns, trends, health-metrics, unassigned",
      400,
    );
  } catch (e) {
    return errorResponse(`SmartLead proxy error: ${(e as Error).message}`, 500);
  }
});
