#!/usr/bin/env python3
"""Daily campaign-tag audit — ensures every active client campaign has all
tagged accounts assigned. Posts results via Marsha to #marsha Slack channel.

Usage:
  python3 daily_audit.py              # Report only (no changes)
  python3 daily_audit.py --fix        # Auto-fix: add missing accounts

Set up as a daily cron/launchd job to catch drift early.
"""

import logging
import sys
from pathlib import Path

# Ensure we can import sibling modules
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import marsha

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

if __name__ == "__main__":
    fix = "--fix" in sys.argv
    result = marsha.run_daily_audit(fix=fix)

    if result["issues"]:
        print(f"Found {len(result['issues'])} campaigns with missing accounts")
        for i in result["issues"]:
            print(f"  {i['campaign']}: {i['on_campaign']}/{i['tagged_total']} accounts ({i['volume_before']}/day)")
        if fix:
            print(f"Applied {len(result['fixes'])} fixes")
    else:
        print("All campaigns fully staffed")
