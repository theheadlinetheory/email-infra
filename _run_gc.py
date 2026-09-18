import _envload; _envload.load()
import json, _gc_live as gc
d = gc.build(live=True)
if d.get("error"):
    print("ERROR:", d["error"]); raise SystemExit(1)
json.dump(d, open("/private/tmp/claude-501/-Users-timdwivedi-Desktop-THT/551e963c-066d-4f6e-8a21-29a529cee587/scratchpad/gc.json","w"))
print("generated_at", d["generated_at"])
for v, x in d["verticals"].items():
    print(f'{v:12} total={x["total_inboxes"]:4} avail={x["available_inboxes"]:4} warming={x["warming_inboxes"]:4} sending={x["sending_inboxes"]:4} disputed={x["disputed_inboxes"]:3} claimed={x["claimed_inboxes"]:3} blocked={x["blocked_inboxes"]:3} ready_in={x["warming_ready_in_days"]}')
print("summary", json.dumps(d["summary"], indent=1))
