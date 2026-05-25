"""Google Sheets I/O — Roster, Activity Log, Daily Summary tabs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from . import config

log = logging.getLogger(__name__)


def _creds() -> Credentials:
    return Credentials.from_service_account_file(config.SERVICE_ACCOUNT_JSON, scopes=config.SCOPES)


def gsclient() -> gspread.Client:
    return gspread.authorize(_creds())


def drive_service():
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


def open_tracker() -> gspread.Spreadsheet:
    if not config.TRACKER_SHEET_ID:
        raise RuntimeError("WORKER_TRACKER_SHEET_ID not set in .env — run setup_worker_sheet.py first")
    return gsclient().open_by_key(config.TRACKER_SHEET_ID)


def open_payroll() -> gspread.Spreadsheet:
    if not config.PAYROLL_SHEET_ID:
        raise RuntimeError("PAYROLL_SHEET_ID not set in .env — run setup_worker_sheet.py first")
    return gsclient().open_by_key(config.PAYROLL_SHEET_ID)


def append_event(worker_name: str, slack_user_id: str, event_type: str, message: str, tz_name: str) -> None:
    ws = open_tracker().worksheet(config.ACTIVITY_TAB)
    now_utc = datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local = now_utc.astimezone(tz)
    ws.append_row(
        [
            now_utc.isoformat(timespec="seconds"),
            local.date().isoformat(),
            local.strftime("%H:%M:%S"),
            worker_name,
            slack_user_id,
            event_type,
            message or "",
        ],
        value_input_option="USER_ENTERED",
    )


def load_roster() -> List[dict]:
    """Active workers only. Returns [{name, user_id, email, tz, expected_start, expected_eod}]."""
    ws = open_tracker().worksheet(config.ROSTER_TAB)
    rows = ws.get_all_records()
    workers: List[dict] = []
    for r in rows:
        if str(r.get("Active", "")).strip().upper() not in ("TRUE", "1", "YES", "Y"):
            continue
        uid = str(r.get("Slack User ID", "")).strip()
        if not uid:
            continue
        def _f(key: str, default: float = 0.0) -> float:
            try:
                v = str(r.get(key, "")).strip().replace("$", "").replace(",", "")
                return float(v) if v else default
            except (TypeError, ValueError):
                return default

        # Per-worker check-in cadence: blank/0 means "use global default"
        try:
            interval_raw = str(r.get("Check-in Frequency (min)", "")).strip()
            checkin_interval = int(float(interval_raw)) if interval_raw else 0
        except (TypeError, ValueError):
            checkin_interval = 0
        if checkin_interval <= 0:
            checkin_interval = config.CHECKIN_INTERVAL_MINUTES

        workers.append({
            "name": str(r.get("Name", "")).strip() or uid,
            "user_id": uid,
            # Work email used for everyday communication + sharing their personal view sheet
            "email": str(r.get("Work Email") or r.get("Email") or "").strip(),
            # Wise email used by the bookkeeper to send payouts (often different from work email)
            "wise_email": str(r.get("Wise Email", "")).strip(),
            "tz": str(r.get("Timezone") or "UTC").strip(),
            "expected_start": str(r.get("Expected Start") or "").strip(),
            "expected_eod": str(r.get("Expected EOD") or "").strip(),
            # Accept "salary" or "salaried" as the same thing; normalize for downstream code
            "pay_type": ("salaried" if str(r.get("Pay Type") or "hourly").strip().lower() in ("salary", "salaried")
                         else (str(r.get("Pay Type") or "hourly").strip().lower() or "hourly")),
            "hourly_rate": _f("Hourly Rate"),
            "salary_per_period": _f("Salary (per period)"),
            "currency": str(r.get("Currency") or config.PAYROLL_DEFAULT_CURRENCY).strip(),
            "ot_threshold": _f("Overtime Threshold (h/wk)", config.PAYROLL_DEFAULT_OT_THRESHOLD),
            "ot_multiplier": _f("Overtime Multiplier", config.PAYROLL_DEFAULT_OT_MULTIPLIER),
            "checkin_interval_min": checkin_interval,
            "personal_view_url": str(r.get("Personal View Sheet URL") or "").strip(),
        })
    return workers


def append_timesheet(row: List) -> None:
    """Append one row to the Timesheet tab on the payroll sheet."""
    ws = open_payroll().worksheet(config.TIMESHEET_TAB)
    ws.append_row(row, value_input_option="USER_ENTERED")


def append_payroll(row: List) -> None:
    """Append one row to the *separate* payroll sheet (not the tracker sheet)."""
    ws = open_payroll().worksheet(config.PAYROLL_TAB)
    ws.append_row(row, value_input_option="USER_ENTERED")


def summaries_in_range(start_iso: str, end_iso: str, slack_user_id: str | None = None) -> list[dict]:
    """Daily Summary rows whose Date column is between start_iso and end_iso (inclusive)."""
    ws = open_tracker().worksheet(config.SUMMARY_TAB)
    out = []
    for r in ws.get_all_records():
        d = str(r.get("Date", "")).strip()
        if not d or d < start_iso or d > end_iso:
            continue
        if slack_user_id and str(r.get("Worker", "")) and r.get("Slack User ID") != slack_user_id:
            # Daily Summary doesn't have a Slack User ID column — fallback to name match upstream
            pass
        out.append(r)
    return out


def activity_rows(local_date_iso: str | None = None) -> List[dict]:
    """Read full Activity Log (or one day's worth). Cheap for first few months."""
    ws = open_tracker().worksheet(config.ACTIVITY_TAB)
    rows = ws.get_all_records()
    if local_date_iso:
        rows = [r for r in rows if str(r.get("Local Date")) == local_date_iso]
    return rows


def append_summary(row: List) -> None:
    ws = open_tracker().worksheet(config.SUMMARY_TAB)
    ws.append_row(row, value_input_option="USER_ENTERED")


def load_profile(slack_user_id: str) -> dict | None:
    """Return the Worker Profile row for this user, or None if not yet created."""
    try:
        ws = open_tracker().worksheet(config.PROFILE_TAB)
    except Exception:
        return None
    rows = ws.get_all_records()
    for r in rows:
        if str(r.get("Slack User ID", "")).strip() == slack_user_id:
            return r
    return None


def upsert_profile(profile: dict) -> None:
    """Insert or update a single row in the Worker Profile tab, keyed by Slack User ID."""
    ws = open_tracker().worksheet(config.PROFILE_TAB)
    rows = ws.get_all_values()
    if not rows:
        ws.update(values=[list(config.PROFILE_HEADER)], range_name="A1", value_input_option="USER_ENTERED")
        rows = ws.get_all_values()
    header = rows[0]
    sid_col = header.index("Slack User ID") if "Slack User ID" in header else 1
    new_row = [str(profile.get(h, "")) for h in header]

    target_row_idx = None
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > sid_col and r[sid_col].strip() == str(profile.get("Slack User ID", "")).strip():
            target_row_idx = i
            break

    if target_row_idx:
        ws.update(values=[new_row], range_name=f"A{target_row_idx}", value_input_option="USER_ENTERED")
    else:
        ws.append_row(new_row, value_input_option="USER_ENTERED")


def all_profiles() -> list[dict]:
    try:
        ws = open_tracker().worksheet(config.PROFILE_TAB)
    except Exception:
        return []
    return ws.get_all_records()


def list_worker_knowledge(slack_user_id: str) -> list[dict]:
    """Return all Processes & Tools entries for one worker."""
    try:
        ws = open_tracker().worksheet(config.KNOWLEDGE_TAB)
    except Exception:
        return []
    return [r for r in ws.get_all_records() if str(r.get("Slack User ID", "")).strip() == slack_user_id]


def upsert_knowledge(entry: dict) -> str:
    """Insert a new Processes & Tools row OR update an existing one matching
    by (Slack User ID, Name). Returns 'inserted' or 'updated'."""
    ws = open_tracker().worksheet(config.KNOWLEDGE_TAB)
    rows = ws.get_all_values()
    if not rows:
        ws.update(values=[list(config.KNOWLEDGE_HEADER)], range_name="A1", value_input_option="USER_ENTERED")
        rows = ws.get_all_values()
    header = rows[0]

    sid_col = header.index("Slack User ID") if "Slack User ID" in header else 1
    name_col = header.index("Name") if "Name" in header else 3
    times_col = header.index("Times Referenced") if "Times Referenced" in header else 9

    target_row_idx = None
    target_existing = None
    target_uid = str(entry.get("Slack User ID", "")).strip()
    target_name = str(entry.get("Name", "")).strip().lower()
    for i, r in enumerate(rows[1:], start=2):
        if len(r) <= max(sid_col, name_col):
            continue
        if r[sid_col].strip() == target_uid and r[name_col].strip().lower() == target_name:
            target_row_idx = i
            target_existing = r
            break

    if target_row_idx and target_existing:
        # Update — merge: keep First Mentioned, bump Times Referenced, refresh URL/Description/Steps if new value present
        merged = list(target_existing) + [""] * (len(header) - len(target_existing))
        for col_name in ("Kind", "URL", "Description", "Steps / Notes"):
            if entry.get(col_name) and col_name in header:
                merged[header.index(col_name)] = str(entry[col_name])
        if "Last Updated" in header:
            merged[header.index("Last Updated")] = entry.get("Last Updated") or datetime.now(timezone.utc).date().isoformat()
        try:
            prev = int(merged[times_col] or 0)
        except (TypeError, ValueError):
            prev = 0
        merged[times_col] = str(prev + 1)
        ws.update(values=[merged], range_name=f"A{target_row_idx}", value_input_option="USER_ENTERED")
        return "updated"

    # Insert
    today = datetime.now(timezone.utc).date().isoformat()
    new_row = [str(entry.get(h, "")) for h in header]
    if "First Mentioned" in header and not new_row[header.index("First Mentioned")]:
        new_row[header.index("First Mentioned")] = today
    if "Last Updated" in header and not new_row[header.index("Last Updated")]:
        new_row[header.index("Last Updated")] = today
    if not new_row[times_col]:
        new_row[times_col] = "1"
    ws.append_row(new_row, value_input_option="USER_ENTERED")
    return "inserted"


def activity_since(days_back: int, slack_user_id: str | None = None) -> list[dict]:
    """All activity from the last N calendar days, optionally filtered by user."""
    from datetime import datetime, timezone, timedelta
    ws = open_tracker().worksheet(config.ACTIVITY_TAB)
    rows = ws.get_all_records()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r.get("Timestamp UTC", "")))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        if slack_user_id and str(r.get("Slack User ID", "")).strip() != slack_user_id:
            continue
        out.append(r)
    return out
