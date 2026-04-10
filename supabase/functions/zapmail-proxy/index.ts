import { corsResponse, jsonResponse, errorResponse } from "../_shared/cors.ts";
import { zmGet, zmListDomains, zmDelete } from "../_shared/zapmail.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return corsResponse();

  const url = new URL(req.url);
  const action = url.searchParams.get("action");

  try {
    if (action === "domains") {
      return jsonResponse(await zmListDomains());
    }

    if (action === "wallet") {
      return jsonResponse(await zmGet("/v2/wallet"));
    }

    if (action === "subscriptions") {
      return jsonResponse(await zmGet("/v2/subscriptions"));
    }

    if (action === "placement-tests") {
      const [eligible, credits] = await Promise.all([
        zmGet("/v2/placement/eligible-mailboxes"),
        zmGet("/v2/placement/credits"),
      ]);
      return jsonResponse({ eligible, credits });
    }

    if (action === "cancel") {
      if (req.method !== "POST") return errorResponse("POST required", 405);
      const body = await req.json();
      const domainIds = body.domain_ids as string[];
      if (!domainIds?.length) return errorResponse("domain_ids required");
      // ZapMail bulk delete endpoint
      const r = await fetch("https://api.zapmail.ai/api/v2/domains", {
        method: "DELETE",
        headers: {
          "x-auth-zapmail": Deno.env.get("ZAPMAIL_API_KEY") || "",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ domainIds }),
      });
      const result = r.status === 200 ? await r.json() : { error: await r.text(), status: r.status };
      return jsonResponse(result);
    }

    if (action === "sync") {
      return jsonResponse(await zmListDomains());
    }

    return errorResponse("Unknown action. Valid: domains, wallet, subscriptions, placement-tests, cancel, sync", 400);
  } catch (e) {
    return errorResponse(`ZapMail proxy error: ${(e as Error).message}`, 500);
  }
});
