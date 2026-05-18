#!/usr/bin/env python3
"""
Zapmail Profile Photo Automation
=================================
Sets profile photos on all mailboxes that don't have one.
Tracks completed uploads to avoid retrying already-done ones.
Reloads page between each to avoid stale DOM.
"""

import asyncio
import json
import sys
import os
from pathlib import Path
from playwright.async_api import async_playwright

SCRIPT_DIR = Path(__file__).parent
BROWSER_DATA = SCRIPT_DIR / ".zapmail_browser"
DEFAULT_PHOTO = SCRIPT_DIR / "headshots" / "sean_reynolds.png"
DONE_FILE = SCRIPT_DIR / ".photos_done.json"
ZAPMAIL_URL = "https://app.zapmail.ai"

def load_env():
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

load_env()
ZM_EMAIL = os.environ.get("ZAPMAIL_EMAIL", "")
ZM_PASSWORD = os.environ.get("ZAPMAIL_PASSWORD", "")


def load_done():
    if DONE_FILE.exists():
        return set(json.loads(DONE_FILE.read_text()))
    return set()

def save_done(done_set):
    DONE_FILE.write_text(json.dumps(list(done_set)))


async def auto_login(page):
    print("  Logging in...", flush=True)
    await page.locator('input[name="email"]').fill(ZM_EMAIL)
    await asyncio.sleep(0.5)
    await page.locator('input[name="password"]').fill(ZM_PASSWORD)
    await asyncio.sleep(0.5)
    await page.locator('button[type="submit"]').click()
    await page.wait_for_selector('nav, [class*="sidebar"]', timeout=30000)
    await asyncio.sleep(2)
    print("  Logged in!", flush=True)


async def go_to_mailboxes(page):
    await page.goto(f"{ZAPMAIL_URL}/mailboxes", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)
    if await page.query_selector('input[name="email"]'):
        await auto_login(page)
        await page.goto(f"{ZAPMAIL_URL}/mailboxes", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)


async def get_all_mailbox_info(page):
    """Get email and photo status for all visible mailbox rows."""
    return await page.evaluate("""() => {
        const rows = document.querySelectorAll('table tbody tr');
        const results = [];
        rows.forEach((row, i) => {
            const cells = row.querySelectorAll('td');
            if (cells.length < 3) return;
            const email = cells[2]?.textContent?.trim() || '';
            const avatar = row.querySelector('.rounded-full');
            const hasImg = avatar ? !!avatar.querySelector('img') : true;
            if (email && email.includes('@')) {
                results.push({index: i, email: email, hasPhoto: hasImg});
            }
        });
        return results;
    }""")


async def upload_photo_for_nth_see_more(page, n, photo_path):
    """Click nth See More → Edit Details → upload → Update Details."""
    see_more = page.locator("text=See More").nth(n)
    await see_more.click(timeout=5000)
    await asyncio.sleep(2)

    # Click Edit Details
    await page.locator('button:has-text("Edit Details")').click(timeout=10000)
    await asyncio.sleep(2)

    # Upload via hidden file input
    await page.locator('input[type="file"]').set_input_files(str(photo_path), timeout=5000)
    await asyncio.sleep(2)

    # Save
    await page.locator('button:has-text("Update Details")').click(timeout=10000)
    await asyncio.sleep(3)


async def run(domain_filter=None):
    photo = DEFAULT_PHOTO
    if not photo.exists():
        print(f"ERROR: Photo not found at {photo}")
        sys.exit(1)
    print(f"Using photo: {photo}", flush=True)

    done = load_done()
    print(f"Already uploaded: {len(done)} mailboxes", flush=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA),
            headless=True,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await go_to_mailboxes(page)

        total_done = 0
        total_skipped = 0
        consecutive_errors = 0

        while consecutive_errors < 5:
            # Get all mailbox info on current page
            mailboxes = await get_all_mailbox_info(page)

            # Find first mailbox that needs a photo and hasn't been done
            target = None
            for mb in mailboxes:
                email = mb["email"]
                # Apply domain filter if specified
                if domain_filter and domain_filter not in email:
                    continue
                if email in done:
                    total_skipped += 1
                    continue
                if not mb["hasPhoto"]:
                    target = mb
                    break

            if not target:
                print("\n  No more mailboxes need photos on this page!", flush=True)
                break

            email = target["email"]
            idx = target["index"]
            print(f"  [{total_done + 1}] {email} ...", end=" ", flush=True)

            try:
                await upload_photo_for_nth_see_more(page, idx, photo)
                print("OK!", flush=True)
                done.add(email)
                save_done(done)
                total_done += 1
                consecutive_errors = 0
            except Exception as e:
                err_msg = str(e).split('\n')[0][:80]
                print(f"FAIL: {err_msg}", flush=True)
                consecutive_errors += 1
                # Mark as done anyway to skip on next pass (photo may have uploaded)
                done.add(email)
                save_done(done)

            # Reload to get fresh DOM
            await go_to_mailboxes(page)

        print(f"\n  Complete! {total_done} new uploads, {len(done)} total done", flush=True)
        await asyncio.sleep(3)
        await context.close()


if __name__ == "__main__":
    domain_filter = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--domain" and i < len(sys.argv) - 1:
            domain_filter = sys.argv[i + 1]
    asyncio.run(run(domain_filter=domain_filter))
