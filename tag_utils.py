"""Tag parsing utilities for the THT infrastructure dashboard.

Group tag format:
  - Generic:     "Generic F", "Generic G2"
  - Client:      "Kay's Landscaping A", "Pioneer Landscaping B"
  - Acquisition: "Acquisition A", "Acquisition H"
"""

from __future__ import annotations

import re

ZAPMAIL_TAG_ID = 262254


def parse_group_tag(tag_name: str) -> dict:
    """Parse a group tag into its components.

    Returns dict with keys: role, client_name, group_letter, raw.
    - role: "generic", "client", or "acquisition"
    - client_name: None for generic/acquisition, client name for client groups
    - group_letter: "A", "B", "F", "G2", "H", etc.
    - raw: the original tag string
    """
    tag = tag_name.strip()

    if tag.lower().startswith("generic"):
        letter = tag[len("generic"):].strip()
        return {"role": "generic", "client_name": None, "group_letter": letter, "raw": tag}

    if tag.lower().startswith("acquisition"):
        letter = tag[len("acquisition"):].strip()
        return {"role": "acquisition", "client_name": None, "group_letter": letter, "raw": tag}

    # Client group: everything before the last " A" or " B" (single uppercase letter)
    m = re.match(r'^(.+)\s+([A-Z](?:\d+)?)$', tag)
    if m:
        return {"role": "client", "client_name": m.group(1), "group_letter": m.group(2), "raw": tag}

    # Fallback: no A/B suffix — treat as client with implied "A" group
    return {"role": "client", "client_name": tag, "group_letter": "A", "raw": tag}


def get_group_tag_from_account(account: dict) -> str | None:
    """Extract the group tag from a SmartLead account's tags array.

    The group tag is the one that is NOT "Zapmail" and NOT a date pattern (M/D/YY).
    """
    for t in account.get("tags", []):
        name = t.get("name", "")
        if name.lower() == "zapmail":
            continue
        if re.match(r'^\d{1,2}/\d{1,2}/\d{2}$', name):
            continue
        return name
    return None


def build_client_group_tag(client_name: str, ab: str) -> str:
    """Build a client group tag from client name and A/B designation."""
    return f"{client_name} {ab.upper()}"


def build_acquisition_tag(letter: str) -> str:
    """Build an acquisition group tag from a group letter."""
    return f"Acquisition {letter.upper()}"


def build_generic_tag(letter: str) -> str:
    """Build a generic group tag from a group letter."""
    return f"Generic {letter}"


def group_accounts_by_tag(accounts: list[dict]) -> dict[str, list[dict]]:
    """Group a list of SmartLead accounts by their group tag.

    Returns {group_tag_string: [account, ...]}.
    Accounts with no parseable group tag go under key "__untagged__".
    """
    groups = {}
    for acc in accounts:
        tag = get_group_tag_from_account(acc)
        key = tag or "__untagged__"
        groups.setdefault(key, []).append(acc)
    return groups
