#!/usr/bin/env python3
"""Fail if a migration creates a public table without granting it to the Data API.

Supabase stopped auto-granting newly created public-schema tables on 2026-10-30.
A table created without an explicit GRANT still exists and still accepts SQL, but
every PostgREST call against it returns 42501 "permission denied for table <name>"
— and because most callers here swallow errors, that reads as "the feature
silently does nothing" rather than as an error.

Usage:
    python3 scripts/check_migration_grants.py                 # check, exit 1 on failure
    python3 scripts/check_migration_grants.py --list          # show every migration's verdict

Migrations older than CUTOFF are skipped: they were applied while Supabase still
granted automatically, so their tables already hold the privileges, and the
backfill in the data_api_grants migration covers a replay from scratch.

Stdlib only, on purpose — this has to run in CI, in a hook, and on a laptop with
no venv.
"""
import argparse
import os
import re
import sys

# Anything at or after this stamp is a migration written under the new rules.
CUTOFF = "20260923"

# ── Lexing ───────────────────────────────────────────────────────────────
# Comments and string literals have to go before we look for keywords, or seed
# data poisons the result: an early version of this check passed a migration
# that has no GRANT at all because its INSERT listed the Kentucky county
# "Grant".

def strip_comments(sql: str) -> str:
    out, i, n = [], 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif sql[i] == "'":
            # Copy the literal through verbatim. We only skip it so that a --
            # or /* inside a string is not mistaken for a comment; the contents
            # still matter, because grant_data_api('t') names its table there.
            start = i
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                elif sql[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
            out.append(sql[start:i])
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def strip_dollar_quoted(sql: str) -> str:
    """Blank out $$ ... $$ bodies, keeping the delimiters so statements still split."""
    out, i = [], 0
    while True:
        m = DOLLAR.search(sql, i)
        if not m:
            out.append(sql[i:])
            break
        tag = m.group(0)
        end = sql.find(tag, m.end())
        if end == -1:
            out.append(sql[i:])
            break
        out.append(sql[i:m.end()])
        i = end
    return "".join(out)


def strip_strings(sql: str) -> str:
    return re.sub(r"'[^']*'", "''", sql)


# ── Statement detection ──────────────────────────────────────────────────
IDENT = r'"?([a-zA-Z_][a-zA-Z0-9_]*)"?'
CREATE_TABLE = re.compile(
    r"create\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?(?:\"?public\"?\s*\.\s*)?" + IDENT,
    re.I)
CREATE_VIEW = re.compile(
    r"create\s+(?:or\s+replace\s+)?(?:materialized\s+)?view\s+(?:if\s+not\s+exists\s+)?"
    r"(?:\"?public\"?\s*\.\s*)?" + IDENT, re.I)
# Anything created in a schema other than public is out of scope.
OTHER_SCHEMA = re.compile(
    r"create\s+(?:or\s+replace\s+)?(?:unlogged\s+|materialized\s+)?(?:table|view)\s+"
    r"(?:if\s+not\s+exists\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?\s*\.", re.I)
GRANT_ON = re.compile(
    r"grant\s+[a-z ,()]*?\s+on\s+(?:table\s+)?(.*?)\s+to\s", re.I | re.S)
HELPER = re.compile(r"grant_data_api\s*\(\s*'([a-zA-Z_][a-zA-Z0-9_]*)'", re.I)


def tables_in_grant_target(blob: str):
    for part in blob.split(","):
        part = part.strip().strip('"')
        if not part:
            continue
        part = part.split(".")[-1].strip().strip('"')
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", part):
            yield part


def audit(path: str):
    raw = open(path, encoding="utf-8", errors="replace").read()
    nocomment = strip_comments(raw)
    helper_grants = {m.group(1) for m in HELPER.finditer(nocomment)}
    body = strip_strings(strip_dollar_quoted(nocomment))

    non_public = {m.group(1).lower() for m in OTHER_SCHEMA.finditer(body)} - {"public"}
    created = set()
    for rx in (CREATE_TABLE, CREATE_VIEW):
        for m in rx.finditer(body):
            name = m.group(1)
            # "create table other_schema.foo" matches IDENT as the schema; drop it.
            if name.lower() in non_public:
                continue
            created.add(name)

    granted = set(helper_grants)
    for m in GRANT_ON.finditer(body):
        target = m.group(1)
        if re.search(r"\b(schema|sequence|function|routine|all\s+tables)\b", target, re.I):
            continue
        granted.update(tables_in_grant_target(target))

    return created, granted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrations", default=None, help="migrations directory")
    ap.add_argument("--since", default=CUTOFF, help="only check migrations named >= this")
    ap.add_argument("--list", action="store_true", help="print every migration, not just failures")
    args = ap.parse_args()

    mig = args.migrations or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supabase", "migrations")
    if not os.path.isdir(mig):
        print(f"check_migration_grants: no migrations directory at {mig}", file=sys.stderr)
        return 2

    failures = []
    checked = 0
    for fn in sorted(os.listdir(mig)):
        if not fn.endswith(".sql"):
            continue
        stamp = re.match(r"(\d+)", fn)
        if args.since and stamp and stamp.group(1) < args.since:
            continue
        checked += 1
        created, granted = audit(os.path.join(mig, fn))
        missing = sorted(created - granted)
        if missing:
            failures.append((fn, missing))
            print(f"FAIL {fn}")
            for t in missing:
                print(f"       public.{t} is created but never granted")
        elif args.list:
            print(f"ok   {fn}" + (f"  ({len(created)} granted)" if created else ""))

    print()
    if failures:
        print(f"{len(failures)} of {checked} migration(s) create a public table with no Data API grant.")
        print()
        print("Add to the same migration, right after the CREATE TABLE:")
        print("    select public.grant_data_api('your_table');")
        print("or spell it out:")
        print("    grant select, insert, update, delete on public.your_table")
        print("      to anon, authenticated, service_role;")
        print()
        print("Supabase stopped granting new public tables automatically on 2026-10-30;")
        print("without this the table 42501s on every Data API call.")
        return 1

    print(f"{checked} migration(s) checked since {args.since}: every new public table is granted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
