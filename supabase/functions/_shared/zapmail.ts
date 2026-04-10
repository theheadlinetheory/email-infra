const API = "https://api.zapmail.ai/api";

function headers(): Record<string, string> {
  return { "x-auth-zapmail": Deno.env.get("ZAPMAIL_API_KEY") || "", "Content-Type": "application/json" };
}

export async function zmGet(path: string): Promise<unknown> {
  const r = await fetch(`${API}${path}`, { headers: headers() });
  return r.json();
}

export async function zmPost(path: string, body: unknown): Promise<unknown> {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function zmPut(path: string, body: unknown): Promise<unknown> {
  const r = await fetch(`${API}${path}`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function zmDelete(path: string): Promise<unknown> {
  const r = await fetch(`${API}${path}`, { method: "DELETE", headers: headers() });
  if (r.status === 204) return { ok: true };
  return r.json();
}

export async function zmListDomains(): Promise<unknown[]> {
  const all: unknown[] = [];
  let page = 1;
  while (true) {
    const data = await zmGet(`/v2/domains?page=${page}`) as Record<string, unknown>;
    const domains = ((data?.data as Record<string, unknown>)?.domains as unknown[]) || [];
    all.push(...domains);
    const lastPage = ((data?.data as Record<string, unknown>)?.last_page as number) || 1;
    if (page >= lastPage) break;
    page++;
  }
  return all;
}
