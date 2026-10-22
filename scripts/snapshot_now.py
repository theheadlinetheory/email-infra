#!/usr/bin/env python3
"""Run today's health snapshot from a laptop, correctly.

The Vercel route is the normal path and this is the stopgap for when it is
failing. Running `snapshot_daily()` by hand is easy to get wrong in three ways
that all look like success, so they are handled here rather than remembered:

1. **The dashboard does not move.** `db._CACHE_WRITE_ENABLED` is False unless
   something sets it, and only the Vercel `/api/health-snapshot` route does. A
   plain local run writes `inbox_health_status`, the daily rows and
   `burn_streaks` but NOT the `health_fleet` cache the Inboxes tab reads — so
   the burn list on screen does not change and it looks like the run did
   nothing.
2. **The date is wrong.** Without `today=`, the snapshot takes the machine's
   date. Run from a timezone ahead of New York and every burn streak advances a
   day early — that is how 7 burned once became 60.
3. **The failure latch stays set.** Without `record_run`, the "snapshot failed"
   state and its Slack alert stay latched even after a good run.

It also waits out a blank Smartlead session rather than recording a failure:
Smartlead intermittently hands a team-member login a session that authenticates
but sees nothing (`auth/me` returns 21 bytes instead of ~327). Measured
2026-10-22: one episode cleared in about 3 minutes; an earlier one took ~30.
Re-minting does not help — it is server-side.

Usage:
    python scripts/snapshot_now.py --check     # session + freshness, no writes
    python scripts/snapshot_now.py             # run it
    python scripts/snapshot_now.py --date 2026-10-22
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# New York is what Vercel and the rest of the pipeline call "today".
NY = ZoneInfo("America/New_York")
BLANK_WAIT_SECONDS = 60
BLANK_MAX_WAIT_MINUTES = 40


def ny_today() -> str:
    return datetime.now(NY).strftime("%Y-%m-%d")


def wait_for_session(hd, max_minutes: int = BLANK_MAX_WAIT_MINUTES) -> bool:
    """Block until Smartlead's session sees an account again."""
    deadline = time.time() + max_minutes * 60
    first = True
    while time.time() < deadline:
        sees = hd.session_sees_account()
        if sees:
            if not first:
                print("  session recovered")
            return True
        if sees is None:
            print("  could not check the session (auth/me unreadable) — retrying")
        elif first:
            print("  Smartlead session is BLANK (auth/me empty). This is on "
                  "their side and clears by itself; waiting.")
        first = False
        time.sleep(BLANK_WAIT_SECONDS)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="snapshot date (default: today in New York)")
    ap.add_argument("--check", action="store_true",
                    help="report session and freshness, write nothing")
    args = ap.parse_args(argv)

    import db as store
    import health_daily as hd
    import health_snapshot as hs

    today = args.date or ny_today()
    machine = datetime.now().strftime("%Y-%m-%d")
    print(f"snapshot date : {today} (New York)")
    if machine != today:
        print(f"  note: this machine says {machine}; using the New York date so "
              f"burn streaks do not advance early")

    sees = hd.session_sees_account()
    print(f"smartlead session sees an account: {sees}")
    try:
        print(f"freshness before: {json.dumps(hd.health_freshness(), default=str)}")
    except Exception as e:                                   # noqa: BLE001
        print(f"freshness before: unavailable ({e})")

    if args.check:
        print("\n--check: nothing was written.")
        return 0

    if not sees and not wait_for_session(hd):
        print(f"\nSmartlead's session was still blank after "
              f"{BLANK_MAX_WAIT_MINUTES} minutes. Nothing was written — a "
              f"snapshot built on an empty read would clear the burn list.")
        return 2

    # Only the Vercel route sets this. Without it the Inboxes tab never moves.
    store._CACHE_WRITE_ENABLED = True

    t0 = time.time()
    out = hs.snapshot_daily(today=today)
    ok = bool(out.get("ok", True)) and not out.get("error")
    hs.record_run(ok, str(out.get("error") or "")[:300])

    print(f"\nran in {time.time() - t0:.0f}s   ok={ok}")
    print(json.dumps({k: v for k, v in out.items() if k != "rows"},
                     default=str)[:900])
    if not ok:
        return 1

    # Verify the artefact, not the return value: the point of the run is that
    # the Inboxes tab changes.
    try:
        fleet, ts = store.cache_get("health_fleet")
        inboxes = (fleet or {}).get("inboxes") or []
        burned = [i for i in inboxes if i.get("status") == "burned"]
        print(f"\nhealth_fleet cache: built_at={ts} inboxes={len(inboxes)} "
              f"burned={len(burned)}")
        for i in burned:
            print(f"   BURNED {i.get('email')}  ({i.get('client')})")
    except Exception as e:                                   # noqa: BLE001
        print(f"could not re-read health_fleet: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
