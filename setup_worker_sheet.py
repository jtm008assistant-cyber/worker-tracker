"""One-shot: create TWO Google Sheets in the Shared Drive:
  1. Worker Tracker — Roster / Activity Log / Daily Summary / Worker Profile
  2. Payroll — Payroll tab (separate sheet so it can be shared independently)

Prints both sheet IDs to paste into .env as WORKER_TRACKER_SHEET_ID and PAYROLL_SHEET_ID.

Usage:
    python setup_worker_sheet.py [--email you@gmail.com]

If --email is passed, both sheets are shared with that user as Editor. Both
sheets are also created with anyone-with-link = Editor so the service
account + manager can read/write.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from worker_tracker import config as wt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("setup_worker_sheet")


def _col_letter(idx_1based: int) -> str:
    """1 -> A, 27 -> AA. Handles up to ZZ. Good enough for our headers."""
    s = ""
    n = idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


def _create_sheet(drive, gc, title: str, folder_id: str, share_with: str | None) -> tuple[str, gspread.Spreadsheet]:
    log.info("Creating Sheet '%s' in folder %s", title, folder_id)
    created = drive.files().create(
        body={
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [folder_id],
        },
        fields="id",
        supportsAllDrives=True,
    ).execute()
    sheet_id = created["id"]

    drive.permissions().create(
        fileId=sheet_id,
        body={"role": "writer", "type": "anyone"},
        fields="id",
        supportsAllDrives=True,
    ).execute()
    if share_with:
        drive.permissions().create(
            fileId=sheet_id,
            body={"role": "writer", "type": "user", "emailAddress": share_with},
            sendNotificationEmail=False,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        log.info("Shared %s with %s", title, share_with)

    return sheet_id, gc.open_by_key(sheet_id)


def _format_header(ws, header):
    last = _col_letter(len(header))
    ws.format(
        f"A1:{last}1",
        {
            "textFormat": {
                "bold": True,
                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            },
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
        },
    )
    ws.freeze(rows=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="Share both sheets with this user as Editor")
    ap.add_argument("--tracker-title", default="Worker Tracker")
    ap.add_argument("--payroll-title", default="Worker Tracker — Payroll")
    args = ap.parse_args()

    folder_id = wt.SHEETS_FOLDER_ID or os.environ.get("DRIVE_ROOT_FOLDER_ID") or wt.SHARED_DRIVE_ID
    if not folder_id:
        sys.exit("Need SHEETS_FOLDER_ID, DRIVE_ROOT_FOLDER_ID, or SHARED_DRIVE_ID in .env")

    creds = Credentials.from_service_account_file(wt.SERVICE_ACCOUNT_JSON, scopes=wt.SCOPES)
    gc = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    # ---------------- TRACKER SHEET ----------------
    tracker_id, ss = _create_sheet(drive, gc, args.tracker_title, folder_id, args.email)

    # Roster
    roster = ss.sheet1
    roster.update_title(wt.ROSTER_TAB)
    roster.resize(rows=50, cols=len(wt.ROSTER_HEADER))
    sample = [
        "Alice Example", "U01ABCDEF", "alice@example.com",
        "America/New_York", "09:00", "17:00", "TRUE",
        "hourly",   # Pay Type: hourly | salaried
        "25",       # Hourly Rate (for hourly workers; ignored if salaried)
        "",         # Salary (per period) — for salaried workers, the fixed amount they get each pay period
        "USD",
        "40", "1.5",
        "",         # Check-in Frequency: blank = use global default (120 min)
    ]
    roster.update(values=[wt.ROSTER_HEADER, sample], range_name="A1", value_input_option="USER_ENTERED")
    _format_header(roster, wt.ROSTER_HEADER)

    # Activity Log
    activity = ss.add_worksheet(title=wt.ACTIVITY_TAB, rows=2000, cols=len(wt.ACTIVITY_HEADER))
    activity.update(values=[wt.ACTIVITY_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    _format_header(activity, wt.ACTIVITY_HEADER)

    # Daily Summary
    summary = ss.add_worksheet(title=wt.SUMMARY_TAB, rows=500, cols=len(wt.SUMMARY_HEADER))
    summary.update(values=[wt.SUMMARY_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    _format_header(summary, wt.SUMMARY_HEADER)

    # Worker Profile
    profile = ss.add_worksheet(title=wt.PROFILE_TAB, rows=50, cols=len(wt.PROFILE_HEADER))
    profile.update(values=[wt.PROFILE_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    _format_header(profile, wt.PROFILE_HEADER)

    # Processes & Tools (knowledge base)
    knowledge = ss.add_worksheet(title=wt.KNOWLEDGE_TAB, rows=500, cols=len(wt.KNOWLEDGE_HEADER))
    knowledge.update(values=[wt.KNOWLEDGE_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    _format_header(knowledge, wt.KNOWLEDGE_HEADER)

    # ---------------- PAYROLL SHEET (separate) ----------------
    payroll_id, ps = _create_sheet(drive, gc, args.payroll_title, folder_id, args.email)
    payroll_ws = ps.sheet1
    payroll_ws.update_title(wt.PAYROLL_TAB)
    payroll_ws.resize(rows=1000, cols=len(wt.PAYROLL_HEADER))
    payroll_ws.update(values=[wt.PAYROLL_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    _format_header(payroll_ws, wt.PAYROLL_HEADER)

    # Timesheet tab — per-day breakdown for bookkeeper
    timesheet_ws = ps.add_worksheet(title=wt.TIMESHEET_TAB, rows=5000, cols=len(wt.TIMESHEET_HEADER))
    timesheet_ws.update(values=[wt.TIMESHEET_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    _format_header(timesheet_ws, wt.TIMESHEET_HEADER)

    # Print summary
    print()
    print("=" * 78)
    print("  Sheets created.")
    print()
    print(f"  Tracker:  https://docs.google.com/spreadsheets/d/{tracker_id}")
    print(f"  Payroll:  https://docs.google.com/spreadsheets/d/{payroll_id}")
    print()
    print("  Add BOTH of these lines to C:\\Ace\\.env :")
    print(f"      WORKER_TRACKER_SHEET_ID={tracker_id}")
    print(f"      PAYROLL_SHEET_ID={payroll_id}")
    print()
    print("  Next steps:")
    print("    1. Open the Tracker sheet -> Roster tab -> replace the Alice Example")
    print("       row with your real workers (one per row). Fill Hourly Rate and")
    print("       leave Active=TRUE. Set Pay Type to 'hourly' or 'salaried'.")
    print("    2. Follow worker_tracker/SETUP.md to create the Slack app and")
    print("       Gmail app password.")
    print("    3. Run: python -m worker_tracker bot")
    print("=" * 78)


if __name__ == "__main__":
    main()
