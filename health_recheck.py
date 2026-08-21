"""Dry-run the fixed scorer and diff it against the live statuses.

Writes nothing. Exists because the sent_3d fix moves the volume gate for the
whole fleet at once, and a change of that blast radius should be measured
before it lands, not discovered afterwards on the dashboard.

    python health_recheck.py            # summary + biggest movements
    python health_recheck.py --verbose  # every inbox that changes status
"""

from __future__ import annotations

import collections
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import db as store            # noqa: E402
import health_daily as hd     # noqa: E402
import health_model as hm     # noqa: E402
import health_snapshot as hs  # noqa: E402

ORDER = ["healthy", "watch", "at_risk", "burned", "insufficient", "idle", "warming"]


def main(verbose: bool = False) -> None:
    overview, ts = store.cache_get("overview_v2")
    if not overview:
        sys.exit("overview_v2 cache empty")
    fleet = hs.build_fleet_from_overview(overview)
    print(f"overview cache: {ts}  ({len(fleet)} inboxes)")

    # true daily totals for the last N complete days -> pick the window
    dates = hd.complete_days(hd.HISTORY_DAYS)
    sent_by_date = {}
    for d in dates:
        w = hd.fetch_window(d, d)
        sent_by_date[d] = sum(x["sent"] for x in w.values())
    live = hd.sending_days(sent_by_date)
    print("\nreal sending calendar (complete days):")
    for d in dates:
        print("  %s  %-7d %s" % (d, sent_by_date[d],
                                 "sending day" if d in live else "-- not a sending day"))

    window = hd.choose_window(sent_by_date, hd.WINDOW_DAYS)
    print(f"\nscoring window: {window[0]}..{window[-1]}  "
          f"({len([d for d in window if d in live])} sending days"
          f"{', spans a weekend' if len(window) > hd.WINDOW_DAYS else ''})")

    window_sig, _ = hd.window_signals(sent_by_date=sent_by_date)
    cfg = store.get_health_config("default") or {}
    min_sent = int(cfg.get("min_sent_3d", hm.DEFAULT_CONFIG["min_sent_3d"]))

    before = {r["email"]: r for r in store.get_health_status_all()}
    history = store.get_health_daily_bulk(dates[0])

    after, moves = {}, []
    for r in fleet:
        w = window_sig.get(r["email"])
        sig = hm.rolling(history.get(r["email"], []), days=3)
        if w:
            sig.update({k: w[k] for k in ("reply", "bounce", "sent_3d")})
        else:
            sig["sent_3d"] = 0
        sig["in_warmup"] = r["in_warmup"]
        sig["smtp_ok"] = r["smtp_ok"]
        sig["in_campaign"] = r["in_campaign"]
        res = hm.score_inbox(sig, cfg)
        after[r["email"]] = res
        old = (before.get(r["email"]) or {}).get("status")
        if old and old != res["status"]:
            moves.append({
                "email": r["email"], "from": old, "to": res["status"],
                "old_sent": (before[r["email"]] or {}).get("sent_3d"),
                "new_sent": sig["sent_3d"],
                "old_bounce": (before[r["email"]] or {}).get("bounce_3d"),
                "new_bounce": sig.get("bounce"),
                "why": "; ".join(res["reasons"])[:90],
            })

    print("\n%-14s %8s %8s %8s" % ("status", "before", "after", "delta"))
    b = collections.Counter(v.get("status") for v in before.values()
                            if v.get("status"))
    a = collections.Counter(v["status"] for v in after.values())
    for k in ORDER:
        d = a[k] - b[k]
        print("  %-12s %8d %8d %+8d" % (k, b[k], a[k], d))
    print("  %-12s %8d %8d" % ("TOTAL", sum(b.values()), sum(a.values())))

    print(f"\nstatus changes: {len(moves)} of {len(fleet)} inboxes")
    for (f, t), n in collections.Counter((m["from"], m["to"]) for m in moves).most_common():
        print("  %-12s -> %-12s %5d" % (f, t, n))

    sents = sorted(s["sent_3d"] for s in window_sig.values() if s["sent_3d"] > 0)
    if sents:
        print(f"\ntrue sent over the window (sending inboxes only, n={len(sents)}):")
        print("  median %d | p10 %d | p90 %d | max %d"
              % (sents[len(sents) // 2], sents[int(len(sents) * .1)],
                 sents[int(len(sents) * .9)], sents[-1]))
        under = sum(1 for v in sents if v < min_sent)
        print("  below min_sent_3d=%d: %d of %d sending (%.0f%%)"
              % (min_sent, under, len(sents), 100 * under / len(sents)))
        old_sents = sorted(int(v.get("sent_3d") or 0) for v in before.values()
                           if int(v.get("sent_3d") or 0) > 0)
        if old_sents:
            print("  (previously reported median %d — inflated %.1fx)"
                  % (old_sents[len(old_sents) // 2],
                     old_sents[len(old_sents) // 2] / max(sents[len(sents) // 2], 1)))

    unburn = [m for m in moves if m["from"] == "burned"]
    if unburn:
        print(f"\nno longer burned ({len(unburn)}) — pooled rates replace averaged daily rates:")
        for m in unburn[:8]:
            print("   %-42s bounce %s%% -> %s%%  (%s)"
                  % (m["email"][:42], m["old_bounce"], m["new_bounce"], m["to"]))

    newburn = [m for m in moves if m["to"] == "burned"]
    if newburn:
        print(f"\nnewly burned ({len(newburn)}):")
        for m in newburn[:8]:
            print("   %-42s %s" % (m["email"][:42], m["why"]))

    if verbose:
        print("\nevery change:")
        for m in sorted(moves, key=lambda x: (x["from"], x["to"], x["email"])):
            print("  %-44s %-12s -> %-12s sent %s->%s"
                  % (m["email"][:44], m["from"], m["to"], m["old_sent"], m["new_sent"]))

    print("\n(dry run — nothing written)")


if __name__ == "__main__":
    main(verbose="--verbose" in sys.argv)
