"""
Google Sheets integration for THT Email Infrastructure.
Reads/writes the THT Domains master sheet and client tabs.

Sheet ID: 1oQZh0NnvJPtbgsBqQq-Fy6kbw_UG3Bo-zzdVeEtfMVc
Master tab: "THT Domains " (note trailing space)
Columns: Domain | Status | Provider | Client | Pool
"""

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from pathlib import Path
from datetime import datetime
import json
import os

SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / "google_token.json"
CREDS_FILE = SCRIPT_DIR / "google_oauth_credentials.json"
SHEET_ID = "1oQZh0NnvJPtbgsBqQq-Fy6kbw_UG3Bo-zzdVeEtfMVc"
MASTER_TAB = "THT Domains "  # trailing space is intentional

# Domains with these substrings are NEVER used for client infrastructure
EXCLUDED_KEYWORDS = ["headlinetheory"]


def get_service():
    """Get authenticated Google Sheets service, refreshing token if needed."""
    token_env = os.environ.get("GOOGLE_TOKEN_JSON", "")
    if token_env:
        creds = Credentials.from_authorized_user_info(json.loads(token_env))
    else:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not token_env:
            TOKEN_FILE.write_text(creds.to_json())
    return build("sheets", "v4", credentials=creds)


def read_range(tab, range_str="A1:Z"):
    """Read a range from the sheet."""
    service = get_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{tab}!{range_str}"
    ).execute()
    return result.get("values", [])


def write_range(tab, range_str, values):
    """Write values to a range."""
    service = get_service()
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{tab}!{range_str}",
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()


def append_rows(tab, values):
    """Append rows to a tab."""
    service = get_service()
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values}
    ).execute()


def get_sheet_tabs():
    """Get all tab names in the sheet."""
    service = get_service()
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def create_tab(tab_name):
    """Create a new tab if it doesn't exist."""
    existing = get_sheet_tabs()
    if tab_name in existing:
        return False  # already exists
    service = get_service()
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
    ).execute()
    return True


# ─── DOMAIN OPERATIONS ───

def get_all_master_domains():
    """Read all domains from the master THT Domains tab."""
    rows = read_range(MASTER_TAB, "A1:E")
    if not rows:
        return [], []

    headers = rows[0]
    domains = []
    for i, row in enumerate(rows[1:], start=2):  # row numbers are 1-indexed, data starts row 2
        domains.append({
            "domain": row[0] if len(row) > 0 else "",
            "status": row[1] if len(row) > 1 else "",
            "provider": row[2] if len(row) > 2 else "",
            "client": row[3] if len(row) > 3 else "",
            "pool": row[4] if len(row) > 4 else "",
            "row_number": i  # actual row number in the sheet
        })
    return headers, domains


def get_available_domains(exclude_keywords=None):
    """Get all available client-pool domains."""
    if exclude_keywords is None:
        exclude_keywords = EXCLUDED_KEYWORDS

    _, all_domains = get_all_master_domains()
    available = []
    for d in all_domains:
        if d["status"].lower().strip() != "available":
            continue
        # Use Pool column if set, otherwise fall back to keyword check
        if d.get("pool", "").lower() == "acquisition":
            continue
        domain_lower = d["domain"].lower()
        if any(kw in domain_lower for kw in exclude_keywords):
            continue
        available.append(d)
    return available


def get_acquisition_domains(exclude_keywords=None):
    """Get available domains in the acquisition pool."""
    if exclude_keywords is None:
        exclude_keywords = EXCLUDED_KEYWORDS
    _, domains = get_all_master_domains()
    available = []
    for d in domains:
        if d["status"].lower().strip() != "available":
            continue
        # Use Pool column if set, otherwise fall back to keyword check
        if d.get("pool", "").lower() == "acquisition":
            available.append(d)
        elif any(kw in d["domain"].lower() for kw in exclude_keywords):
            available.append(d)
    return available


def claim_domains(domains_to_claim, client_name):
    """Mark domains as 'In use' in the master sheet. Returns list of claimed domains.

    domains_to_claim: list of domain dicts with 'domain' and 'row_number' keys
    """
    for d in domains_to_claim:
        row = d["row_number"]
        # Update Status (col B) to "In use" and Client (col D) to client name
        write_range(MASTER_TAB, f"B{row}", [["In use"]])
        write_range(MASTER_TAB, f"D{row}", [[client_name]])

    return domains_to_claim


def mark_domains_in_use_batch(domains_with_rows, client_name):
    """Efficiently mark multiple domains as In use in one batch."""
    service = get_service()
    batch_data = []
    for d in domains_with_rows:
        row = d["row_number"]
        batch_data.append({
            "range": f"{MASTER_TAB}!B{row}",
            "values": [["In use"]]
        })
        batch_data.append({
            "range": f"{MASTER_TAB}!D{row}",  # Client column
            "values": [[client_name]]
        })

    if batch_data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": batch_data
            }
        ).execute()


# ─── CLIENT TAB OPERATIONS ───

def setup_client_tab(client_name, domains, setup_date=None):
    """Create/update a client tab with their domains.

    Format matches existing tabs:
      Row 0: "Zapmail Google {setup_date}"
      Row 1+: domain names
    """
    if setup_date is None:
        setup_date = datetime.now().strftime("%-m/%-d/%y")

    tab_name = client_name
    created = create_tab(tab_name)

    # Build rows: header + domain list
    header = f"Zapmail Google {setup_date}"
    rows = [[header]]
    for d in domains:
        domain_name = d["domain"] if isinstance(d, dict) else d
        rows.append([domain_name])

    if created:
        # New tab — write header + domains from A1
        write_range(tab_name, "A1", rows)
    else:
        # Existing tab — append a blank row then header + domains
        append_rows(tab_name, [[""]] + rows)
    return tab_name


def add_domains_to_existing_client_tab(client_name, domains):
    """Append domains to an existing client tab."""
    rows = [[d["domain"] if isinstance(d, dict) else d] for d in domains]
    append_rows(client_name, rows)


# ─── REPORTING ───

def get_domain_summary():
    """Get a summary of domain counts by status, provider, and pool."""
    _, all_domains = get_all_master_domains()
    from collections import Counter

    statuses = Counter()
    providers = Counter()
    pools = Counter()
    available_client = 0
    available_acquisition = 0

    for d in all_domains:
        statuses[d["status"]] += 1
        if d["provider"]:
            providers[d["provider"]] += 1
        pool = d.get("pool", "Client")
        pools[pool] += 1
        if d["status"].lower().strip() == "available":
            if pool == "Acquisition" or any(kw in d["domain"].lower() for kw in EXCLUDED_KEYWORDS):
                available_acquisition += 1
            else:
                available_client += 1

    return {
        "total": len(all_domains),
        "by_status": dict(statuses),
        "by_provider": dict(providers),
        "by_pool": dict(pools),
        "available_for_clients": available_client,
        "available_for_acquisition": available_acquisition,
    }
