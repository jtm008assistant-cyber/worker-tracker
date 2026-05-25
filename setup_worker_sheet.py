"""One-shot: create the 'Worker Tracker' Sheet in the Shared Drive with the
Roster / Activity Log / Daily Summary tabs. Prints the sheet ID to paste into
.env as WORKER_TRACKER_SHEET_ID.

Usage:
    python setup_worker_sheet.py [--email you@gmail.com]

If --email is passed, the sheet is also shared with that user as Editor. The
sheet is always created with anyone-with-link = Editor so the manager and
service account can both read/write.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from worker_tracker import config as wt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("setup_worker_sheet")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", help="Share sheet with this user as Editor")
    ap.add_argument("--title", default="Worker Tracker")
    args = ap.parse_args()

    folder_id = wt.SHEETS_FOLDER_ID or os.environ.get("DRIVE_ROOT_FOLDER_ID") or wt.SHARED_DRIVE_ID
    if not folder_id:
        sys.exit("Need SHEETS_FOLDER_ID, DRIVE_ROOT_FOLDER_ID, or SHARED_DRIVE_ID in .env")

    creds = Credentials.from_service_account_file(wt.SERVICE_ACCOUNT_JSON, scopes=wt.SCOPES)
    gc = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    log.info("Creating Sheet '%s' in folder %s", args.title, folder_id)
    created = drive.files().create(
        body={
            "name": args.title,
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
    if args.email:
        drive.permissions().create(
            fileId=sheet_id,
            body={"role": "writer", "type": "user", "emailAddress": args.email},
            sendNotificationEmail=False,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        log.info("Shared with %s", args.email)

    ss = gc.open_by_key(sheet_id)

    roster = ss.sheet1
    roster.update_title(wt.ROSTER_TAB)
    roster.resize(rows=50, cols=len(wt.ROSTER_HEADER))
    sample = ["Alice Example", "U01ABCDEF", "alice@example.com", "America/New_York", "09:00", "17:00", "TRUE"]
    roster.update(values=[wt.ROSTER_HEADER, sample], range_name="A1", value_input_option="USER_ENTERED")
    roster.format("A1:G1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}, "horizontalAlignment": "LEFT"})
    roster.format("A1:G1", {"textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}})
    roster.freeze(rows=1)

    activity = ss.add_worksheet(title=wt.ACTIVITY_TAB, rows=2000, cols=len(wt.ACTIVITY_HEADER))
    activity.update(values=[wt.ACTIVITY_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    activity.format("A1:G1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}})
    activity.freeze(rows=1)

    summary = ss.add_worksheet(title=wt.SUMMARY_TAB, rows=500, cols=len(wt.SUMMARY_HEADER))
    summary.update(values=[wt.SUMMARY_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    last_col = chr(ord("A") + len(wt.SUMMARY_HEADER) - 1)
    summary.format(f"A1:{last_col}1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}})
    summary.freeze(rows=1)

    profile = ss.add_worksheet(title=wt.PROFILE_TAB, rows=50, cols=len(wt.PROFILE_HEADER))
    profile.update(values=[wt.PROFILE_HEADER], range_name="A1", value_input_option="USER_ENTERED")
    last_col_p = chr(ord("A") + len(wt.PROFILE_HEADER) - 1)
    profile.format(f"A1:{last_col_p}1", {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}, "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}})
    profile.freeze(rows=1)

    log.info("Created sheet: https://docs.google.com/spreadsheets/d/%s", sheet_id)
    print()
    print("=" * 70)
    print("  Worker Tracker sheet created.")
    print(f"  URL: https://docs.google.com/spreadsheets/d/{sheet_id}")
    print()
    print("  Add this line to C:\\Ace\\.env :")
    print(f"      WORKER_TRACKER_SHEET_ID={sheet_id}")
    print()
    print("  Next: fill the Roster tab with your workers, then see worker_tracker/SETUP.md")
    print("=" * 70)


if __name__ == "__main__":
    main()
