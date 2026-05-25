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
        workers.append({
            "name": str(r.get("Name", "")).strip() or uid,
            "user_id": uid,
            "email": str(r.get("Email", "")).strip(),
            "tz": str(r.get("Timezone") or "UTC").strip(),
            "expected_start": str(r.get("Expected Start") or "").strip(),
            "expected_eod": str(r.get("Expected EOD") or "").strip(),
        })
    return workers


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
