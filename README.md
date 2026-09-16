# Vacant Home Utility Report

Automated report of Zendesk tickets on the **"Vacant Home Utility Request"**
form. For each ticket in a date window it records the Property ID custom field
and whether the ticket has a side conversation, builds a styled Excel workbook,
and emails it via Gmail.

It runs on GitHub Actions on a self-managed schedule and can also be run
on-demand from the Actions **Run workflow** form.

## What it produces

An `.xlsx` with one row per ticket and these columns:

| Ticket ID | Date | Property ID | Side Conversation | Status | Subject |
|-----------|------|-------------|-------------------|--------|---------|

The date window is half-open (`created >= start` and `created < end`). On
scheduled runs it defaults to the **previous full calendar month**.

## Repository layout

| File | Purpose |
|------|---------|
| `report_exporter.py` | Fetches tickets, builds the Excel report, emails it. |
| `.github/workflows/vacant_home_utility_report.yml` | The report workflow + its **Run workflow** options form. |
| `.github/workflows/scheduler.yml` | Hourly cron that triggers the report when it's due. |
| `check_schedule.py` | Reads `schedule.json` and dispatches the report workflow. |
| `schedule.json` | Persisted schedule settings (frequency, hour, etc.). |
| `credentials.ini.example` | Template for local credentials (copy to `credentials.ini`). |
| `requirements.txt` | Python dependencies. |

## Setup (GitHub Actions)

Add these **repository Secrets** (Settings → Secrets and variables → Actions):

| Secret | Description |
|--------|-------------|
| `ZENDESK_SUBDOMAIN` | Your Zendesk subdomain (the part before `.zendesk.com`). |
| `ZENDESK_OAUTH_TOKEN` | Zendesk OAuth access token (sent as a `Bearer` token). |
| `GMAIL_EMAIL` | Gmail address that sends the report. |
| `GMAIL_APP_PASSWORD` | Gmail **app password** (not your normal password). |
| `RECIPIENT_EMAIL` | Default recipient of the report. |

## Options (Run workflow form)

Open **Actions → Vacant Home Utility Report → Run workflow** to run on demand
or change the schedule. Inputs:

- **frequency** — `manual`, `daily`, `weekly`, or `monthly`. Saved to
  `schedule.json`; the hourly scheduler fires the report when due.
- **day_of_week** — used only when frequency is `weekly`.
- **hour** — hour of day (UTC) to run.
- **save_only** — save the schedule without running the report now.
- **internal** — set automatically by the scheduler; leave unchecked.
- **dry_run** — build the report and upload it as a workflow artifact **without
  sending email**.
- **test_one** — only process the first matching ticket (quick smoke test).
- **recipient_email** — override the recipient for this run (blank = secret).
- **start_date** / **end_date** — override the date window (`YYYY-MM-DD`).
  Leave both blank to use the previous full calendar month.

The hourly `scheduler.yml` reads `schedule.json`, and when the report is due it
dispatches the report workflow with `start_date`/`end_date` blank (so the
previous full month is used). `last_triggered_date` prevents double-firing
within the same period.

## Running locally

```bash
pip install -r requirements.txt
cp credentials.ini.example credentials.ini   # then fill in real values
DRY_RUN=true TEST_ONE=true python report_exporter.py
```

`DRY_RUN=true` writes the `.xlsx` to the current folder and skips email.
`credentials.ini` is gitignored — never commit real credentials. Environment
variables (the Secrets above, plus `START_DATE`/`END_DATE`/`DRY_RUN`/
`TEST_ONE`/`RECIPIENT_OVERRIDE`) take priority over `credentials.ini`.

## Adjusting the form / field

If the Zendesk form is renamed or the Property ID field id changes, update
these constants at the top of `report_exporter.py`:

```python
FORM_NAME = "Vacant Home Utility Request"
PROPERTY_ID_FIELD_ID = 5969744168091
```
