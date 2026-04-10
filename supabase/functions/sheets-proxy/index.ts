import { corsResponse, jsonResponse, errorResponse } from "../_shared/cors.ts";

const SHEET_ID = Deno.env.get("GOOGLE_SHEET_ID") || "1oQZh0NnvJPtbgsBqQq-Fy6kbw_UG3Bo-zzdVeEtfMVc";
const MASTER_TAB = "THT Domains "; // trailing space is intentional

async function getAccessToken(): Promise<string> {
  const clientId = Deno.env.get("GOOGLE_CLIENT_ID") || "";
  const clientSecret = Deno.env.get("GOOGLE_CLIENT_SECRET") || "";
  const refreshToken = Deno.env.get("GOOGLE_REFRESH_TOKEN") || "";

  const r = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      refresh_token: refreshToken,
      grant_type: "refresh_token",
    }),
  });
  const data = (await r.json()) as Record<string, string>;
  if (!data.access_token) throw new Error(`OAuth token refresh failed: ${JSON.stringify(data)}`);
  return data.access_token;
}

async function sheetsGet(range: string): Promise<unknown> {
  const token = await getAccessToken();
  const r = await fetch(
    `https://sheets.googleapis.com/v4/spreadsheets/${SHEET_ID}/values/${encodeURIComponent(range)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  return r.json();
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return corsResponse();

  const url = new URL(req.url);
  const action = url.searchParams.get("action");

  try {
    if (action === "domain-inventory") {
      const data = await sheetsGet(`${MASTER_TAB}!A1:Z`);
      return jsonResponse(data);
    }

    if (action === "read-range") {
      const tab = url.searchParams.get("tab") || MASTER_TAB;
      const range = url.searchParams.get("range") || "A1:Z";
      const data = await sheetsGet(`${tab}!${range}`);
      return jsonResponse(data);
    }

    return errorResponse("Unknown action. Valid: domain-inventory, read-range", 400);
  } catch (e) {
    return errorResponse(`Sheets proxy error: ${(e as Error).message}`, 500);
  }
});
