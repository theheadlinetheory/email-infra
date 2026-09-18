import json, collections
S="/private/tmp/claude-501/-Users-timdwivedi-Desktop-THT/551e963c-066d-4f6e-8a21-29a529cee587/scratchpad/gc.json"
d=json.load(open(S))
ib=d["inboxes"]
print("keys:", sorted(ib[0].keys()))
print("\n=== MERRY / BRIGHT / MARY matches ===")
for i in ib:
    if any(k in (i.get("tag") or "").lower()+" "+(i.get("owner") or "").lower() for k in ("merry","brite","bright")):
        print(f'{i["email"]:45} tag={i.get("tag")!r:35} owner={i.get("owner")!r:25} niche={i["niche"]:11} state={i["state"]:9} age={i.get("age_days")} camps={i.get("active_campaigns")}')
