"""Tier 2a — All actions as pure functions.

SDK-agnostic. Every Sam action — read or write — lives here as a Python
function that takes args and returns a dict. The agent calls them, the
fallback layer calls them, even the deterministic commands call into
them. Single source of truth for "what Sam can do".

Permission gate enforced at the entry of every per-worker tool:
  - workers querying peer workers → OK
  - workers querying owners → blocked with {"error": ...}
  - admins (owners + managers) → unrestricted

Tools return dicts with either:
  - normal result fields, OR
  - {"error": "..."} on failure (caller decides how to surface)

None of these tools raise exceptions on user-facing errors. Internal
crashes are caught and logged; the dict comes back with an error field.
"""
from __future__ import annotations

import difflib
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import config, sheets

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────

def resolve_worker(name_query: str, workers: list[dict]) -> dict | None:
    """Fuzzy name match with typo + nickname tolerance."""
    if not name_query:
        return None
    q = name_query.strip().lower()

    # Exact full-name
    for w in workers:
        if w["name"].lower() == q:
            return w
    # First-name exact
    for w in workers:
        if w["name"].split()[0].lower() == q:
            return w
    # Nickname exact
    for w in workers:
        if q in (w.get("nicknames") or []):
            return w
    # Substring of name parts
    for w in workers:
        parts = [w["name"].lower(), w["name"].split()[0].lower()]
        parts.extend(w.get("nicknames") or [])
        if any(q in p for p in parts):
            return w
    # Fuzzy fallback (typos)
    scored: list[tuple[float, dict]] = []
    for w in workers:
        cands = [w["name"].lower(), w["name"].split()[0].lower()]
        cands.extend(w.get("nicknames") or [])
        best = max((difflib.SequenceMatcher(None, q, c).ratio() for c in cands), default=0.0)
        scored.append((best, w))
    scored.sort(key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] >= 0.72:
        return scored[0][1]
    return None


def _gate(target: dict | None, is_speaker_admin: bool) -> dict | None:
    """Return None if the caller may proceed with this target. Return an
    error dict if a non-admin is trying to query an owner."""
    if target is None:
        return None
    if not is_speaker_admin and target["user_id"] in config.OWNER_SLACK_IDS:
        return {"error": "Owner-level data is restricted to admins. Ask Jan or Ideen directly."}
    return None


def parse_when(when: str, tz_name: str = "UTC") -> tuple[str, str]:
    """Parse 'when' into (start_date_iso, end_date_iso) inclusive.

    Accepts: 'today' / 'yesterday' / 'last_week' / 'this_week' / 'this_month'
             / 'last_month' / 'last_7_days' / 'YYYY-MM-DD'
             / 'YYYY-MM-DD to YYYY-MM-DD' / month name / etc.
    """
    try:
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    today_local = datetime.now(tz).date()
    w = (when or "today").lower().strip()

    if w == "today":
        return today_local.isoformat(), today_local.isoformat()
    if w == "yesterday":
        d = today_local - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if w in ("last_week", "last week", "past_week", "past week",
              "last_7_days", "last 7 days", "past 7 days"):
        return (today_local - timedelta(days=7)).isoformat(), today_local.isoformat()
    if w in ("this_week", "this week"):
        start = today_local - timedelta(days=today_local.weekday())
        return start.isoformat(), today_local.isoformat()
    if w in ("this_month", "this month"):
        return today_local.replace(day=1).isoformat(), today_local.isoformat()
    if w in ("last_month", "last month"):
        first_this = today_local.replace(day=1)
        last_last = first_this - timedelta(days=1)
        return last_last.replace(day=1).isoformat(), last_last.isoformat()

    # "YYYY-MM-DD to YYYY-MM-DD"
    if " to " in w or " through " in w:
        parts = re.split(r"\s+(?:to|through)\s+", w)
        if len(parts) == 2:
            try:
                a = datetime.fromisoformat(parts[0].strip()).date()
                b = datetime.fromisoformat(parts[1].strip()).date()
                return a.isoformat(), b.isoformat()
            except Exception:
                pass

    # Single ISO date
    try:
        d = datetime.fromisoformat(w).date()
        return d.isoformat(), d.isoformat()
    except Exception:
        pass

    return today_local.isoformat(), today_local.isoformat()


def parse_hhmm_to_utc(hhmm: str, tz_name: str, date_iso: str | None = None) -> datetime | None:
    """Parse '7:44am' / '18:30' / '6pm' into UTC datetime, anchored to today
    (or the given local date) in the worker's TZ. If the result is in the
    future relative to 'now', assumes yesterday."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    s = (hhmm or "").strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2}):?(\d{0,2})\s*(am|pm)?$", s)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if date_iso:
        try:
            base = date.fromisoformat(date_iso)
        except Exception:
            base = datetime.now(tz).date()
    else:
        base = datetime.now(tz).date()
    local_dt = datetime(base.year, base.month, base.day, h, mi, 0, tzinfo=tz)
    if local_dt > datetime.now(tz):
        local_dt -= timedelta(days=1)
    return local_dt.astimezone(timezone.utc)


def _write_activity_row(worker: dict, event_type: str, message: str,
                        when_utc: datetime) -> None:
    """Backdated Activity Log write at a specified UTC time."""
    try:
        tz = ZoneInfo(worker["tz"])
    except Exception:
        tz = ZoneInfo("UTC")
    local = when_utc.astimezone(tz)
    ws = sheets.open_tracker().worksheet(config.ACTIVITY_TAB)
    ws.append_row([
        when_utc.isoformat(timespec="seconds"),
        local.date().isoformat(),
        local.strftime("%H:%M:%S"),
        worker["name"],
        worker["user_id"],
        event_type,
        message,
    ], value_input_option="USER_ENTERED")
    # Invalidate caches so subsequent reads see this write
    try:
        sheets.activity_rows.invalidate()  # type: ignore[attr-defined]
        sheets.activity_since.invalidate()  # type: ignore[attr-defined]
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# READ tools — observe team state
# ─────────────────────────────────────────────────────────────────────────

def get_worker_status(name: str, workers: list[dict],
                     is_speaker_admin: bool = False) -> dict:
    """Current state — working/on_break/logged_off/not_started — for the
    worker's CURRENT or MOST RECENT shift. Shift = unbroken login→eod span,
    NOT a calendar day (so it works across midnight)."""
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    gate = _gate(target, is_speaker_admin)
    if gate:
        return gate

    try:
        recent = sheets.activity_since(2, slack_user_id=target["user_id"])
    except Exception as e:
        log.exception("get_worker_status: activity fetch failed")
        return {"error": f"couldn't read activity: {e}"}
    recent.sort(key=lambda r: r.get("Timestamp UTC", ""))

    login_ts = eod_ts = None
    break_start_ts = None
    break_total_sec = 0.0
    state = "not_started"
    last_msg = ""
    last_msg_ts = None

    for r in recent:
        try:
            ts = datetime.fromisoformat(r["Timestamp UTC"]).astimezone(timezone.utc)
        except Exception:
            continue
        t = r.get("Type", "")
        if t == "login":
            # Duplicate-login fold
            if login_ts and not eod_ts and (ts - login_ts).total_seconds() < 8 * 3600:
                continue
            login_ts = ts
            eod_ts = None
            break_start_ts = None
            break_total_sec = 0.0
            state = "working"
        elif t == "eod":
            eod_ts = ts
            state = "logged_off"
            if break_start_ts:
                break_total_sec += (ts - break_start_ts).total_seconds()
                break_start_ts = None
        elif t == "break_start":
            break_start_ts = ts
            state = "on_break"
        elif t == "break_end":
            if break_start_ts:
                break_total_sec += (ts - break_start_ts).total_seconds()
            break_start_ts = None
            state = "working"
        elif t in ("checkin", "help_request"):
            msg = str(r.get("Message") or "").strip()
            if msg:
                last_msg = msg
                last_msg_ts = ts

    if not login_ts:
        return {"name": target["name"], "state": "not_started",
                "hours_so_far": 0.0,
                "message": "Hasn't clocked in for the current/most recent shift."}

    now = datetime.now(timezone.utc)
    end = eod_ts or now
    elapsed = (end - login_ts).total_seconds()
    breaks = break_total_sec
    if break_start_ts and state == "on_break":
        breaks += (now - break_start_ts).total_seconds()
    active_hours = max(0.0, (elapsed - breaks) / 3600.0)

    try:
        tz = ZoneInfo(target["tz"])
    except Exception:
        tz = ZoneInfo("UTC")
    return {
        "name": target["name"],
        "state": state,
        "hours_so_far": round(active_hours, 2),
        "login_local": login_ts.astimezone(tz).strftime("%H:%M %Z"),
        "eod_local": eod_ts.astimezone(tz).strftime("%H:%M %Z") if eod_ts else None,
        "last_checkin_message": last_msg[:300] if last_msg else None,
        "last_checkin_minutes_ago": int((now - last_msg_ts).total_seconds() / 60) if last_msg_ts else None,
        "current_break_minutes": int((now - break_start_ts).total_seconds() / 60) if break_start_ts else None,
        "tz": target["tz"],
    }


def get_worker_activity(name: str, when: str, workers: list[dict],
                       is_speaker_admin: bool = False) -> dict:
    """Chronological list of meaningful events (login/checkin/break/eod) for
    a worker over a date or date range."""
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    gate = _gate(target, is_speaker_admin)
    if gate:
        return gate

    start_iso, end_iso = parse_when(when, target.get("tz", "UTC"))
    end_d = date.fromisoformat(end_iso)
    start_d = date.fromisoformat(start_iso)
    days_back = min(60, max(1, (end_d - start_d).days + 1) + 1)

    try:
        rows = sheets.activity_since(days_back, slack_user_id=target["user_id"])
    except Exception as e:
        return {"error": f"couldn't read activity: {e}"}

    rows = [r for r in rows if start_iso <= str(r.get("Local Date", "")) <= end_iso]
    rows.sort(key=lambda r: r.get("Timestamp UTC", ""))

    events = []
    for r in rows:
        t = r.get("Type", "")
        if t.startswith("sam_") or t == "prompt_sent":
            continue
        msg = str(r.get("Message") or "").strip()
        events.append({
            "date": r.get("Local Date", ""),
            "time": (r.get("Local Time") or "")[:5],
            "type": t,
            "message": msg[:400],
        })
    return {
        "name": target["name"],
        "date_range": f"{start_iso} to {end_iso}",
        "events": events[:150],
        "event_count": len(events),
    }


def get_worker_hours(name: str, period: str, workers: list[dict],
                    is_speaker_admin: bool = False) -> dict:
    """Pay-period hours including today's open session."""
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    gate = _gate(target, is_speaker_admin)
    if gate:
        return gate

    try:
        from . import payroll
        if period in ("", "current", "this_period"):
            start, end = payroll.current_open_period()
        elif period in ("previous", "last_period"):
            cs, _ = payroll.current_open_period()
            end = cs - timedelta(days=1)
            start = end.replace(day=16) if end.day >= 16 else end.replace(day=1)
        else:
            start, end = payroll.current_open_period()
        totals = payroll.worker_period_totals(target, start, end)
        live = get_worker_status(name, workers, is_speaker_admin=True)
        today_hours = 0.0
        if isinstance(live, dict) and live.get("state") in ("working", "on_break"):
            today_hours = live.get("hours_so_far", 0.0)
        return {
            "name": target["name"],
            "period": f"{start} → {end}",
            "days_completed": totals.get("days_worked", 0),
            "hours_completed_days": totals.get("total_hours", 0),
            "regular_hours": totals.get("regular_hours", 0),
            "overtime_hours": totals.get("overtime_hours", 0),
            "hours_today_open_session": round(today_hours, 2),
            "total_incl_today": round(totals.get("total_hours", 0) + today_hours, 2),
        }
    except Exception as e:
        log.exception("get_worker_hours failed")
        return {"error": f"hours calc failed: {e}"}


def get_worker_benefits(name: str, workers: list[dict],
                       is_speaker_admin: bool = False) -> dict:
    """Vacation/sick/holiday/PTO allocations + used + remaining, plus HMO,
    calamity fund, perf bonus date, pay schedule, hourly rate, 13th month."""
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    gate = _gate(target, is_speaker_admin)
    if gate:
        return gate

    year = datetime.now(ZoneInfo(target.get("tz") or "UTC")).year
    alloc = {
        "vacation": int(target.get("vacation_days_year") or 0),
        "sick": int(target.get("sick_days_year") or 0),
        "holiday": int(target.get("holiday_days_year") or 0),
        "pto": int(target.get("pto_days_year") or 0),
    }
    used = {"vacation": 0, "sick": 0, "pto": 0, "holiday": 0, "personal": 0, "unpaid": 0}
    try:
        for r in sheets.time_off_for_worker(target["user_id"], year=year):
            t = (r.get("Type") or "").strip().lower()
            if t in used:
                try:
                    used[t] += int(r.get("Days") or 0)
                except (TypeError, ValueError):
                    pass
    except Exception:
        log.exception("benefits used-lookup failed")

    return {
        "name": target["name"],
        "year": year,
        "allocations": alloc,
        "used": {k: v for k, v in used.items() if v > 0 or k in alloc},
        "remaining": {k: alloc.get(k, 0) - used.get(k, 0) for k in alloc},
        "notes": target.get("benefits_notes") or "",
        "extras": {
            "hmo_reimbursement_php": int(float(target.get("hmo_reimbursement_php") or 0)),
            "calamity_fund_php": int(float(target.get("calamity_fund_php") or 0)),
            "performance_bonus_date": target.get("perf_bonus_date") or "",
            "thirteenth_month_eligible": target.get("thirteenth_month_eligible") or "No",
            "pay_schedule": target.get("pay_schedule") or "",
            "hourly_rate_contract": target.get("hourly_rate_contract") or "",
        },
    }


def get_worker_open_tasks(name: str, workers: list[dict],
                         is_speaker_admin: bool = False) -> dict:
    """Pending relays + delivered-but-not-done relays + open self-commitments."""
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    gate = _gate(target, is_speaker_admin)
    if gate:
        return gate

    try:
        pending = sheets.list_pending_relays_for_worker(target["user_id"])
    except Exception:
        pending = []
    try:
        delivered = sheets.list_delivered_relays_for_worker(target["user_id"])
    except Exception:
        delivered = []
    try:
        commitments = sheets.list_open_commitments(target["user_id"])
    except Exception:
        commitments = []
    return {
        "name": target["name"],
        "pending_relays": [{"message": r.get("Message", ""), "from": r.get("From Name", ""),
                            "queued": r.get("Date Created", "")} for r in pending],
        "delivered_relays_awaiting_completion": [
            {"message": r.get("Message", ""), "from": r.get("From Name", ""),
             "delivered": r.get("Date Delivered", "")} for r in delivered
        ],
        "self_commitments": [{"text": c.get("Commitment", ""), "created": c.get("Date Created", ""),
                              "due": c.get("Due By", "")} for c in commitments],
    }


def get_worker_knowledge(name: str, workers: list[dict],
                        is_speaker_admin: bool = False) -> dict:
    """Tools/processes/people/sheets/jobs Sam has logged for this worker."""
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    gate = _gate(target, is_speaker_admin)
    if gate:
        return gate

    try:
        kb = sheets.list_worker_knowledge(target["user_id"])
    except Exception as e:
        return {"error": f"KB read failed: {e}"}
    return {
        "name": target["name"],
        "entry_count": len(kb),
        "entries": [{
            "kind": k.get("Kind", ""), "name": k.get("Name", ""),
            "url": k.get("URL", ""), "description": k.get("Description", ""),
            "steps": k.get("Steps / Notes", ""),
            "first_seen": k.get("First Mentioned", ""),
            "times_referenced": k.get("Times Referenced", ""),
        } for k in kb],
    }


def get_team_status(workers: list[dict]) -> dict:
    """Current state for every active non-owner worker."""
    out = []
    for w in workers:
        if w["user_id"] in config.OWNER_SLACK_IDS:
            continue
        try:
            s = get_worker_status(w["name"].split()[0], workers, is_speaker_admin=True)
            if "error" not in s:
                out.append(s)
        except Exception:
            pass
    return {"workers": out, "count": len(out)}


def get_all_benefits(workers: list[dict]) -> dict:
    """Compact benefits table for every active worker — for comparison queries."""
    rows = []
    for w in workers:
        if w["user_id"] in config.OWNER_SLACK_IDS:
            continue
        try:
            year = datetime.now(ZoneInfo(w.get("tz") or "UTC")).year
        except Exception:
            year = datetime.now(timezone.utc).year
        alloc = {
            "vacation": int(w.get("vacation_days_year") or 0),
            "sick": int(w.get("sick_days_year") or 0),
            "holiday": int(w.get("holiday_days_year") or 0),
            "pto": int(w.get("pto_days_year") or 0),
        }
        used = {"vacation": 0, "sick": 0, "pto": 0, "holiday": 0}
        try:
            for r in sheets.time_off_for_worker(w["user_id"], year=year):
                t = (r.get("Type") or "").strip().lower()
                if t in used:
                    try:
                        used[t] += int(r.get("Days") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
        rows.append({
            "name": w["name"],
            "vacation_remaining": alloc["vacation"] - used["vacation"],
            "sick_remaining": alloc["sick"] - used["sick"],
            "holiday_remaining": alloc["holiday"] - used["holiday"],
            "pto_remaining": alloc["pto"] - used["pto"],
            "vacation_total": alloc["vacation"],
            "sick_total": alloc["sick"],
            "holiday_total": alloc["holiday"],
            "pto_total": alloc["pto"],
            "hmo_php": int(float(w.get("hmo_reimbursement_php") or 0)),
            "calamity_php": int(float(w.get("calamity_fund_php") or 0)),
            "perf_bonus_date": w.get("perf_bonus_date") or "",
            "thirteenth_month_eligible": w.get("thirteenth_month_eligible") or "No",
            "pay_schedule": w.get("pay_schedule") or "",
            "hourly_rate": w.get("hourly_rate_contract") or "",
        })
    return {"workers": rows, "count": len(rows)}


def get_learned_today(workers: list[dict]) -> dict:
    """What Sam captured today across the team."""
    today_local = datetime.now(ZoneInfo(config.MANAGER_TZ)).date().isoformat()
    new_kb = []
    for w in workers:
        if w["user_id"] in config.OWNER_SLACK_IDS:
            continue
        try:
            kb = sheets.list_worker_knowledge(w["user_id"])
        except Exception:
            continue
        for k in kb:
            f = (k.get("First Mentioned") or "")[:10]
            l = (k.get("Last Updated") or "")[:10]
            if f == today_local or l == today_local:
                new_kb.append({
                    "worker": w["name"], "kind": k.get("Kind", ""),
                    "name": k.get("Name", ""), "description": k.get("Description", "")
                })
    try:
        all_today = sheets.activity_rows(today_local)
    except Exception:
        all_today = []
    by_worker: dict[str, list[str]] = {}
    for r in all_today:
        if r.get("Type") not in ("checkin", "help_request"):
            continue
        uid = (r.get("Slack User ID") or "").strip()
        if uid in config.OWNER_SLACK_IDS:
            continue
        msg = str(r.get("Message") or "").strip()
        if len(msg) < 30:
            continue
        by_worker.setdefault((r.get("Worker") or "").strip(), []).append(msg[:300])
    return {
        "date": today_local,
        "new_knowledge": new_kb,
        "substantive_checkins_by_worker": by_worker,
    }


def get_roster_summary(workers: list[dict]) -> dict:
    """Full active roster with first names, TZs, nicknames, roles."""
    return {
        "active_workers": [
            {"name": w["name"], "first_name": w["name"].split()[0],
             "tz": w.get("tz", "UTC"), "nicknames": w.get("nicknames", []),
             "is_owner": w["user_id"] in config.OWNER_SLACK_IDS,
             "is_manager": w["user_id"] in config.MANAGER_SLACK_IDS}
            for w in workers
        ],
    }


# ─────────────────────────────────────────────────────────────────────────
# WRITE tools — change state
# ─────────────────────────────────────────────────────────────────────────

def log_retroactive_eod(name: str, time_hhmm: str, date_iso: str | None,
                        workers: list[dict]) -> dict:
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    when = parse_hhmm_to_utc(time_hhmm, target.get("tz", "UTC"), date_iso)
    if not when:
        return {"error": f"Couldn't parse time '{time_hhmm}'. Try HH:MM or '7:44am'."}
    try:
        _write_activity_row(target, "eod",
                            f"retroactive EOD: worker reported {time_hhmm}", when)
        return {"ok": True, "logged": "eod", "worker": target["name"],
                "local_time": time_hhmm, "utc_timestamp": when.isoformat()}
    except Exception as e:
        return {"error": str(e)}


def log_retroactive_login(name: str, time_hhmm: str, date_iso: str | None,
                          workers: list[dict]) -> dict:
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    when = parse_hhmm_to_utc(time_hhmm, target.get("tz", "UTC"), date_iso)
    if not when:
        return {"error": f"Couldn't parse time '{time_hhmm}'."}
    try:
        _write_activity_row(target, "login",
                            f"retroactive login: worker reported start at {time_hhmm}", when)
        return {"ok": True, "logged": "login", "worker": target["name"],
                "local_time": time_hhmm, "utc_timestamp": when.isoformat()}
    except Exception as e:
        return {"error": str(e)}


def log_retroactive_break(name: str, start_hhmm: str, end_hhmm: str,
                          date_iso: str | None, workers: list[dict]) -> dict:
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    start = parse_hhmm_to_utc(start_hhmm, target.get("tz", "UTC"), date_iso)
    end = parse_hhmm_to_utc(end_hhmm, target.get("tz", "UTC"), date_iso)
    if not start or not end:
        return {"error": "Couldn't parse times."}
    if end <= start:
        return {"error": "Break end must be after break start."}
    try:
        _write_activity_row(target, "break_start",
                            f"retroactive break start at {start_hhmm}", start)
        dur_min = (end - start).total_seconds() / 60
        _write_activity_row(target, "break_end",
                            f"retroactive break end at {end_hhmm} ({dur_min:.0f}min)", end)
        return {"ok": True, "worker": target["name"], "break_minutes": int(dur_min)}
    except Exception as e:
        return {"error": str(e)}


def stop_checkin_prompts(name: str, reason: str, workers: list[dict]) -> dict:
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    now_utc = datetime.now(timezone.utc)
    try:
        _write_activity_row(target, "eod",
                            f"check-in prompts stopped: {reason or 'worker indicated done'}",
                            now_utc)
        return {"ok": True, "worker": target["name"],
                "action": "logged as EOD at now; check-in prompts will stop"}
    except Exception as e:
        return {"error": str(e)}


def log_time_off(name: str, type_: str, start_date: str, end_date: str,
                 days: int, notes: str, logged_by: str,
                 workers: list[dict]) -> dict:
    target = resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    try:
        row = [
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            target["name"], target["user_id"],
            (type_ or "vacation").lower(),
            start_date, end_date, int(days or 1),
            "logged", logged_by, notes or "",
        ]
        sheets.append_time_off(row)
        return {"ok": True, "logged": {"worker": target["name"], "type": type_,
                                        "start": start_date, "end": end_date, "days": days}}
    except Exception as e:
        return {"error": f"failed to log: {e}"}


def queue_message_for_worker(to_name: str, message: str, deferred: bool,
                             estimated_time: str, from_name: str, from_user_id: str,
                             workers: list[dict]) -> dict:
    """Queue or deliver a message to a worker. If deferred=False AND the
    worker is currently online, the caller should deliver immediately (this
    function just queues to the Relay Queue for tracking)."""
    target = resolve_worker(to_name, workers)
    if not target:
        return {"error": f"No worker matching '{to_name}'."}
    try:
        relay_id = "r-" + uuid.uuid4().hex[:8]
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = [
            relay_id, now_iso, from_name, from_user_id,
            target["name"], target["user_id"],
            message, estimated_time or "",
            "pending", "", "", "", "",
        ]
        sheets.append_relay(row)
        return {"ok": True, "relay_id": relay_id, "to_name": target["name"],
                "to_user_id": target["user_id"], "deferred": deferred,
                "message": message}
    except Exception as e:
        return {"error": f"queue write failed: {e}"}


def save_knowledge(worker_name: str, kind: str, name: str, url: str,
                   description: str, steps: str, workers: list[dict]) -> dict:
    target = resolve_worker(worker_name, workers)
    if not target:
        return {"error": f"No worker matching '{worker_name}'."}
    try:
        sheets.upsert_knowledge({
            "Worker": target["name"], "Slack User ID": target["user_id"],
            "Kind": (kind or "tool").lower(), "Name": name,
            "URL": url or "", "Description": description or "",
            "Steps / Notes": steps or "",
        })
        return {"ok": True, "saved": name, "for": target["name"]}
    except Exception as e:
        return {"error": f"KB write failed: {e}"}


def send_eod_digest_now() -> dict:
    """Trigger the daily digest immediately."""
    try:
        from . import report
        result = report.send_daily_digest()
        return {"ok": result.get("slack", False), "workers": result.get("workers", 0),
                "errors": result.get("errors", [])}
    except Exception as e:
        return {"error": f"digest failed: {e}"}
