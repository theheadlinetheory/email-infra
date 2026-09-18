import _envload; _envload.load()
import os, requests, json
K=os.environ["SMARTLEAD_API_KEY"]; S="https://server.smartlead.ai/api/v1"; CID=3902213
def g(p, **kw):
    r=requests.get(f"{S}/campaigns/{CID}{p}", params={"api_key":K, **kw}, timeout=60)
    return r.status_code, (r.json() if r.status_code==200 else r.text[:200])
for p in ("", "/email-accounts", "/sequences", "/leads?offset=0&limit=1"):
    sc, d = g(p.split("?")[0], **(dict(offset=0,limit=1) if "leads" in p else {}))
    if p=="/email-accounts":
        print("email-accounts:", sc, len(d) if isinstance(d,list) else d)
        for a in (d if isinstance(d,list) else [])[:5]: print("   ", a.get("from_email"))
    elif p=="/sequences":
        print("sequences:", sc, len(d) if isinstance(d,list) else d)
        for s in (d if isinstance(d,list) else []): print("   step", s.get("seq_number"), "delay", s.get("seq_delay_details"), "subj", (s.get("subject") or (s.get("seq_variants") or [{}])[0].get("subject"))[:60] if (s.get("subject") or s.get("seq_variants")) else None)
    elif p=="":
        print("campaign:", sc, json.dumps({k:d.get(k) for k in ("id","name","status","created_at","track_settings","scheduler_cron_value","min_time_btwn_emails","max_leads_per_day","client_id")}, default=str)[:600])
    else:
        print("leads:", sc, (d.get("total_leads") if isinstance(d,dict) else d))
