"""Blocklist surveillance for the whole sending fleet.

A URI blocklist listing is invisible from inside SmartLead: the campaign keeps
sending, the mailbox keeps reporting healthy, and the damage only surfaces as a
line buried in a bounce body ("A URL in this email is listed on surbl.org").
The 2026-08-18 audit found 57 of 76 acquisition domains listed on SURBL while
the standing assumption was three, purely because nobody had ever queried the
full set. This module makes that a daily check instead of a discovery.

WHY THE CONTROLS ARE NOT OPTIONAL
---------------------------------
Every DNSBL answers "not listed" as NXDOMAIN, which is indistinguishable from a
resolver failure unless you look. SURBL additionally answers 127.0.0.1 to every
query from a blocked or over-quota resolver, which reads as "everything is
listed". Both failure modes produce a confident and completely wrong report.

So each run first asserts two controls per zone -- a known-listed test point and
a known-clean domain -- and refuses to report on a zone whose controls disagree.
Within the sweep, NXDOMAIN means clean, an exception after retries means
UNKNOWN, and UNKNOWN is never folded into either bucket.

(Learned the hard way: an earlier pass in this investigation used
socket.gethostbyname_ex and nslookup at high concurrency, read transient
failures as "record absent", and reported 24 domains with no website and 26 with
no DMARC. All 76 had both.)
"""

from __future__ import annotations

import os
import time

import requests

import db as store

_BASE = "https://server.smartlead.ai/api/v1"
_TIMEOUT = 20
# SmartLead REST sub-endpoints 403 on a default python User-Agent.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_SURBL_ZONE = "multi.surbl.org"
_ZONES = {
    "surbl": _SURBL_ZONE,
    "uribl": "multi.uribl.com",
    "dbl": "dbl.spamhaus.org",
}
# What each SURBL bit means, so an alert says why rather than just an IP.
_SURBL_BITS = {
    "127.0.0.8": "phishing",
    "127.0.0.16": "malware",
    "127.0.0.64": "abuse (spam)",
    "127.0.0.128": "cracked site",
}
# (probe domain, must_be_listed) -- asserted before any real lookup is trusted.
_CONTROLS = {
    "surbl": [("test.surbl.org", True), ("google.com", False)],
    "uribl": [("test.uribl.com", True), ("google.com", False)],
    "dbl": [("dbltest.com", True), ("google.com", False)],
}

_RETRIES = 3
_PACE = 0.05


def _key() -> str:
    return (os.environ.get("SMARTLEAD_API_KEY", "")
            or os.environ.get("SMARTLEAD_KEY", "")).strip()


def _resolver(nameservers: list[str] | None = None, timeout: float = 4.0):
    """A resolver, optionally pinned to specific nameservers.

    Deliberately NOT a public resolver by default: SURBL blocks queries from
    8.8.8.8 and friends, and answers 127.0.0.1 to everything when it does.
    """
    import dns.resolver
    r = dns.resolver.Resolver()
    if nameservers:
        r.nameservers = list(nameservers)
    r.timeout = timeout
    r.lifetime = timeout * 2
    return r


def _pick_nameservers() -> tuple[list[str], list[dict]]:
    """Keep only the configured nameservers that actually answer DNSBL queries.

    A box can list several resolvers where only one serves these zones; dnspython
    burns the full timeout on each dead one before falling through, which turned a
    7-second sweep into a 15-minute one. Probing once up front and pinning the
    survivors is the difference between the check being daily and being abandoned.
    """
    import dns.resolver
    try:
        configured = list(dns.resolver.Resolver().nameservers)
    except Exception:
        return [], []
    good, report = [], []
    for ns in configured:
        r = _resolver([ns], timeout=3.0)
        t0 = time.time()
        listed = _lookup(r, f"test.surbl.org.{_SURBL_ZONE}", retries=1)
        clean = _lookup(r, f"google.com.{_SURBL_ZONE}", retries=1)
        ok = bool(listed) and clean == [] and listed != ["127.0.0.1"]
        report.append({"nameserver": ns, "ok": ok, "seconds": round(time.time() - t0, 2)})
        if ok:
            good.append(ns)
    return good, report


def _lookup(res, name: str, retries: int | None = None) -> list[str] | None:
    """[] = not listed (NXDOMAIN). [ips] = listed. None = UNKNOWN, never guessed."""
    import dns.resolver
    for attempt in range(retries if retries is not None else _RETRIES):
        try:
            return [x.address for x in res.resolve(name, "A")]
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    return None


def _check_controls(res, zone_key: str) -> dict:
    """Refuse to trust a zone whose controls do not behave as documented."""
    zone = _ZONES[zone_key]
    problems = []
    for probe, must_be_listed in _CONTROLS[zone_key]:
        got = _lookup(res, f"{probe}.{zone}")
        if got is None:
            problems.append(f"{probe}: no answer after {_RETRIES} tries")
        elif got == ["127.0.0.1"]:
            problems.append(f"{probe}: 127.0.0.1 -- this resolver is blocked")
        elif must_be_listed and not got:
            problems.append(f"{probe}: expected LISTED, got clean")
        elif not must_be_listed and got:
            problems.append(f"{probe}: expected clean, got {got}")
    return {"ok": not problems, "problems": problems}


def fleet_domains() -> list[str]:
    """Every distinct sending domain in the overview cache.

    Cache-derived, so it can lag a very fresh purchase by a day. Fine for
    surveillance, unlike offboarding where trusting this cache has bitten us.
    """
    ov, _ = store.cache_get("overview_v2")
    if not ov:
        return []
    buckets = []
    for c in ov.get("clients", []):
        for letter in ("a", "b"):
            buckets.append((c.get(f"group_{letter}") or {}).get("account_details", []))
    for section in ("generic_groups", "acquisition_groups"):
        for g in ov.get(section, []):
            buckets.append(g.get("account_details", []))
    doms: set[str] = set()
    for details in buckets:
        for ad in details or []:
            em = (ad.get("email") or "").strip().lower()
            if "@" in em:
                doms.add(em.split("@", 1)[1])
    return sorted(doms)


def campaign_domains(campaign_ids: list[int]) -> list[str]:
    """Live read of the domains sending specific campaigns (no cache)."""
    key = _key()
    if not key:
        return []
    doms: set[str] = set()
    for cid in campaign_ids:
        try:
            r = requests.get(f"{_BASE}/campaigns/{cid}/email-accounts",
                             params={"api_key": key},
                             headers={"User-Agent": _UA}, timeout=_TIMEOUT)
            r.raise_for_status()
            for a in r.json() or []:
                em = (a.get("from_email") or "").strip().lower()
                if "@" in em:
                    doms.add(em.split("@", 1)[1])
        except Exception:
            continue
    return sorted(doms)


def scan(domains: list[str] | None = None,
         zones: tuple[str, ...] = ("surbl", "uribl", "dbl")) -> dict:
    """Check every domain against each zone.

    Returns {"ok": False, ...} rather than a wrong answer when the controls
    fail. Callers must check "ok" before believing "listed".
    """
    try:
        servers, ns_report = _pick_nameservers()
    except ImportError:
        return {"ok": False, "error": "dnspython not installed (pip install dnspython)"}
    if not servers:
        return {"ok": False, "error": "no configured nameserver answers DNSBL queries",
                "nameservers": ns_report, "checked": 0}
    res = _resolver(servers)

    domains = domains if domains is not None else fleet_domains()
    if not domains:
        return {"ok": False, "error": "no sending domains found"}

    control = {z: _check_controls(res, z) for z in zones}
    trusted = [z for z in zones if control[z]["ok"]]
    if not trusted:
        return {"ok": False, "error": "all blocklist controls failed",
                "controls": control, "checked": 0}

    listings: dict[str, dict[str, str]] = {}
    unknown: dict[str, list[str]] = {}
    for d in domains:
        for z in trusted:
            got = _lookup(res, f"{d}.{_ZONES[z]}")
            if got is None:
                unknown.setdefault(d, []).append(z)
            elif got:
                listings.setdefault(d, {})[z] = ", ".join(
                    _SURBL_BITS.get(ip, ip) if z == "surbl" else ip for ip in got)
            time.sleep(_PACE)

    # Re-assert controls AFTER the sweep: SURBL and URIBL cut off a resolver that
    # exceeds its free-use quota mid-run, and everything queried after that point
    # would be silently wrong. A start-of-run check alone cannot catch this.
    post = {z: _check_controls(res, z) for z in trusted}
    burned = [z for z in trusted if not post[z]["ok"]]
    if burned:
        return {"ok": False,
                "error": f"controls failed AFTER the sweep on {','.join(burned)} "
                         "-- likely resolver quota exhausted mid-run; results discarded",
                "controls": control, "controls_post": post, "checked": len(domains)}

    return {
        "ok": True,
        "checked": len(domains),
        "nameservers": ns_report,
        "zones_trusted": trusted,
        "zones_skipped": [z for z in zones if z not in trusted],
        "controls": control,
        "listed": listings,
        "listed_count": len(listings),
        "unknown": unknown,
        "clean_count": len(domains) - len(listings) - len(unknown),
    }


def diff_against_last(result: dict) -> dict:
    """Newly listed / newly cleared since the previous stored scan."""
    if not result.get("ok"):
        return {"new": [], "cleared": []}
    try:
        prev = (store.get_state("blocklist_last_scan") or {}).get("listed") or {}
    except Exception:
        # No baseline reachable (offline / creds missing) is not a reason to
        # discard a good scan -- report it as a first run instead.
        return {"new": [], "cleared": [], "no_baseline": True}
    now = result["listed"]
    return {"new": sorted(set(now) - set(prev)),
            "cleared": sorted(set(prev) - set(now))}


def save(result: dict) -> bool:
    if not result.get("ok"):
        return False
    try:
        store.set_state("blocklist_last_scan",
                        {"listed": result["listed"], "checked": result["checked"]})
        return True
    except Exception:
        return False


def summary_line(result: dict, delta: dict | None = None) -> str:
    if not result.get("ok"):
        return f"Blocklist check FAILED (not reporting): {result.get('error')}"
    bits = [f"{result['listed_count']}/{result['checked']} sending domains listed"]
    if delta and delta.get("new"):
        bits.append(f"NEW: {', '.join(delta['new'][:5])}")
    if delta and delta.get("cleared"):
        bits.append(f"cleared: {len(delta['cleared'])}")
    if result["unknown"]:
        bits.append(f"{len(result['unknown'])} unknown")
    if result["zones_skipped"]:
        bits.append(f"skipped {','.join(result['zones_skipped'])} (controls failed)")
    return " | ".join(bits)


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")

    doms = None
    if "--campaigns" in sys.argv:
        ids = [int(x) for x in sys.argv[sys.argv.index("--campaigns") + 1].split(",")]
        doms = campaign_domains(ids)
    elif "--domains" in sys.argv:
        doms = sys.argv[sys.argv.index("--domains") + 1].split(",")

    out = scan(doms)
    delta = diff_against_last(out)
    print(summary_line(out, delta))
    if not out.get("ok"):
        print(json.dumps(out.get("controls", {}), indent=1))
        sys.exit(1)
    for d, z in sorted(out["listed"].items()):
        print(f"  LISTED  {d:40s} " + "; ".join(f"{k}={v}" for k, v in z.items()))
    for d, z in sorted(out.get("unknown", {}).items()):
        print(f"  UNKNOWN {d:40s} ({','.join(z)})")
    if "--save" in sys.argv:
        print("saved as baseline" if save(out) else "could not save baseline (db unreachable)")
