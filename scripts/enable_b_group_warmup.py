"""Enable warmup on all 4 new B group accounts in SmartLead.

Usage:
  python3 enable_b_group_warmup.py          # Execute
  python3 enable_b_group_warmup.py --check   # Just check status
"""
import sys, json, time, os, requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup import SMARTLEAD_KEY, SMARTLEAD_API, SMARTLEAD_INTERNAL_API, SMARTLEAD_JWT

B_GROUP_CLIENT_IDS = {411912, 411913, 411914, 411915}
CLIENT_NAMES = {
    411912: "GM Landscaping B",
    411913: "Tropical Landscaping B",
    411914: "Generic K",
    411915: "Denair HVAC B",
}

def fetch_b_group_accounts():
    all_ours = []
    offset = 0
    while True:
        url = f"{SMARTLEAD_API}/email-accounts?api_key={SMARTLEAD_KEY}&limit=100&offset={offset}"
        r = requests.get(url, timeout=30)
        if r.status_code == 429:
            print("  Rate limited, waiting 60s...")
            time.sleep(60)
            r = requests.get(url, timeout=30)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} at offset {offset}")
            break
        accounts = r.json() if isinstance(r.json(), list) else r.json().get("data", [])
        ours = [a for a in accounts if a.get("client_id") in B_GROUP_CLIENT_IDS]
        all_ours.extend(ours)
        if len(accounts) < 100:
            break
        offset += 100
        time.sleep(1)
    return all_ours

def enable_warmup_public(account_id):
    r = requests.post(
        f"{SMARTLEAD_API}/email-accounts/{account_id}/warmup?api_key={SMARTLEAD_KEY}",
        json={"warmup_enabled": True},
        timeout=15,
    )
    return r.status_code == 200

def configure_warmup_internal(account_id):
    headers = {"Authorization": f"Bearer {SMARTLEAD_JWT}"}
    r = requests.get(
        f"{SMARTLEAD_INTERNAL_API}/email-account/fetch-warmup-details-by-email-account-id/{account_id}",
        headers=headers,
        timeout=15,
    )
    if r.status_code != 200:
        return False
    warmup_key = r.json().get("message", {}).get("warmup_key_id", "")
    if not warmup_key:
        return False
    payload = {
        "emailAccountId": str(account_id),
        "maxEmailPerDay": 15,
        "isRampupEnabled": True,
        "rampupValue": 5,
        "warmupMinCount": 10,
        "warmupMaxCount": 15,
        "replyRate": 40,
        "dailyReplyLimit": 15,
        "autoAdjustWarmup": False,
        "sendWarmupsOnlyOnWeekdays": False,
        "useCustomDomain": False,
        "status": "ACTIVE",
        "warmupKeyId": warmup_key,
    }
    r2 = requests.post(
        f"{SMARTLEAD_INTERNAL_API}/email-account/save-warmup",
        json=payload,
        headers=headers,
        timeout=15,
    )
    return r2.status_code == 200

def main():
    check_only = "--check" in sys.argv

    print("Fetching B group accounts...")
    accounts = fetch_b_group_accounts()
    print(f"Found {len(accounts)} B group accounts")

    by_client = {}
    for a in accounts:
        cid = a.get("client_id")
        by_client.setdefault(cid, []).append(a)

    for cid in sorted(B_GROUP_CLIENT_IDS):
        accs = by_client.get(cid, [])
        on = sum(1 for a in accs if a.get("warmup_enabled"))
        off = len(accs) - on
        print(f"  {CLIENT_NAMES[cid]}: {len(accs)} accounts, warmup ON: {on}, OFF: {off}")

    if check_only:
        return

    need_warmup = [a for a in accounts if not a.get("warmup_enabled")]
    if not need_warmup:
        print("\nAll accounts already have warmup enabled!")
        return

    print(f"\nEnabling warmup on {len(need_warmup)} accounts...")
    success, failed = 0, 0
    for i, acc in enumerate(need_warmup):
        aid = acc["id"]
        email = acc.get("from_email", "?")
        try:
            ok = enable_warmup_public(aid)
            if ok:
                configure_warmup_internal(aid)
                success += 1
            else:
                failed += 1
                print(f"  FAIL: {email}")
        except requests.exceptions.HTTPError as e:
            if "429" in str(e):
                print(f"  Rate limited at {i+1}/{len(need_warmup)}, waiting 60s...")
                time.sleep(60)
                try:
                    ok = enable_warmup_public(aid)
                    if ok:
                        configure_warmup_internal(aid)
                        success += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            else:
                failed += 1
                print(f"  ERROR: {email}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {email}: {e}")

        if (i + 1) % 20 == 0:
            print(f"  Progress: {i+1}/{len(need_warmup)} (ok: {success}, fail: {failed})")
            time.sleep(2)

    print(f"\nDone! Success: {success}, Failed: {failed}")
    print(f"Total with warmup: {success + sum(1 for a in accounts if a.get('warmup_enabled'))}/{len(accounts)}")

if __name__ == "__main__":
    main()
