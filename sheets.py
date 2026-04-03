"""
Google Sheets integration for THT Email Infrastructure.
Reads/writes the THT Domains master sheet and client tabs.

Sheet ID: 1oQZh0NnvJPtbgsBqQq-Fy6kbw_UG3Bo-zzdVeEtfMVc
Master tab: "THT Domains " (note trailing space)
Columns: Domains | Status | Provider | Notes | Purchase Date | Renewal date | Designation | Inbox
"""

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / "google_token.json"
CREDS_FILE = SCRIPT_DIR / "google_oauth_credentials.json"
SHEET_ID = "1oQZh0NnvJPtbgsBqQq-Fy6kbw_UG3Bo-zzdVeEtfMVc"
MASTER_TAB = "THT Domains "  # trailing space is intentional

# Domains with these substrings are NEVER used for client infrastructure
EXCLUDED_KEYWORDS = ["headlinetheory"]


def get_service():
    """Get authenticated Google Sheets service, refreshing token if needed."""
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
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
    rows = read_range(MASTER_TAB, "A1:H")
    if not rows:
        return [], []

    headers = rows[0]
    domains = []
    for i, row in enumerate(rows[1:], start=2):  # row numbers are 1-indexed, data starts row 2
        domains.append({
            "domain": row[0] if len(row) > 0 else "",
            "status": row[1] if len(row) > 1 else "",
            "provider": row[2] if len(row) > 2 else "",
            "notes": row[3] if len(row) > 3 else "",
            "purchase_date": row[4] if len(row) > 4 else "",
            "renewal_date": row[5] if len(row) > 5 else "",
            "designation": row[6] if len(row) > 6 else "",
            "inbox": row[7] if len(row) > 7 else "",
            "row_number": i  # actual row number in the sheet
        })
    return headers, domains


def get_available_domains(exclude_keywords=None):
    """Get all available generic domains (excludes headlinetheory, etc.)."""
    if exclude_keywords is None:
        exclude_keywords = EXCLUDED_KEYWORDS

    _, all_domains = get_all_master_domains()
    available = []
    for d in all_domains:
        if d["status"].lower().strip() != "available":
            continue
        # Exclude branded domains
        domain_lower = d["domain"].lower()
        if any(kw in domain_lower for kw in exclude_keywords):
            continue
        available.append(d)
    return available


def claim_domains(domains_to_claim, client_name):
    """Mark domains as 'In use' in the master sheet. Returns list of claimed domains.

    domains_to_claim: list of domain dicts with 'domain' and 'row_number' keys
    """
    service = get_service()
    today = datetime.now().strftime("%m/%d/%y")
    requests_batch = []

    for d in domains_to_claim:
        row = d["row_number"]
        # Update Status (col B) to "In use" and Notes (col D) to client name
        write_range(MASTER_TAB, f"B{row}", [["In use"]])
        write_range(MASTER_TAB, f"D{row}", [[f"{client_name}"]])

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
            "range": f"{MASTER_TAB}!D{row}",
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
    """Get a summary of domain counts by status and provider."""
    _, all_domains = get_all_master_domains()
    from collections import Counter

    statuses = Counter()
    providers = Counter()
    available_generic = 0

    for d in all_domains:
        statuses[d["status"]] += 1
        if d["provider"]:
            providers[d["provider"]] += 1
        if d["status"].lower().strip() == "available":
            if not any(kw in d["domain"].lower() for kw in EXCLUDED_KEYWORDS):
                available_generic += 1

    return {
        "total": len(all_domains),
        "by_status": dict(statuses),
        "by_provider": dict(providers),
        "available_for_clients": available_generic
    }
