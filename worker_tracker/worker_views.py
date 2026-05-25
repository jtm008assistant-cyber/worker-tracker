"""Per-worker personal view sheets.

Each worker gets one Google Sheet that auto-shows ONLY their own data,
pulled via QUERY+IMPORTRANGE from the central tracker + payroll sheets.

Workers get view-only access to their personal sheet, so they can audit
their hours and pay without touching the central admin data. They can
never see other workers' rows because the QUERY filter pins to their name.

Workflow:
  - python -m worker_tracker create_views — bulk-create for every active worker
    who doesn't have one yet (writes the URL back to their Roster row)
  - Or, the bot auto-creates one the first time an unprovisioned worker logs in

First-time gotcha: when the worker first opens their sheet, Google shows
a yellow banner saying "Allow access?" — they click Allow once per source
sheet and the data appears.
"""
from __future__ import annotations

import logging
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from . import config, sheets

log = logging.getLogger(__name__)


def _drive_creds():
    return Credentials.from_service_account_file(config.SERVICE_ACCOUNT_JSON, scopes=config.SCOPES)


def _drive():
    return build("drive", "v3", credentials=_drive_creds(), cache_discovery=False)


def _gc():
    return gspread.authorize(_drive_creds())


def _query_formula(source_sheet_id: str, sheet_range: str, select_cols: str,
                   filter_col: str, worker_name: str, order_by: str = "Col1 desc") -> str:
    """Build a =QUERY(IMPORTRANGE(...)) formula filtered to one worker."""
    safe_name = worker_name.replace("'", "\\'")
    q = f"select {select_cols} where {filter_col} = '{safe_name}' order by {order_by}"
    return f'=QUERY(IMPORTRANGE("{source_sheet_id}", "{sheet_range}"), "{q}", 1)'


def create_view_sheet(worker: dict, parent_folder_id: str | None = None) -> tuple[str, str]:
    """Create a personal view sheet for one worker. Returns (sheet_id, url).
    Idempotent at the caller level — caller should check Roster for existing URL first.
    """
    parent_folder_id = parent_folder_id or config.SHEETS_FOLDER_ID
    if not parent_folder_id:
        raise RuntimeError("No SHEETS_FOLDER_ID configured; can't create view sheet")
    if not worker.get("email"):
        log.warning("Worker %s has no email — view sheet will be created but not shared with them", worker["name"])

    drive = _drive()
    gc = _gc()

    title = f"{worker['name']} — Your Hours & Pay"
    created = drive.files().create(
        body={
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [parent_folder_id],
        },
        fields="id",
        supportsAllDrives=True,
    ).execute()
    sheet_id = created["id"]
    log.info("Created view sheet '%s' (%s) for %s", title, sheet_id, worker["name"])

    # Share read-only with the worker
    if worker.get("email"):
        try:
            drive.permissions().create(
                fileId=sheet_id,
                body={"role": "reader", "type": "user", "emailAddress": worker["email"]},
                sendNotificationEmail=False,
                fields="id",
                supportsAllDrives=True,
            ).execute()
        except Exception:
            log.exception("Failed to share view sheet with %s", worker["email"])

    # Allow link-readers as a fallback (the worker can also be linked from Slack)
    try:
        drive.permissions().create(
            fileId=sheet_id,
            body={"role": "reader", "type": "anyone"},
            fields="id",
            supportsAllDrives=True,
        ).execute()
    except Exception:
        log.exception("Could not set link-reader permission on %s", sheet_id)

    ss = gc.open_by_key(sheet_id)
    first_tab = ss.sheet1

    # ---------- Tab 1: Daily Activity ----------
    first_tab.update_title("Daily Activity")
    first_tab.resize(rows=200, cols=8)
    header = [
        f"Daily activity for {worker['name']} — read-only · auto-updates from the central tracker",
    ]
    first_tab.update(values=[header], range_name="A1", value_input_option="USER_ENTERED")
    first_tab.format("A1", {"textFormat": {"bold": True, "fontSize": 12},
                            "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}})
    # The QUERY result includes a header row from the source; place it at A3 so the
    # banner sits in A1 with a blank row 2 separating it.
    formula = _query_formula(
        config.TRACKER_SHEET_ID,
        "Daily Summary!A:N",
        # Worker-friendly columns: Date, Login, EOD, Active Hours, Check-ins, Status, Notes
        "Col1, Col3, Col4, Col5, Col6, Col9, Col10",
        "Col2",
        worker["name"],
    )
    first_tab.update(values=[[formula]], range_name="A3", value_input_option="USER_ENTERED")
    first_tab.format("A3:G3", {"textFormat": {"bold": True}})
    first_tab.freeze(rows=3)

    # ---------- Tab 2: Pay History ----------
    pay_tab = ss.add_worksheet(title="Pay History", rows=100, cols=10)
    pay_tab.update(values=[[
        f"Pay history for {worker['name']} — read-only · auto-updates after each pay period closes",
    ]], range_name="A1", value_input_option="USER_ENTERED")
    pay_tab.format("A1", {"textFormat": {"bold": True, "fontSize": 12},
                          "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}})
    pay_formula = _query_formula(
        config.PAYROLL_SHEET_ID,
        "Payroll!A:Q",
        # Period Start, Period End, Pay Type, Days, Total Hours, Reg, OT, Rate, Salary, Gross, Currency
        "Col1, Col2, Col5, Col6, Col7, Col8, Col9, Col10, Col11, Col14, Col15",
        "Col3",
        worker["name"],
    )
    pay_tab.update(values=[[pay_formula]], range_name="A3", value_input_option="USER_ENTERED")
    pay_tab.format("A3:K3", {"textFormat": {"bold": True}})
    pay_tab.freeze(rows=3)

    # ---------- Tab 3: How to read this sheet ----------
    info = ss.add_worksheet(title="About", rows=20, cols=2)
    info.update(values=[
        ["About this sheet"],
        [""],
        ["This sheet shows your hours and pay history. It updates automatically as you check in via Slack with Sam."],
        [""],
        ["FIRST-TIME SETUP: if any tab shows a #REF! error or 'Allow access' button, click Allow once.That lets your sheet pull data from the central tracker. After that it just works."],
        [""],
        ["• Daily Activity tab: every day you've worked, your check-in time, EOD time, and total hours."],
        ["• Pay History tab: every closed pay period and what you earned. Future periods appear after the 15th / 1st."],
        [""],
        ["If something looks wrong, DM Sam with the details. Sam logs it and Jan reviews before payroll runs."],
        [""],
        ["You can also ask Sam at any time:"],
        ["  'hours' — see your current pay period total"],
        ["  'how many hours' — same thing"],
    ], range_name="A1", value_input_option="USER_ENTERED")
    info.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
    info.columns_auto_resize(0, 1)

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    return sheet_id, url


def _roster_url_column_index() -> int:
    """1-based column index of 'Personal View Sheet URL' on the Roster tab."""
    return config.ROSTER_HEADER.index("Personal View Sheet URL") + 1


def _column_letter(idx_1based: int) -> str:
    s = ""
    n = idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


def write_url_to_roster(slack_user_id: str, url: str) -> None:
    """Set the 'Personal View Sheet URL' cell for this worker."""
    ws = sheets.open_tracker().worksheet(config.ROSTER_TAB)
    rows = ws.get_all_values()
    if not rows:
        return
    header = rows[0]
    sid_col = header.index("Slack User ID") if "Slack User ID" in header else 1
    target_row = None
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > sid_col and r[sid_col].strip() == slack_user_id:
            target_row = i
            break
    if not target_row:
        return
    col_letter = _column_letter(_roster_url_column_index())
    ws.update(values=[[url]], range_name=f"{col_letter}{target_row}", value_input_option="USER_ENTERED")


def ensure_view_for_worker(worker: dict) -> Optional[str]:
    """Create a view sheet for this worker if they don't have one. Returns the URL.
    If they already have a URL on their Roster row, returns that (no creation).
    """
    if worker.get("personal_view_url"):
        return worker["personal_view_url"]
    try:
        sid, url = create_view_sheet(worker)
        write_url_to_roster(worker["user_id"], url)
        return url
    except Exception:
        log.exception("Failed to create view sheet for %s", worker["name"])
        return None


def create_views_for_all() -> dict[str, str]:
    """Bulk-create view sheets for every active worker who doesn't have one.
    Returns {worker_name: url}.
    """
    roster = sheets.load_roster()
    created: dict[str, str] = {}
    for w in roster:
        if w.get("personal_view_url"):
            log.info("Skipping %s — already has a view sheet", w["name"])
            continue
        url = ensure_view_for_worker(w)
        if url:
            created[w["name"]] = url
            log.info("View sheet ready for %s: %s", w["name"], url)
    return created
