import _envload; _envload.load()
import os, requests, json
K=os.environ["SMARTLEAD_API_KEY"]
S="https://server.smartlead.ai/api/v1"
r=requests.get(f"{S}/campaigns", params={"api_key":K}, timeout=60); r.raise_for_status()
cs=r.json()
print("total campaigns", len(cs))
for c in cs:
    n=(c.get("name") or "")
    if any(k in n.lower() for k in ("merry","bright","brite","m&b")):
        print(f'{c["id"]:8} {c.get("status"):10} {n!r}  created={str(c.get("created_at"))[:10]}')
