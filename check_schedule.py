#!/usr/bin/env python3
"""
check_schedule.py
-----------------
Read schedule.json and trigger vacant_home_utility_report.yml if it's due.
Called by the hourly scheduler workflow.

GitHub Actions cron is best-effort — a scheduled run can be delayed or
silently skipped under load. To survive a missed hour, this script:
  1. Fires if now.hour >= scheduled_hour (not an exact match) — catches
     late-firing scheduler runs within the same day.
  2. Tracks last_triggered_date in schedule.json so it never double-fires
     once it has already run for the current period.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

SCHEDULE_FILE = "schedule.json"
WORKFLOW_FILE = "vacant_home_utility_report.yml"

# Day of the month that the monthly report fires on (UTC). Running on the 2nd
# (rather than the 1st) gives the previous month a full day to settle before
# the report covers it.
MONTHLY_RUN_DAY = 2


def save_schedule(schedule: dict) -> None:
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, indent=2)


def already_ran_today(token: str, repo: str, today_key: str) -> bool:
    """Ask GitHub whether the report workflow already ran today (UTC).

    Defense-in-depth against a lost last_triggered_date marker: GitHub's run
    history is the source of truth for 'did the report fire today', so even if
    the committed marker gets dropped by a concurrent write we still won't
    double-fire. Fails open (returns False) on any API error so a transient
    GitHub hiccup can't silently suppress a legitimately-due report.
    """
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{WORKFLOW_FILE}/runs?per_page=20")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
        print(f"Could not check prior runs (continuing): {e}")
        return False

    for run in data.get("workflow_runs", []):
        # created_at is ISO-8601 UTC, e.g. 2026-07-03T08:04:11Z
        if (run.get("created_at", "")[:10] == today_key
                and run.get("event") == "workflow_dispatch"):
            print(f"Report workflow already ran today ({today_key}) "
                  f"per GitHub run history — skipping.")
            return True
    return False


def main():
    if not os.path.exists(SCHEDULE_FILE):
        print("No schedule.json found — nothing scheduled.")
        return

    with open(SCHEDULE_FILE) as f:
        schedule = json.load(f)

    frequency = schedule.get("frequency", "manual")
    if frequency == "manual":
        print("Frequency is 'manual' — auto-scheduling disabled.")
        return

    now            = datetime.now(timezone.utc)
    scheduled_hour = int(schedule.get("hour", 8))
    day_names      = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    today_key      = now.strftime("%Y-%m-%d")

    print(f"Now (UTC): {now.strftime('%A %Y-%m-%d %H:%M')}")
    print(f"Schedule : {frequency} at {scheduled_hour:02d}:00 UTC"
          + (f" on {schedule.get('day_of_week', 'Friday')}" if frequency == "weekly" else ""))

    # Already triggered for this period — don't double-fire even if checked
    # again later the same day (e.g. multiple scheduler runs after 8am).
    if schedule.get("last_triggered_date") == today_key:
        print(f"Already triggered today ({today_key}) — skipping.")
        return

    # Catch late-firing scheduler runs: fire any time from the scheduled
    # hour onward (not just an exact match), so a missed 8am slot still
    # catches up at 9am, 10am, etc. the same day.
    if now.hour < scheduled_hour:
        print(f"Too early — {now.hour}:00 < scheduled {scheduled_hour}:00. Skipping.")
        return

    if frequency == "weekly":
        today = day_names[now.weekday()]
        if today != schedule.get("day_of_week", "Friday"):
            print(f"Day mismatch ({today} != {schedule['day_of_week']}) — skipping.")
            return

    elif frequency == "monthly":
        if now.day != MONTHLY_RUN_DAY:
            print(f"Not day {MONTHLY_RUN_DAY} of the month (day={now.day}) — skipping.")
            return

    token = os.environ.get("GH_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPOSITORY", "")

    if not token or not repo:
        print("ERROR: GH_TOKEN or GITHUB_REPOSITORY not set.", file=sys.stderr)
        sys.exit(1)

    # Belt-and-suspenders dedup: even if last_triggered_date was lost to a
    # concurrent write, don't fire again if GitHub already shows a report run
    # today.
    if already_ran_today(token, repo, today_key):
        # Re-persist the marker we apparently lost, so the cheap file check
        # short-circuits future runs this period without another API call.
        schedule["last_triggered_date"] = today_key
        save_schedule(schedule)
        return

    print("✓ Schedule matched — triggering Vacant Home Utility Report...")

    # Scheduled runs leave start_date/end_date blank so the exporter uses the
    # previous full calendar month. Manual overrides live only in a manual
    # dispatch, not here.
    inputs = {
        "frequency":       frequency,
        "day_of_week":     schedule.get("day_of_week", "Friday"),
        "hour":            str(scheduled_hour),
        "save_only":       "false",
        "internal":        "true",
        "dry_run":         str(schedule.get("dry_run", "false")).lower(),
        "test_one":        "false",
        "recipient_email": "",
        "start_date":      schedule.get("start_date", ""),
        "end_date":        schedule.get("end_date", ""),
    }

    payload = json.dumps({"ref": "main", "inputs": inputs}).encode()
    url     = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"

    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
            "Content-Type":  "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Triggered successfully (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"Trigger failed: HTTP {e.code} — {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    # Mark as triggered for today so we don't fire again this period
    schedule["last_triggered_date"] = today_key
    save_schedule(schedule)


if __name__ == "__main__":
    main()
