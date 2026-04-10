import { corsResponse, jsonResponse, errorResponse } from "../_shared/cors.ts";
import { getServiceClient } from "../_shared/supabase.ts";
import { slGet, slPost } from "../_shared/smartlead.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return corsResponse();
  if (req.method !== "POST") return errorResponse("POST required", 405);

  const url = new URL(req.url);
  const action = url.searchParams.get("action");
  const body = await req.json();

  try {
    if (action === "assign") {
      const { account_ids, client_id } = body;
      if (!account_ids?.length || !client_id) return errorResponse("account_ids and client_id required");
      const results: Array<Record<string, unknown>> = [];
      for (const id of account_ids) {
        const r = await slPost(`/email-accounts/${id}/update`, { client_id });
        results.push({ id, result: r });
      }
      return jsonResponse({ success: true, results });
    }

    if (action === "pause-monitor") {
      const { client_name, paused } = body;
      if (!client_name) return errorResponse("client_name required");
      const sb = getServiceClient();
      const { data: row } = await sb.from("state").select("data").eq("key", "paused_clients").single();
      const state = row?.data ? (typeof row.data === "string" ? JSON.parse(row.data) : row.data) : { clients: [] };
      const clients: string[] = state.clients || [];
      if (paused && !clients.includes(client_name)) clients.push(client_name);
      if (!paused) {
        const idx = clients.indexOf(client_name);
        if (idx !== -1) clients.splice(idx, 1);
      }
      await sb.from("state").upsert({
        key: "paused_clients",
        data: JSON.stringify({ clients }),
        updated_at: new Date().toISOString(),
      });
      return jsonResponse({ ok: true, paused_clients: clients });
    }

    if (action === "set-target-volume") {
      const { client_name, target_volume } = body;
      if (!client_name) return errorResponse("client_name required");
      const sb = getServiceClient();
      await sb.from("state").upsert({
        key: `target_volume:${client_name}`,
        data: JSON.stringify({ target_volume }),
        updated_at: new Date().toISOString(),
      });
      return jsonResponse({ ok: true });
    }

    if (action === "remove-from-campaign") {
      const { email_account_id, campaign_id } = body;
      if (!email_account_id || !campaign_id) return errorResponse("email_account_id and campaign_id required");
      const r = await slPost(`/campaigns/${campaign_id}/email-accounts/remove`, {
        email_account_ids: [email_account_id],
      });
      return jsonResponse(r);
    }

    if (action === "remove-from-all-campaigns") {
      const { email_account_id, from_email } = body;
      if (!email_account_id && !from_email) return errorResponse("email_account_id or from_email required");
      const campaigns = (await slGet("/campaigns/")) as Array<Record<string, unknown>>;
      const removed: string[] = [];
      for (const camp of campaigns) {
        if (!["ACTIVE", "PAUSED"].includes(camp.status as string)) continue;
        try {
          const accounts = (await slGet(`/campaigns/${camp.id}/email-accounts`)) as Array<Record<string, unknown>>;
          const match = accounts.find(
            (a) =>
              (email_account_id && a.id === email_account_id) ||
              (from_email && a.from_email === from_email),
          );
          if (match) {
            await slPost(`/campaigns/${camp.id}/email-accounts/remove`, {
              email_account_ids: [match.id],
            });
            removed.push(camp.name as string);
          }
        } catch {
          continue;
        }
      }
      return jsonResponse({ success: true, removed_from: removed });
    }

    return errorResponse(
      "Unknown action. Valid: assign, pause-monitor, set-target-volume, remove-from-campaign, remove-from-all-campaigns",
      400,
    );
  } catch (e) {
    return errorResponse(`Mutation error: ${(e as Error).message}`, 500);
  }
});
