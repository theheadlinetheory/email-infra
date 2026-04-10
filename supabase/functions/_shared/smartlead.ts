const API = "https://server.smartlead.ai/api/v1";
const INTERNAL_API = "https://server.smartlead.ai/api";

function apiKey(): string {
  return Deno.env.get("SMARTLEAD_API_KEY") || "";
}

function jwt(): string {
  return Deno.env.get("SMARTLEAD_JWT") || "";
}

export function internalHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${jwt()}`, "Content-Type": "application/json" };
}

export async function slGet(path: string, params: Record<string, string> = {}): Promise<unknown> {
  const url = new URL(`${API}${path}`);
  url.searchParams.set("api_key", apiKey());
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const r = await fetch(url.toString(), { headers: { "Content-Type": "application/json" } });
  return r.json();
}

export async function slPost(path: string, body: unknown): Promise<unknown> {
  const url = `${API}${path}?api_key=${apiKey()}`;
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function slInternalGet(path: string): Promise<unknown> {
  const r = await fetch(`${INTERNAL_API}${path}`, { headers: internalHeaders() });
  return r.json();
}

export async function slInternalPost(path: string, body: unknown): Promise<unknown> {
  const r = await fetch(`${INTERNAL_API}${path}`, {
    method: "POST",
    headers: internalHeaders(),
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function slListAccounts(offset = 0, limit = 100): Promise<unknown[]> {
  const data = await slGet("/email-accounts/", { offset: String(offset), limit: String(limit) });
  return Array.isArray(data) ? data : [];
}

export async function slGetAllAccounts(): Promise<unknown[]> {
  const all: unknown[] = [];
  let offset = 0;
  while (true) {
    const batch = await slListAccounts(offset, 100);
    all.push(...batch);
    if (batch.length < 100) break;
    offset += 100;
  }
  return all;
}

export async function slGetClients(): Promise<unknown[]> {
  const data = await slGet("/client");
  return Array.isArray(data) ? data : [];
}

export async function slGetAllTags(): Promise<unknown[]> {
  const data = await slGet("/email-accounts/tags");
  return Array.isArray(data) ? data : [];
}
