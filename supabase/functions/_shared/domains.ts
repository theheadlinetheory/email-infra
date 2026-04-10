const PORKBUN_API = "https://api.porkbun.com/api/json/v3";
const SPACESHIP_API = "https://spaceship.dev/api/v1";

function porkbunAuth(): Record<string, string> {
  return {
    apikey: Deno.env.get("PORKBUN_API_KEY") || "",
    secretapikey: Deno.env.get("PORKBUN_SECRET_KEY") || "",
  };
}

function spaceshipHeaders(): Record<string, string> {
  return {
    "X-Api-Key": Deno.env.get("SPACESHIP_API_KEY") || "",
    "X-Api-Secret": Deno.env.get("SPACESHIP_SECRET_KEY") || "",
    "Content-Type": "application/json",
  };
}

export async function porkbunPost(path: string, extra: Record<string, unknown> = {}): Promise<unknown> {
  const r = await fetch(`${PORKBUN_API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...porkbunAuth(), ...extra }),
  });
  return r.json();
}

export async function spaceshipGet(path: string): Promise<unknown> {
  const r = await fetch(`${SPACESHIP_API}${path}`, { headers: spaceshipHeaders() });
  return r.json();
}
