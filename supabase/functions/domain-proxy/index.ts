import { corsResponse, jsonResponse, errorResponse } from "../_shared/cors.ts";
import { porkbunPost, spaceshipGet } from "../_shared/domains.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return corsResponse();

  const url = new URL(req.url);
  const action = url.searchParams.get("action");

  try {
    if (action === "list") {
      // Porkbun domains
      const pbRaw = (await porkbunPost("/domain/listAll")) as Record<string, unknown>;
      const pbDomains = ((pbRaw?.domains as Array<Record<string, unknown>>) || []).map((d) => ({
        domain: d.domain || "",
        registrar: "porkbun",
        status: d.status || "UNKNOWN",
        expires: ((d.expireDate as string) || "").slice(0, 10),
        auto_renew: d.autoRenew === "1",
        created: ((d.createDate as string) || "").slice(0, 10),
      }));

      // Spaceship domains (paginated)
      const spDomains: Array<Record<string, unknown>> = [];
      let skip = 0;
      while (true) {
        const data = (await spaceshipGet(`/domains?take=100&skip=${skip}`)) as Record<string, unknown>;
        const items = (data?.items as Array<Record<string, unknown>>) || [];
        for (const d of items) {
          spDomains.push({
            domain: d.name || "",
            registrar: "spaceship",
            status: d.lifecycleStatus || "UNKNOWN",
            expires: ((d.expirationDate as string) || "").slice(0, 10),
            auto_renew: d.autoRenew || false,
            created: ((d.registrationDate as string) || "").slice(0, 10),
          });
        }
        if (items.length < 100) break;
        skip += 100;
      }

      return jsonResponse({ porkbun: pbDomains, spaceship: spDomains });
    }

    if (action === "auto-renew") {
      if (req.method !== "POST") return errorResponse("POST required", 405);
      const body = await req.json();
      const { domain, registrar, enabled } = body;
      if (!domain || !registrar) return errorResponse("domain and registrar required");
      if (registrar === "porkbun") {
        const result = (await porkbunPost(`/domain/updateAutoRenew/${domain}`, {
          status: enabled ? "on" : "off",
        })) as Record<string, unknown>;
        return jsonResponse({
          success: result.status === "SUCCESS",
          message: result.message || "",
        });
      }
      return errorResponse(`${registrar} auto-renew not supported via API`);
    }

    return errorResponse("Unknown action. Valid: list, auto-renew", 400);
  } catch (e) {
    return errorResponse(`Domain proxy error: ${(e as Error).message}`, 500);
  }
});
