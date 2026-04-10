import { corsResponse, jsonResponse, errorResponse } from "../_shared/cors.ts";
import { getServiceClient } from "../_shared/supabase.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return corsResponse();

  const url = new URL(req.url);
  const action = url.searchParams.get("action");
  const sb = getServiceClient();

  try {
    if (action === "active") {
      const { data, error } = await sb
        .from("pipelines")
        .select("data")
        .in("status", ["running", "pending", "waiting"]);
      if (error) return errorResponse(error.message, 500);
      const pipelines = (data || []).map((r: Record<string, unknown>) =>
        typeof r.data === "string" ? JSON.parse(r.data as string) : r.data,
      );
      return jsonResponse(pipelines);
    }

    if (action === "detail") {
      const pipelineId = url.searchParams.get("id");
      if (!pipelineId) return errorResponse("id required");
      const { data, error } = await sb
        .from("pipelines")
        .select("data")
        .eq("id", pipelineId)
        .single();
      if (error || !data) return errorResponse("Pipeline not found", 404);
      const pipeline = typeof data.data === "string" ? JSON.parse(data.data as string) : data.data;
      return jsonResponse(pipeline);
    }

    // Pipeline creation is too complex for Edge Functions (spawns long-running threads).
    // Use dashboard.py locally for these operations.
    if (action === "new-client" || action === "replacement" || action === "new-acquisition") {
      if (req.method !== "POST") return errorResponse("POST required", 405);
      return errorResponse(
        "Pipeline creation is not yet supported via Edge Functions. Use the local dashboard.py for pipeline operations.",
        501,
      );
    }

    return errorResponse("Unknown action. Valid: active, detail, new-client, replacement, new-acquisition", 400);
  } catch (e) {
    return errorResponse(`Pipeline ops error: ${(e as Error).message}`, 500);
  }
});
