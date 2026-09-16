"""
Vacant Home Utility Report Exporter
-----------------------------------
Rebuilds the "Vacant Home Utility Request" report from Zendesk:

  Form    : Vacant Home Utility Request
  Range   : a date window on ticket `created` (default: the previous full
            calendar month; overridable via START_DATE / END_DATE)
  Rows    : Ticket ID, Date, Property ID, Side Conversation, Status, Subject

For each matching ticket the script reads the Property ID custom field and
checks whether the ticket has any side conversations, then writes a styled
Excel workbook and emails it via Gmail SMTP.

Designed to run on GitHub Actions on a self-managed schedule (see
check_schedule.py / the workflows). All flags and credentials are read from
environment variables (GitHub Secrets + workflow_dispatch inputs) with a
fallback to credentials.ini for local runs.
"""

import os
import io
import sys
import time
import smtplib
import configparser
from pathlib import Path
from datetime import datetime, timezone, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

import requests
import pandas as pd

# ============================================================================
# CONFIG
# ============================================================================
CREDENTIALS_FILE = "credentials.ini"

# The Zendesk ticket form and the custom field that carries the Property ID.
# Adjust these if the form is renamed or the field id changes.
FORM_NAME = "Vacant Home Utility Request"
PROPERTY_ID_FIELD_ID = 5969744168091

# Throttle between per-ticket API calls to stay well under rate limits.
SLEEP_SECONDS = 0.25

REQUEST_TIMEOUT = 60

# Flags: read from env (set by workflow_dispatch inputs) or fall back to defaults
DRY_RUN  = os.environ.get("DRY_RUN",  "false").lower() == "true"
TEST_ONE = os.environ.get("TEST_ONE", "false").lower() == "true"

# OAuth scope requested for the client_credentials access token. "read" is
# Zendesk's global read scope and covers Search, Tickets, and Side
# Conversations. Must be within the OAuth client's allowed scopes (if any are
# configured on the client). Space-separated for multiple scopes.
ZENDESK_SCOPE = os.environ.get("ZENDESK_SCOPE", "read")

# Date range: overridable from Pocket Automation / workflow inputs.
# When either is blank, the previous full calendar month is used.
START_DATE_ENV = (os.environ.get("START_DATE") or "").strip()
END_DATE_ENV   = (os.environ.get("END_DATE") or "").strip()

# Columns in the output spreadsheet (order preserved).
REPORT_COLUMNS = [
    "Ticket ID",
    "Date",
    "Property ID",
    "Side Conversation",
    "Status",
    "Subject",
]

SCRIPT_DIR = Path(__file__).parent


# ============================================================================
# CREDENTIALS
# ============================================================================
def load_credentials():
    """Prefer environment variables (GitHub Secrets + workflow inputs).
    Fall back to credentials.ini in the script folder for local testing."""
    cfg = {
        "zendesk_subdomain":     os.environ.get("ZENDESK_SUBDOMAIN"),
        # OAuth client (client_credentials grant) — the headless default.
        "zendesk_client_id":     os.environ.get("ZENDESK_CLIENT_ID"),
        "zendesk_client_secret": os.environ.get("ZENDESK_CLIENT_SECRET"),
        # Optional: a pre-minted access token. If set, it's used as-is and the
        # client_credentials exchange is skipped.
        "zendesk_oauth_token":   os.environ.get("ZENDESK_OAUTH_TOKEN"),
        "gmail_email":        os.environ.get("GMAIL_EMAIL"),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD"),
        # RECIPIENT_OVERRIDE (from workflow_dispatch input) takes priority over secret
        "recipient_email":    (os.environ.get("RECIPIENT_OVERRIDE") or "").strip()
                              or os.environ.get("RECIPIENT_EMAIL"),
    }

    ini_path = SCRIPT_DIR / CREDENTIALS_FILE
    if ini_path.exists():
        parser = configparser.ConfigParser()
        parser.read(ini_path)
        cfg["zendesk_subdomain"]     = cfg["zendesk_subdomain"]     or parser.get("zendesk", "subdomain",     fallback=None)
        cfg["zendesk_client_id"]     = cfg["zendesk_client_id"]     or parser.get("zendesk", "client_id",     fallback=None)
        cfg["zendesk_client_secret"] = cfg["zendesk_client_secret"] or parser.get("zendesk", "client_secret", fallback=None)
        cfg["zendesk_oauth_token"]   = cfg["zendesk_oauth_token"]   or parser.get("zendesk", "oauth_token",   fallback=None)
        cfg["gmail_email"]        = cfg["gmail_email"]        or parser.get("gmail",   "email",        fallback=None)
        cfg["gmail_app_password"] = cfg["gmail_app_password"] or parser.get("gmail",   "app_password", fallback=None)
        cfg["recipient_email"]    = cfg["recipient_email"]    or parser.get("email",   "recipient",    fallback=None)

    # Zendesk access needs the subdomain plus EITHER a pre-minted access token
    # OR an OAuth client id/secret to exchange for one.
    if not cfg.get("zendesk_subdomain"):
        raise SystemExit(f"Missing ZENDESK_SUBDOMAIN (env var or {ini_path}).")
    if not cfg.get("zendesk_oauth_token") and not (
        cfg.get("zendesk_client_id") and cfg.get("zendesk_client_secret")
    ):
        raise SystemExit(
            "Missing Zendesk auth: set ZENDESK_CLIENT_ID + ZENDESK_CLIENT_SECRET "
            f"(or a ZENDESK_OAUTH_TOKEN) as env vars or in {ini_path}."
        )

    # For a dry run we only need Zendesk access; email creds may be absent.
    if not DRY_RUN:
        missing = [k for k in ("gmail_email", "gmail_app_password", "recipient_email")
                   if not cfg.get(k)]
        if missing:
            raise SystemExit(
                f"Missing email credentials: {missing}. "
                f"Set them as env vars (GitHub Secrets) or populate {ini_path}."
            )
    return cfg


def get_access_token(creds):
    """Return a Zendesk OAuth access token.

    Uses a pre-minted ZENDESK_OAUTH_TOKEN if provided; otherwise performs the
    headless client_credentials grant against /oauth/tokens using the OAuth
    client id/secret. The client's configured scopes must include read access.
    """
    if creds.get("zendesk_oauth_token"):
        return creds["zendesk_oauth_token"]

    base = f"https://{creds['zendesk_subdomain']}.zendesk.com"
    print("  Requesting access token via client_credentials grant...")
    resp = requests.post(
        f"{base}/oauth/tokens",
        json={
            "grant_type":    "client_credentials",
            "client_id":     creds["zendesk_client_id"],
            "client_secret": creds["zendesk_client_secret"],
            "scope":         ZENDESK_SCOPE,
        },
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 400:
        print("  ERROR BODY:", resp.text)
    resp.raise_for_status()

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise SystemExit(f"No access_token in OAuth response: {data}")
    return token


# ============================================================================
# DATE RANGE
# ============================================================================
def resolve_date_range():
    """Return (start_date, end_date) as 'YYYY-MM-DD' strings.

    The window is half-open: created >= start AND created < end. When both
    START_DATE and END_DATE are provided they are used verbatim; otherwise the
    previous full calendar month is computed (first day of last month up to,
    but not including, the first day of the current month).
    """
    if START_DATE_ENV and END_DATE_ENV:
        return START_DATE_ENV, END_DATE_ENV

    today = datetime.now(timezone.utc).date()
    first_of_this_month = today.replace(day=1)  # exclusive upper bound

    if first_of_this_month.month == 1:
        prev_year, prev_month = first_of_this_month.year - 1, 12
    else:
        prev_year, prev_month = first_of_this_month.year, first_of_this_month.month - 1
    first_of_prev_month = date(prev_year, prev_month, 1)

    return first_of_prev_month.isoformat(), first_of_this_month.isoformat()


def period_labels(start_date):
    """Return (month_label, 'Month YYYY') for the covered month.

    The covered month is the month of the (inclusive) start date, so a
    default Aug 1 -> Sep 1 window yields ('August', 'August 2026'). This is
    what names the output file and rolls over automatically each month.
    """
    try:
        d = datetime.strptime(start_date, "%Y-%m-%d")
        return d.strftime("%B"), d.strftime("%B %Y")
    except ValueError:
        return start_date, start_date


def format_date(created_at):
    """Format a Zendesk ISO timestamp as MM/DD/YYYY."""
    if not created_at:
        return ""
    try:
        return datetime.strptime(
            created_at, "%Y-%m-%dT%H:%M:%SZ"
        ).strftime("%m/%d/%Y")
    except ValueError:
        return created_at[:10]


# ============================================================================
# ZENDESK API
# ============================================================================
def make_session(creds):
    token = get_access_token(creds)
    session = requests.Session()
    # Zendesk OAuth: authenticate with a Bearer access token.
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    })
    return session


def zendesk_get(session, url, params=None):
    """GET with Zendesk rate-limit (429) handling."""
    while True:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "10"))
            print(f"  Rate limited. Waiting {wait}s...")
            time.sleep(wait)
            continue

        if response.status_code >= 400:
            print("  ERROR URL:", response.url)
            print("  ERROR BODY:", response.text)

        response.raise_for_status()
        return response.json()


def search_tickets(session, base_url, start_date, end_date):
    query = (
        f'type:ticket '
        f'form:"{FORM_NAME}" '
        f'created>={start_date} '
        f'created<{end_date}'
    )

    print(f"  Query: {query}")

    url = f"{base_url}/api/v2/search.json"
    params = {"query": query}

    while url:
        data = zendesk_get(session, url, params=params)

        for result in data.get("results", []):
            if result.get("result_type") == "ticket":
                yield result

        url = data.get("next_page")
        params = None
        time.sleep(SLEEP_SECONDS)


def get_ticket_details(session, base_url, ticket_id):
    url = f"{base_url}/api/v2/tickets/{ticket_id}.json"
    data = zendesk_get(session, url)
    return data.get("ticket", {})


def get_property_id_from_ticket(ticket):
    for field in ticket.get("custom_fields", []):
        if field.get("id") == PROPERTY_ID_FIELD_ID:
            return field.get("value", "") or ""
    return ""


def has_side_conversation(session, base_url, ticket_id):
    url = f"{base_url}/api/v2/tickets/{ticket_id}/side_conversations.json"
    data = zendesk_get(session, url)
    return "Yes" if data.get("side_conversations") else "No"


def collect_rows(session, base_url, start_date, end_date):
    rows = []
    count = 0

    for search_ticket in search_tickets(session, base_url, start_date, end_date):
        ticket_id = search_ticket["id"]
        count += 1

        print(f"  Checking Ticket {ticket_id}")

        full_ticket = get_ticket_details(session, base_url, ticket_id)
        property_id = get_property_id_from_ticket(full_ticket)
        side_convo  = has_side_conversation(session, base_url, ticket_id)
        ticket_date = format_date(search_ticket.get("created_at", ""))

        rows.append({
            "Ticket ID":         ticket_id,
            "Date":              ticket_date,
            "Property ID":       property_id,
            "Side Conversation": side_convo,
            "Status":            search_ticket.get("status", ""),
            "Subject":           search_ticket.get("subject", ""),
        })

        if TEST_ONE:
            print("  TEST_ONE set — stopping after the first ticket.")
            break

        time.sleep(SLEEP_SECONDS)

    return rows, count


# ============================================================================
# REPORT BUILDING
# ============================================================================
def build_excel(rows):
    df = pd.DataFrame(rows, columns=REPORT_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Vacant Home Utility")
        ws = writer.sheets["Vacant Home Utility"]

        # Auto-size columns (capped at 60 chars)
        for col in ws.columns:
            letter  = col[0].column_letter
            max_len = max((len(str(c.value or "")) for c in col), default=10)
            ws.column_dimensions[letter].width = min(max_len + 2, 60)

        # Freeze header row
        ws.freeze_panes = "A2"

    buf.seek(0)
    return buf, len(rows)


# ============================================================================
# EMAIL
# ============================================================================
def send_email(creds, attachment_buf, filename, row_count, start_date, end_date, period_label):
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    msg            = MIMEMultipart()
    msg["From"]    = creds["gmail_email"]
    msg["To"]      = creds["recipient_email"]
    msg["Subject"] = f"Vacant Home Utility Report - {period_label}"

    if TEST_ONE:
        msg["Subject"] += " [TEST ONE]"

    body = (
        f"Hi,\n\n"
        f"Attached is the Vacant Home Utility Request report for tickets "
        f"created from {start_date} (inclusive) to {end_date} (exclusive).\n\n"
        f"Ticket count:   {row_count}\n"
        f"Date range:     {start_date} -> {end_date}\n"
        f"Generated:      {today} (UTC)\n"
        f"Sent to:        {creds['recipient_email']}\n\n"
        f"This report is generated automatically on its configured schedule.\n"
        f"To run manually or with overrides, use the GitHub Actions "
        f"\"Vacant Home Utility Report\" workflow -> Run workflow.\n"
    )
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(attachment_buf.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=REQUEST_TIMEOUT) as server:
        server.ehlo()
        server.starttls()
        server.login(creds["gmail_email"], creds["gmail_app_password"])
        server.sendmail(creds["gmail_email"], creds["recipient_email"], msg.as_string())

    print(f"  Email sent to {creds['recipient_email']}")


# ============================================================================
# MAIN
# ============================================================================
def main():
    print("=== Vacant Home Utility Report Exporter ===")
    now = datetime.now(timezone.utc)
    print(f"Run date:  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"DRY_RUN={DRY_RUN}  TEST_ONE={TEST_ONE}")

    creds = load_credentials()
    start_date, end_date = resolve_date_range()
    print(f"Date range: {start_date} (>=) to {end_date} (<)\n")

    base_url = f"https://{creds['zendesk_subdomain']}.zendesk.com"
    session  = make_session(creds)

    print("[1/3] Fetching tickets from Zendesk...")
    rows, count = collect_rows(session, base_url, start_date, end_date)
    print(f"  Processed {count} tickets.")

    print("[2/3] Building Excel report...")
    excel_buf, row_count = build_excel(rows)
    month_label, period_label = period_labels(start_date)
    filename = f"{month_label}_Vacant_Home_Utility_Requests.xlsx"
    print(f"  Rows: {row_count}")
    print(f"  File: {filename}")

    if DRY_RUN:
        out = SCRIPT_DIR / filename
        out.write_bytes(excel_buf.getvalue())
        print(f"[3/3] DRY_RUN — saved locally to {out}. Email NOT sent.")
        return

    print("[3/3] Sending email...")
    send_email(creds, excel_buf, filename, row_count, start_date, end_date, period_label)

    print("\nDone. Report delivered.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        raise
