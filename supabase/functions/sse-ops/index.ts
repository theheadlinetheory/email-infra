import { corsResponse, jsonResponse, errorResponse } from "../_shared/cors.ts";
import { getServiceClient } from "../_shared/supabase.ts";
import { slGetClients, slPost, slGetAllTags, slGet } from "../_shared/smartlead.ts";
import { zmListDomains } from "../_shared/zapmail.ts";

async function writeEvent(
  sb: ReturnType<typeof getServiceClient>,
  jobId: string,
  step: number,
  status: string,
  message: string,
  data: Record<string, unknown> = {},
) {
  await sb.from("sse_events").insert({ job_id: jobId, step, status, message, data });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return corsResponse();

  const url = new URL(req.url);
  const action = url.searchParams.get("action");
  const sb = getServiceClient();

  // Poll for events
  if (action === "poll") {
    const jobId = url.searchParams.get("job_id");
    if (!jobId) return errorResponse("job_id required");
    const { data } = await sb
      .from("sse_events")
      .select("*")
      .eq("job_id", jobId)
      .order("step", { ascending: true });
    return jsonResponse(data || []);
  }

  if (req.method !== "POST") return errorResponse("POST required", 405);
  const body = await req.json();

  try {
    if (action === "assign-client") {
      const { pipeline_id, client_name } = body;
      if (!pipeline_id || !client_name) return errorResponse("pipeline_id and client_name required");

      const jobId = crypto.randomUUID();

      // Process inline (Edge Functions don't support background tasks)
      try {
        await writeEvent(sb, jobId, 1, "running", `Looking up SmartLead client: ${client_name}`);
        const clients = (await slGetClients()) as Array<Record<string, unknown>>;
        let client = clients.find((c) => (c.name as string)?.toLowerCase() === client_name.toLowerCase());
        if (!client) {
          const created = await slPost("/client", { name: client_name });
          client = created as Record<string, unknown>;
          await writeEvent(sb, jobId, 1, "done", `Created new SmartLead client: ${client_name}`);
        } else {
          await writeEvent(sb, jobId, 1, "done", `Found existing SmartLead client: ${(client as Record<string, unknown>).name}`);
        }

        await writeEvent(sb, jobId, 2, "running", "Checking SmartLead tags...");
        const tags = await slGetAllTags();
        await writeEvent(sb, jobId, 2, "done", `Found ${(tags as unknown[]).length} tags`);

        await writeEvent(sb, jobId, 3, "done", "Assignment complete", { client });
      } catch (e) {
        await writeEvent(sb, jobId, 0, "error", (e as Error).message);
      }

      return jsonResponse({ job_id: jobId, status: "completed" });
    }

    if (action === "delete-infra" || action === "transition") {
      return errorResponse(
        `${action} is not yet supported via Edge Functions. Use the local dashboard.py.`,
        501,
      );
    }

    return errorResponse("Unknown action. Valid: assign-client, delete-infra, transition, poll", 400);
  } catch (e) {
    return errorResponse(`SSE ops error: ${(e as Error).message}`, 500);
  }
});
