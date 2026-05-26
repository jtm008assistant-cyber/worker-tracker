"""Google Sheets I/O — Roster, Activity Log, Daily Summary tabs.

Includes a small caching + retry layer because we were hitting Google's
default 60 reads/min/user quota on bursty admin queries. See _ttl_cache and
_with_429_retry below — if you touch this file, keep both honored.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, List
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from . import config

log = logging.getLogger(__name__)


class RateLimited(Exception):
    """Raised when Sheets API 429s persist past the retry budget. The bot
    catches this and shows a friendly 'Sam is rate-limited, try again' to the
    user instead of the raw stack trace."""


# ---------------------------------------------------------------------------
# Retry layer: wrap any gspread read so transient 429 / 5xx auto-backoff.
# ---------------------------------------------------------------------------

def _with_429_retry(fn: Callable) -> Callable:
    """Retry a Sheets-API call with exponential backoff on 429 / 5xx errors.

    Total budget ~25s across 5 attempts. After that we raise RateLimited so
    the bot layer can show a friendly message rather than blasting the raw
    APIError stack trace at the user (which is what triggered this whole
    rebuild after Jan saw a 429 dump from a 'is jonny working' query).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        delays = [1.0, 2.0, 4.0, 8.0, 10.0]  # ~25s total
        last_exc: Exception | None = None
        for i, delay in enumerate([0.0] + delays):
            if delay:
                # Jitter so 5 concurrent handlers don't sync up on retries.
                time.sleep(delay + random.uniform(0, 0.5))
            try:
                return fn(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                last_exc = e
                code = None
                try:
                    code = int(e.response.status_code)  # type: ignore[union-attr]
                except Exception:
                    pass
                if code == 429 or (code is not None and 500 <= code < 600):
                    log.warning("Sheets API %s on %s, attempt %d/%d, backing off %.1fs",
                                code, fn.__name__, i + 1, len(delays) + 1, delays[i] if i < len(delays) else 0)
                    continue
                raise  # non-retryable APIError, bubble up
        # Budget exhausted — convert to RateLimited so callers can handle nicely
        raise RateLimited(
            f"Sheets API rate-limited after {len(delays) + 1} attempts on {fn.__name__}"
        ) from last_exc
    return wrapper


# ---------------------------------------------------------------------------
# TTL cache: thread-safe, keyed by function name + args.
# ---------------------------------------------------------------------------

_CACHE: dict[tuple, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


_CACHE_STATS = {"hits": 0, "fresh_misses": 0, "stale_serves": 0, "errors": 0}


def _ttl_cache(seconds: float, stale_seconds: float = 3600) -> Callable:
    """Decorator: cache the result of fn(*args, **kwargs) for `seconds`.

    Two-tier behavior:
      - Within `seconds`: serve cached value, no API call. (Hit.)
      - Between `seconds` and `stale_seconds`: try fresh fetch; if it
        succeeds, replace cache. If it RAISES (RateLimited, APIError, etc.),
        return the STALE cached value with a warning log. (Stale serve.)
      - After `stale_seconds`: no fallback. Fresh fetch must succeed or
        the exception propagates.

    This means: even if Sheets API is fully exhausted, Sam keeps answering
    with data up to 1 hour old instead of throwing — the user can keep
    chatting through a quota event without ever seeing a crash.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
            if hit and (now - hit[0]) < seconds:
                _CACHE_STATS["hits"] += 1
                return hit[1]
            # Cache miss or stale — attempt a fresh fetch.
            try:
                val = fn(*args, **kwargs)
                with _CACHE_LOCK:
                    _CACHE[key] = (now, val)
                _CACHE_STATS["fresh_misses"] += 1
                return val
            except Exception as e:
                # Fresh fetch failed. If we have a stale-but-not-ancient cached
                # value, serve it rather than crash. Especially useful when
                # the failure is RateLimited (the whole point of this layer).
                if hit and (now - hit[0]) < stale_seconds:
                    _CACHE_STATS["stale_serves"] += 1
                    age = now - hit[0]
                    log.warning(
                        "%s fresh fetch failed (%s: %s) — serving stale cache (age %.0fs)",
                        fn.__name__, type(e).__name__, e, age,
                    )
                    return hit[1]
                _CACHE_STATS["errors"] += 1
                raise
        # Expose an invalidator so write-paths can punch out stale entries.
        def _invalidate(*args, **kwargs) -> None:
            with _CACHE_LOCK:
                if not args and not kwargs:
                    # Invalidate ALL entries for this function
                    keys_to_drop = [k for k in _CACHE if k[0] == fn.__name__]
                    for k in keys_to_drop:
                        _CACHE.pop(k, None)
                else:
                    key = (fn.__name__, args, tuple(sorted(kwargs.items())))
                    _CACHE.pop(key, None)
        wrapper.invalidate = _invalidate  # type: ignore[attr-defined]
        return wrapper
    return decorator


def cache_stats() -> dict:
    """Snapshot of cache hit/miss/stale counters. Useful for logging or
    diagnosing why we're hitting the API too often."""
    total = sum(_CACHE_STATS.values())
    return {
        **_CACHE_STATS,
        "total": total,
        "hit_rate": (_CACHE_STATS["hits"] / total) if total else 0.0,
    }


def _creds() -> Credentials:
    return Credentials.from_service_account_file(config.SERVICE_ACCOUNT_JSON, scopes=config.SCOPES)


@_ttl_cache(seconds=300)
def gsclient() -> gspread.Client:
    """Cached for 5 min — reusing the authorized client avoids repeated
    OAuth token exchanges on every API call. Token has its own ~1h lifetime
    so 5 min is well within the safety window."""
    return gspread.authorize(_creds())


def drive_service():
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


@_ttl_cache(seconds=300)
def open_tracker() -> gspread.Spreadsheet:
    if not config.TRACKER_SHEET_ID:
        raise RuntimeError("WORKER_TRACKER_SHEET_ID not set in .env — run setup_worker_sheet.py first")
    return gsclient().open_by_key(config.TRACKER_SHEET_ID)


@_ttl_cache(seconds=300)
def open_payroll() -> gspread.Spreadsheet:
    if not config.PAYROLL_SHEET_ID:
        raise RuntimeError("PAYROLL_SHEET_ID not set in .env — run setup_worker_sheet.py first")
    return gsclient().open_by_key(config.PAYROLL_SHEET_ID)


@_with_429_retry
def append_event(worker_name: str, slack_user_id: str, event_type: str, message: str, tz_name: str) -> None:
    ws = open_tracker().worksheet(config.ACTIVITY_TAB)
    now_utc = datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local = now_utc.astimezone(tz)
    # Append invalidates today's activity cache so subsequent reads see this row.
    try:
        activity_rows.invalidate(local.date().isoformat())  # type: ignore[attr-defined]
        activity_rows.invalidate()  # also drop the no-arg "full log" cache key
    except Exception:
        pass
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


@_ttl_cache(seconds=300)
@_with_429_retry
def load_roster() -> List[dict]:
    """Active workers only.

    Cached 5min (stale-tolerant up to 1h) — Roster is edited manually ~weekly,
    so 5min is invisible to users. reload_roster() is called from 13+ places
    in bot.py; without this, each scheduled handler's first read costs us a
    full Sheets fetch. With this, ~1 read per 5min regardless of caller count.
    """
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
            # Payout details — could be Wise, e-Transfer, PayPal, direct deposit, etc.
            "payout_email": str(r.get("Payout Email") or r.get("Wise Email") or "").strip(),
            "payout_method": (str(r.get("Payout Method") or "").strip().lower() or "wise"),
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
            # Comma-separated nicknames the bot will match against (e.g. "norks, norlan")
            "nicknames": [
                n.strip().lower() for n in str(r.get("Nicknames", "")).split(",") if n.strip()
            ],
            "vacation_days_year": _f("Vacation Days/Year"),
            "sick_days_year": _f("Sick Days/Year"),
            "holiday_days_year": _f("Holiday Days/Year"),
            "pto_days_year": _f("PTO Days/Year"),
            "benefits_notes": str(r.get("Benefits Notes", "")).strip(),
            "hourly_rate_contract": str(r.get("Hourly Rate (from Contract)", "")).strip(),
            "pay_schedule": str(r.get("Pay Schedule", "")).strip(),
            "hmo_reimbursement_php": _f("HMO Reimbursement (PHP/yr)"),
            "thirteenth_month_eligible": str(r.get("13th Month Eligible", "")).strip(),
            "perf_bonus_date": str(r.get("Performance Bonus Date", "")).strip(),
            "calamity_fund_php": _f("Calamity Fund (PHP/yr)"),
            "contract_start": str(r.get("Contract Start Date", "")).strip(),
            "probation_end": str(r.get("Probation End Date", "")).strip(),
        })
    return workers


@_with_429_retry
def append_time_off(row: List) -> None:
    """Append a row to the Time Off tab on the tracker sheet."""
    ws = open_tracker().worksheet(config.TIME_OFF_TAB)
    ws.append_row(row, value_input_option="USER_ENTERED")
    try:
        time_off_for_worker.invalidate()  # type: ignore[attr-defined]
    except Exception:
        pass


def append_commitment(row: List) -> None:
    """Append a row to the Commitments tab."""
    ws = open_tracker().worksheet(config.COMMITMENTS_TAB)
    ws.append_row(row, value_input_option="USER_ENTERED")


# ---------------- Relay Queue ----------------

def append_relay(row: List) -> None:
    """Append a row to the Relay Queue tab."""
    ws = open_tracker().worksheet(config.RELAY_TAB)
    ws.append_row(row, value_input_option="USER_ENTERED")


def _relay_rows() -> tuple[list[list[str]], list[str]]:
    """Return (rows_including_header, header)."""
    try:
        ws = open_tracker().worksheet(config.RELAY_TAB)
    except Exception:
        return [], []
    rows = ws.get_all_values()
    if not rows:
        return [], []
    return rows, rows[0]


def list_pending_relays_for_worker(slack_user_id: str) -> list[dict]:
    """Relays addressed to this worker with status='pending' — i.e. not yet
    delivered. Returned oldest-first."""
    rows, header = _relay_rows()
    if not rows:
        return []
    out: list[dict] = []
    for r in rows[1:]:
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        if rec.get("To Slack ID", "").strip() != slack_user_id:
            continue
        if (rec.get("Status") or "").strip().lower() != "pending":
            continue
        out.append(rec)
    out.sort(key=lambda r: str(r.get("Date Created", "")))
    return out


def list_delivered_relays_for_worker(slack_user_id: str) -> list[dict]:
    """Relays delivered to this worker but not yet completed/dropped — these
    are the ones we'll watch for completion in the worker's next reply."""
    rows, header = _relay_rows()
    if not rows:
        return []
    out: list[dict] = []
    for r in rows[1:]:
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        if rec.get("To Slack ID", "").strip() != slack_user_id:
            continue
        if (rec.get("Status") or "").strip().lower() != "delivered":
            continue
        out.append(rec)
    out.sort(key=lambda r: str(r.get("Date Created", "")))
    return out


def _update_relay_row_by_id(relay_id: str, updates: dict) -> bool:
    """Find the row whose Relay ID matches and patch the columns in `updates`
    (mapping of header name -> new value). Returns True on success."""
    try:
        ws = open_tracker().worksheet(config.RELAY_TAB)
    except Exception:
        return False
    rows = ws.get_all_values()
    if not rows:
        return False
    header = rows[0]
    try:
        id_col = header.index("Relay ID")
    except ValueError:
        return False
    target = relay_id.strip()
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > id_col and r[id_col].strip() == target:
            for k, v in updates.items():
                if k in header:
                    ws.update_cell(i, header.index(k) + 1, v)
            return True
    return False


def mark_relay_delivered(relay_id: str) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    return _update_relay_row_by_id(relay_id, {
        "Status": "delivered",
        "Date Delivered": today,
    })


def mark_relay_done(relay_id: str, worker_reply: str = "", notes: str = "") -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    return _update_relay_row_by_id(relay_id, {
        "Status": "done",
        "Date Completed": today,
        "Worker Reply": worker_reply or "",
        "Notes": notes or "",
    })


def mark_relay_dropped(relay_id: str, notes: str = "") -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    return _update_relay_row_by_id(relay_id, {
        "Status": "dropped",
        "Date Completed": today,
        "Notes": notes or "",
    })


def list_open_commitments(slack_user_id: str) -> list[dict]:
    """All open (not done/dropped) commitments for one worker, oldest first."""
    try:
        ws = open_tracker().worksheet(config.COMMITMENTS_TAB)
    except Exception:
        return []
    rows = ws.get_all_records()
    out = []
    for r in rows:
        if str(r.get("Slack User ID", "")).strip() != slack_user_id:
            continue
        status = (r.get("Status") or "").strip().lower()
        if status not in ("done", "dropped", "resolved"):
            out.append(r)
    out.sort(key=lambda r: str(r.get("Date Created", "")))
    return out


def mark_commitment_status(slack_user_id: str, commitment_text: str,
                            new_status: str, resolution_notes: str = "") -> bool:
    """Mark a worker's commitment matching the text as done/dropped. Returns True
    if a row was updated. Best-effort match — exact text on Commitment col."""
    try:
        ws = open_tracker().worksheet(config.COMMITMENTS_TAB)
    except Exception:
        return False
    rows = ws.get_all_values()
    if not rows:
        return False
    header = rows[0]
    try:
        sid_col = header.index("Slack User ID")
        commit_col = header.index("Commitment")
        status_col = header.index("Status")
        resolved_col = header.index("Date Resolved")
        notes_col = header.index("Resolution Notes")
    except ValueError:
        return False
    target_text = commitment_text.strip().lower()
    today = datetime.now(timezone.utc).date().isoformat()
    for i, r in enumerate(rows[1:], start=2):
        if (len(r) > max(sid_col, commit_col, status_col)
                and r[sid_col].strip() == slack_user_id
                and r[commit_col].strip().lower() == target_text):
            ws.update_cell(i, status_col + 1, new_status)
            ws.update_cell(i, resolved_col + 1, today)
            if resolution_notes:
                ws.update_cell(i, notes_col + 1, resolution_notes)
            return True
    return False


@_ttl_cache(seconds=300)
@_with_429_retry
def time_off_for_worker(slack_user_id: str, year: int | None = None) -> list[dict]:
    """Return all Time Off rows for one worker (optionally filtered to a specific year by Start Date).
    Cached 60s — append_time_off invalidates."""
    try:
        ws = open_tracker().worksheet(config.TIME_OFF_TAB)
    except Exception:
        return []
    rows = ws.get_all_records()
    out = []
    for r in rows:
        if str(r.get("Slack User ID", "")).strip() != slack_user_id:
            continue
        if year:
            try:
                start = str(r.get("Start Date", ""))
                if start and not start.startswith(str(year)):
                    continue
            except Exception:
                pass
        out.append(r)
    return out


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


@_ttl_cache(seconds=60)
@_with_429_retry
def activity_rows(local_date_iso: str | None = None) -> List[dict]:
    """Read full Activity Log (or one day's worth).

    Cached 60s (stale-tolerant 1h) — admin queries and the periodic
    snapshot builder all hit this. Cache is invalidated by append_event()
    so newly logged events appear immediately to *subsequent* reads.
    Between unrelated handlers this is ~1 read/min instead of N.
    """
    ws = open_tracker().worksheet(config.ACTIVITY_TAB)
    rows = ws.get_all_records()
    if local_date_iso:
        rows = [r for r in rows if str(r.get("Local Date")) == local_date_iso]
    return rows


@_with_429_retry
def append_summary(row: List) -> None:
    ws = open_tracker().worksheet(config.SUMMARY_TAB)
    ws.append_row(row, value_input_option="USER_ENTERED")


@_ttl_cache(seconds=300)
@_with_429_retry
def load_profile(slack_user_id: str) -> dict | None:
    """Return the Worker Profile row for this user, or None if not yet created.

    Cached 60s — profiles are updated once weekly by the synthesis cron, so
    short cache window is safe and saves a Sheets read on every memory query.
    """
    try:
        ws = open_tracker().worksheet(config.PROFILE_TAB)
    except Exception:
        return None
    rows = ws.get_all_records()
    for r in rows:
        if str(r.get("Slack User ID", "")).strip() == slack_user_id:
            return r
    return None


@_with_429_retry
def upsert_profile(profile: dict) -> None:
    """Insert or update a single row in the Worker Profile tab, keyed by Slack User ID."""
    try:
        load_profile.invalidate()  # type: ignore[attr-defined]
        all_profiles.invalidate()  # type: ignore[attr-defined]
    except Exception:
        pass
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


@_ttl_cache(seconds=300)
@_with_429_retry
def all_profiles() -> list[dict]:
    try:
        ws = open_tracker().worksheet(config.PROFILE_TAB)
    except Exception:
        return []
    return ws.get_all_records()


@_ttl_cache(seconds=300)
@_with_429_retry
def list_worker_knowledge(slack_user_id: str) -> list[dict]:
    """Return all Processes & Tools entries for one worker. Cached 60s — written by
    upsert_knowledge() which invalidates this cache."""
    try:
        ws = open_tracker().worksheet(config.KNOWLEDGE_TAB)
    except Exception:
        return []
    return [r for r in ws.get_all_records() if str(r.get("Slack User ID", "")).strip() == slack_user_id]


@_with_429_retry
def upsert_knowledge(entry: dict) -> str:
    """Insert a new Processes & Tools row OR update an existing one matching
    by (Slack User ID, Name). Returns 'inserted' or 'updated'."""
    try:
        list_worker_knowledge.invalidate()  # type: ignore[attr-defined]
    except Exception:
        pass
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


@_ttl_cache(seconds=30)
@_with_429_retry
def activity_since(days_back: int, slack_user_id: str | None = None) -> list[dict]:
    """All activity from the last N calendar days, optionally filtered by user.
    Cached 30s — primarily hit by 'X hasn't clocked in today' lookback and
    weekly memory synthesis."""
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
