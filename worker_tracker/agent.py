"""Tool-calling agent loop. Replaces the regex/intent dispatcher.

When an admin (or worker) sends a non-worker-action message, this agent:
  1. Loads the speaker's last N conversation turns from the Conversations tab.
  2. Calls Gemini 2.5 Pro with a system prompt + the 14 tools below + history.
  3. The model decides which tool(s) to call.
  4. We execute, feed the results back, the model loops or finalizes.
  5. Final natural-language reply is sent + the turn is persisted.

The model has memory ("his", "her", "yesterday's question", "the same guy")
because every call sees the prior turns. Pronouns resolve naturally.

Worker actions (login / break / EOD / hours / discrepancy) STILL run as
fast regex paths in bot.py — they're sub-100ms and unambiguous. The agent
is for everything else.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
from typing import Any

from google import genai
from google.genai import types

from . import config, sheets

log = logging.getLogger(__name__)

# Per-DM in-memory cache so we don't re-read the sheet on every turn.
# Keyed by speaker Slack user ID -> list of {"role": "user"|"assistant", "text": str, "ts": iso}
_CONV_CACHE: dict[str, list[dict]] = {}
_CONV_HISTORY_LIMIT = 14


# ─────────────────────────────────────────────────────────────────────────
# TOOL IMPLEMENTATIONS — Python side
# ─────────────────────────────────────────────────────────────────────────

def _resolve_worker(name_query: str, workers: list[dict]) -> dict | None:
    """Fuzzy match a name against the roster. Handles typos + nicknames."""
    if not name_query:
        return None
    import difflib
    q = name_query.strip().lower()
    # exact full-name
    for w in workers:
        if w["name"].lower() == q:
            return w
    # first-name exact
    for w in workers:
        if w["name"].split()[0].lower() == q:
            return w
    # nickname
    for w in workers:
        if q in (w.get("nicknames") or []):
            return w
    # substring
    for w in workers:
        if q in w["name"].lower() or q in w["name"].split()[0].lower():
            return w
    # fuzzy
    scored = []
    for w in workers:
        candidates = [w["name"].lower(), w["name"].split()[0].lower()]
        candidates.extend(w.get("nicknames") or [])
        best = max((difflib.SequenceMatcher(None, q, c).ratio() for c in candidates), default=0.0)
        scored.append((best, w))
    scored.sort(key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] >= 0.72:
        return scored[0][1]
    return None


def _parse_when(when: str, tz_name: str = "UTC") -> tuple[str, str]:
    """Parse a 'when' string into (start_iso, end_iso) inclusive date range.

    Accepts: 'today' / 'yesterday' / 'last_week' / 'this_week' / 'this_month'
             / 'YYYY-MM-DD' / 'YYYY-MM-DD to YYYY-MM-DD' / 'last_7_days'
             / month names like 'may' (current/last year auto)
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
    if w in ("last_week", "last week", "past_week", "past week", "last_7_days", "last 7 days"):
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

    # Try "YYYY-MM-DD to YYYY-MM-DD"
    if "to" in w or " - " in w or " through " in w:
        parts = re.split(r"\s+(?:to|through|-)\s+", w)
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

    # Default to today
    return today_local.isoformat(), today_local.isoformat()


def tool_get_worker_status(name: str, workers: list[dict]) -> dict:
    """Current state — working/on_break/logged_off/not_started — plus hours-so-far."""
    target = _resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}' on the roster."}
    # Use the snapshot logic from bot.py — minimal re-impl here to avoid circular import.
    try:
        recent = sheets.activity_since(2, slack_user_id=target["user_id"])
    except Exception as e:
        return {"error": f"Activity read failed: {e}"}
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
            if login_ts and not eod_ts and (ts - login_ts).total_seconds() < 8 * 3600:
                continue  # duplicate-login fold
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
            msg = (r.get("Message") or "").strip()
            if msg:
                last_msg = msg
                last_msg_ts = ts

    if not login_ts:
        return {"name": target["name"], "state": "not_started",
                "hours_today": 0.0, "message": "Hasn't clocked in for the current/most recent shift."}

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
        "last_checkin": last_msg[:300] if last_msg else None,
        "last_checkin_minutes_ago": int((now - last_msg_ts).total_seconds() / 60) if last_msg_ts else None,
        "current_break_minutes": int((now - break_start_ts).total_seconds() / 60) if break_start_ts else None,
        "tz": target["tz"],
    }


def tool_get_worker_activity(name: str, when: str, workers: list[dict]) -> dict:
    """Chronological events for a worker over a date range."""
    target = _resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    start_iso, end_iso = _parse_when(when, target.get("tz", "UTC"))

    # Pull a wide window from activity_since (cached at sheets layer)
    today_local = date.fromisoformat(end_iso)
    days_back = max(1, (today_local - date.fromisoformat(start_iso)).days + 1)
    days_back = min(days_back + 1, 60)  # cap to keep payload small
    try:
        rows = sheets.activity_since(days_back, slack_user_id=target["user_id"])
    except Exception as e:
        return {"error": f"Activity read failed: {e}"}

    rows = [r for r in rows if start_iso <= str(r.get("Local Date", "")) <= end_iso]
    rows.sort(key=lambda r: r.get("Timestamp UTC", ""))

    events = []
    for r in rows:
        t = r.get("Type", "")
        if t.startswith("sam_") or t in ("prompt_sent",):
            continue
        msg = (r.get("Message") or "").strip()
        events.append({
            "date": r.get("Local Date", ""),
            "time": (r.get("Local Time") or "")[:5],
            "type": t,
            "message": msg[:400],
        })
    return {
        "name": target["name"],
        "date_range": f"{start_iso} to {end_iso}",
        "events": events[:120],
        "event_count": len(events),
    }


def tool_get_worker_hours(name: str, period: str, workers: list[dict]) -> dict:
    """Pay-period hours for a worker. period: 'current' / 'previous' / 'this_month' / etc."""
    target = _resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    try:
        from . import payroll
        if period in ("", "current", "this_period"):
            start, end = payroll.current_open_period()
        elif period in ("previous", "last_period"):
            cs, ce = payroll.current_open_period()
            # The previous period ends the day before the current one starts
            end = cs - timedelta(days=1)
            # Semimonthly: previous is either 1-15 or 16-EOM of prior month
            if end.day >= 16:
                start = end.replace(day=16)
            else:
                start = end.replace(day=1)
        else:
            start, end = payroll.current_open_period()
        totals = payroll.worker_period_totals(target, start, end)
        # Also add today's live hours via the snapshot
        live = tool_get_worker_status(name, workers)
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
        return {"error": f"Hours calc failed: {e}"}


def tool_get_worker_benefits(name: str, workers: list[dict]) -> dict:
    """Vacation/sick/holiday/PTO allocations + used + remaining."""
    target = _resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
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
        pass

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


def tool_get_worker_open_tasks(name: str, workers: list[dict]) -> dict:
    """Open relays + commitments for a worker."""
    target = _resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
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
        "pending_relays": [{"message": r.get("Message",""), "from": r.get("From Name",""),
                            "queued": r.get("Date Created","")} for r in pending],
        "delivered_relays_awaiting_completion": [
            {"message": r.get("Message",""), "from": r.get("From Name",""),
             "delivered": r.get("Date Delivered","")} for r in delivered
        ],
        "self_commitments": [{"text": c.get("Commitment",""), "created": c.get("Date Created",""),
                              "due": c.get("Due By","")} for c in commitments],
    }


def tool_get_worker_knowledge(name: str, workers: list[dict]) -> dict:
    """Tools/processes/people/sheets/jobs Sam has logged about this worker."""
    target = _resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    try:
        kb = sheets.list_worker_knowledge(target["user_id"])
    except Exception as e:
        return {"error": f"KB read failed: {e}"}
    return {
        "name": target["name"],
        "entry_count": len(kb),
        "entries": [{
            "kind": k.get("Kind",""), "name": k.get("Name",""),
            "url": k.get("URL",""), "description": k.get("Description",""),
            "steps": k.get("Steps / Notes",""),
            "first_seen": k.get("First Mentioned",""),
            "times_referenced": k.get("Times Referenced",""),
        } for k in kb],
    }


def tool_get_team_status(workers: list[dict]) -> dict:
    """Current state for every active worker (excludes owners)."""
    out = []
    for w in workers:
        if w["user_id"] in config.OWNER_SLACK_IDS:
            continue
        try:
            s = tool_get_worker_status(w["name"].split()[0], workers)
            if "error" not in s:
                out.append(s)
        except Exception:
            pass
    return {"workers": out, "count": len(out)}


def tool_log_time_off(name: str, type_: str, start_date: str, end_date: str,
                       days: int, notes: str, logged_by: str,
                       workers: list[dict]) -> dict:
    target = _resolve_worker(name, workers)
    if not target:
        return {"error": f"No worker matching '{name}'."}
    try:
        from datetime import datetime as _dt
        row = [
            _dt.now(timezone.utc).isoformat(timespec="seconds"),
            target["name"], target["user_id"],
            (type_ or "vacation").lower(),
            start_date, end_date, int(days or 1),
            "logged", logged_by, notes or "",
        ]
        sheets.append_time_off(row)
        return {"ok": True, "logged": {"worker": target["name"], "type": type_,
                                          "start": start_date, "end": end_date, "days": days}}
    except Exception as e:
        return {"error": f"Failed to log: {e}"}


def tool_queue_message(to_name: str, message: str, deferred: bool,
                        estimated_time: str, from_name: str, from_user_id: str,
                        workers: list[dict]) -> dict:
    target = _resolve_worker(to_name, workers)
    if not target:
        return {"error": f"No worker matching '{to_name}'."}
    try:
        import uuid
        from datetime import datetime as _dt
        relay_id = "r-" + uuid.uuid4().hex[:8]
        now_iso = _dt.now(timezone.utc).isoformat(timespec="seconds")
        row = [
            relay_id, now_iso, from_name, from_user_id,
            target["name"], target["user_id"],
            message, estimated_time or "",
            "pending", "", "", "", "",
        ]
        sheets.append_relay(row)
        return {"ok": True, "relay_id": relay_id, "to": target["name"], "deferred": deferred}
    except Exception as e:
        return {"error": f"Queue write failed: {e}"}


def tool_save_knowledge(worker_name: str, kind: str, name: str, url: str,
                         description: str, steps: str, workers: list[dict]) -> dict:
    target = _resolve_worker(worker_name, workers)
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


def tool_send_eod_digest_now() -> dict:
    try:
        from . import report
        result = report.send_daily_digest()
        return {"ok": result.get("slack", False), "workers": result.get("workers", 0),
                 "errors": result.get("errors", [])}
    except Exception as e:
        return {"error": f"Digest failed: {e}"}


def tool_get_learned_today(workers: list[dict]) -> dict:
    """What Sam captured today from the team — KB additions + substantive check-ins + new commitments."""
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
                new_kb.append({"worker": w["name"], "kind": k.get("Kind",""),
                               "name": k.get("Name",""), "description": k.get("Description","")})
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
        msg = (r.get("Message") or "").strip()
        if len(msg) < 30:
            continue
        by_worker.setdefault((r.get("Worker") or "").strip(), []).append(msg[:300])
    return {
        "date": today_local,
        "new_knowledge": new_kb,
        "substantive_checkins_by_worker": by_worker,
    }


def tool_get_roster_summary(workers: list[dict]) -> dict:
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
# FUNCTION DECLARATIONS for Gemini
# ─────────────────────────────────────────────────────────────────────────

def _build_tools() -> list[types.Tool]:
    decls = [
        types.FunctionDeclaration(
            name="get_worker_status",
            description=(
                "Get a worker's CURRENT state right now — whether they're working, on break, "
                "logged off, or haven't started. Returns login time, hours so far in the open "
                "shift, last check-in message, current break duration if on break. Use this for "
                "questions like 'is X working', 'how is X doing', 'what's X up to', 'where's X', "
                "'check on X'."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"name": types.Schema(type=types.Type.STRING,
                    description="Worker name or nickname — typos and partial names OK.")},
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_worker_activity",
            description=(
                "Get the chronological list of a worker's actual events (check-ins, breaks, "
                "logins, EODs) for a date or date range. Use this for 'what did X do today', "
                "'what did X work on yesterday', 'show me X's trail on May 28', 'recap X's week'. "
                "Returns every meaningful event with timestamps and messages."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(type=types.Type.STRING),
                    "when": types.Schema(type=types.Type.STRING,
                        description="Date range. Accepts: 'today', 'yesterday', 'last_week', 'this_week', 'this_month', 'last_month', 'YYYY-MM-DD', or 'YYYY-MM-DD to YYYY-MM-DD'."),
                },
                required=["name", "when"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_worker_hours",
            description=(
                "Pay-period hours for a worker including the current day's open session. Use "
                "for 'how many hours has X worked', 'X's hours this period', 'X's hours last period'."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(type=types.Type.STRING),
                    "period": types.Schema(type=types.Type.STRING,
                        description="'current' for the open pay period, 'previous' for the prior one."),
                },
                required=["name", "period"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_worker_benefits",
            description=(
                "Vacation / sick / holiday / PTO allocations + used + remaining for a worker, "
                "plus HMO reimbursement, calamity fund, performance bonus date, pay schedule, "
                "hourly rate. Use for 'how many vacation days does X have', 'X's benefits', "
                "'when is X's perf bonus', 'how much PTO does X have left'."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"name": types.Schema(type=types.Type.STRING)},
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_worker_open_tasks",
            description=(
                "Open tasks for a worker — pending relays (admin sent, not yet delivered), "
                "delivered relays awaiting completion, and self-made commitments. Use for "
                "'X's open tasks', 'what's on X's plate (still open)', 'X's checklist'."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"name": types.Schema(type=types.Type.STRING)},
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_worker_knowledge",
            description=(
                "Tools, sheets, processes, people, jobs Sam has logged about a worker — the "
                "Knowledge Base for that person. Use for 'what tools does X use', 'who does X "
                "coordinate with', 'what processes does X do', 'X's workflow map'."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"name": types.Schema(type=types.Type.STRING)},
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_team_status",
            description=(
                "Current state for every active worker. Use for 'team status', 'did everyone "
                "log in', 'who's working right now', 'who's on break', 'who hasn't logged in'."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="get_learned_today",
            description=(
                "What Sam has captured today across the team — new Knowledge Base entries, "
                "substantive check-ins per worker, anything noteworthy. Use for 'what did you "
                "learn today', 'what's new', 'today's takeaways from the team'."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="get_roster_summary",
            description=(
                "Full list of active workers with their names, nicknames, timezones, and "
                "owner/manager roles. Useful when the speaker references someone ambiguously "
                "and you need to know who's on the team."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="log_time_off",
            description=(
                "Log time off for a worker. Use when an admin says 'log vacation for hannah dec "
                "1-5' / 'sick day for rey today' / 'pto for ger next monday'."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(type=types.Type.STRING),
                    "type": types.Schema(type=types.Type.STRING,
                        description="One of: vacation, sick, holiday, pto, personal, unpaid"),
                    "start_date": types.Schema(type=types.Type.STRING, description="YYYY-MM-DD"),
                    "end_date": types.Schema(type=types.Type.STRING, description="YYYY-MM-DD"),
                    "days": types.Schema(type=types.Type.INTEGER),
                    "notes": types.Schema(type=types.Type.STRING),
                },
                required=["name", "type", "start_date", "end_date", "days"],
            ),
        ),
        types.FunctionDeclaration(
            name="queue_message_for_worker",
            description=(
                "Queue or deliver a message to a worker. If deferred=true, hold until they next "
                "log in. If false, deliver immediately (or queue if they're offline). Use for "
                "'tell X to do Y', 'send this to X', 'when X logs in tell her Y', 'give X this link'."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "to_name": types.Schema(type=types.Type.STRING),
                    "message": types.Schema(type=types.Type.STRING,
                        description="The actual message to deliver to the worker, written naturally."),
                    "deferred": types.Schema(type=types.Type.BOOLEAN,
                        description="True if 'when X logs in' / 'next time X is online'. False for immediate."),
                    "estimated_time": types.Schema(type=types.Type.STRING,
                        description="Optional time estimate like '15 min' if mentioned."),
                },
                required=["to_name", "message", "deferred"],
            ),
        ),
        types.FunctionDeclaration(
            name="save_knowledge",
            description=(
                "Save a Knowledge Base entry for a worker — a tool, sheet, person, process, "
                "etc. Use when a worker shares a sheet URL with description, or describes a "
                "new tool/process/contact."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "worker_name": types.Schema(type=types.Type.STRING),
                    "kind": types.Schema(type=types.Type.STRING,
                        description="software | tool | sheet | doc | process | workflow | platform | person | link | job | compliance"),
                    "name": types.Schema(type=types.Type.STRING),
                    "url": types.Schema(type=types.Type.STRING),
                    "description": types.Schema(type=types.Type.STRING,
                        description="1-2 sentences: what it is + what the worker uses it for."),
                    "steps": types.Schema(type=types.Type.STRING),
                },
                required=["worker_name", "kind", "name", "description"],
            ),
        ),
        types.FunctionDeclaration(
            name="send_eod_digest_now",
            description=(
                "Trigger the end-of-day digest immediately. Use when admin says 'send digest', "
                "'EOD report now', 'give me today's report'."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
    ]
    return [types.Tool(function_declarations=decls)]


_TOOL_FUNCTIONS = {
    "get_worker_status": lambda args, ctx: tool_get_worker_status(args["name"], ctx["workers"]),
    "get_worker_activity": lambda args, ctx: tool_get_worker_activity(args["name"], args.get("when", "today"), ctx["workers"]),
    "get_worker_hours": lambda args, ctx: tool_get_worker_hours(args["name"], args.get("period", "current"), ctx["workers"]),
    "get_worker_benefits": lambda args, ctx: tool_get_worker_benefits(args["name"], ctx["workers"]),
    "get_worker_open_tasks": lambda args, ctx: tool_get_worker_open_tasks(args["name"], ctx["workers"]),
    "get_worker_knowledge": lambda args, ctx: tool_get_worker_knowledge(args["name"], ctx["workers"]),
    "get_team_status": lambda args, ctx: tool_get_team_status(ctx["workers"]),
    "get_learned_today": lambda args, ctx: tool_get_learned_today(ctx["workers"]),
    "get_roster_summary": lambda args, ctx: tool_get_roster_summary(ctx["workers"]),
    "log_time_off": lambda args, ctx: tool_log_time_off(
        args["name"], args.get("type", "vacation"),
        args["start_date"], args["end_date"], args.get("days", 1),
        args.get("notes", ""), ctx["speaker_name"], ctx["workers"]),
    "queue_message_for_worker": lambda args, ctx: tool_queue_message(
        args["to_name"], args["message"], args.get("deferred", False),
        args.get("estimated_time", ""), ctx["speaker_name"], ctx["speaker_id"],
        ctx["workers"]),
    "save_knowledge": lambda args, ctx: tool_save_knowledge(
        args["worker_name"], args.get("kind", "tool"), args["name"],
        args.get("url", ""), args["description"], args.get("steps", ""),
        ctx["workers"]),
    "send_eod_digest_now": lambda args, ctx: tool_send_eod_digest_now(),
}


# ─────────────────────────────────────────────────────────────────────────
# CONVERSATION MEMORY
# ─────────────────────────────────────────────────────────────────────────

def remember_turn(speaker_id: str, role: str, text: str) -> None:
    """Append a turn to the in-memory conversation cache."""
    hist = _CONV_CACHE.setdefault(speaker_id, [])
    hist.append({
        "role": role, "text": text[:2000],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if len(hist) > _CONV_HISTORY_LIMIT:
        _CONV_CACHE[speaker_id] = hist[-_CONV_HISTORY_LIMIT:]


def get_history(speaker_id: str) -> list[dict]:
    return list(_CONV_CACHE.get(speaker_id, []))


# ─────────────────────────────────────────────────────────────────────────
# AGENT LOOP
# ─────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are Sam, the AI ops assistant for Hey Girl Tea. You time-track
a small remote team (workers in the Philippines, manager Jan in Vancouver,
admin Hannah). You're a thoughtful coworker — warm but tight, lowercase,
1-3 sentences per reply unless answering a question that genuinely needs
length.

YOU HAVE TOOLS. Use them aggressively to get real data instead of guessing
or saying "i don't have access". Almost every question about the team has
a tool answer. Look it up before responding.

PRONOUN RESOLUTION: the speaker's prior messages are in the conversation
history. When they say "he", "her", "they", "the same guy", "yesterday's
question" — figure out who/what they meant from context. Don't ask them to
repeat.

CALLING MULTIPLE TOOLS: if a question needs multiple lookups (e.g. "how is
Rey doing and is he still stuck on the Walmart case?"), call multiple
tools, then synthesize.

WHEN A QUESTION INVOLVES A DATE OR DATE RANGE: use get_worker_activity with
the right `when` value, not get_worker_open_tasks (which only shows the
current queue).

EXAMPLES of right tool choice:
- "is rey working?" → get_worker_status("rey")
- "what did hannah do today?" → get_worker_activity("hannah", "today")
- "what was rey's specific tasks yesterday?" → get_worker_activity("rey", "yesterday")
- "tasks for rey on may 28" → get_worker_activity("rey", "2026-05-28")
- "how many vacation days does rey have?" → get_worker_benefits("rey")
- "how many hours has hannah worked this period?" → get_worker_hours("hannah", "current")
- "did everyone log in today?" → get_team_status()
- "what did you learn from the team today?" → get_learned_today()
- "tell rey to upload the new thumbs" → queue_message_for_worker(rey, "...", deferred=false)
- "when ger logs in remind her about wise" → queue_message_for_worker(ger, "...", deferred=true)
- "log vacation for hannah dec 1-5" → log_time_off(...)

VOICE: like a real coworker. Acknowledge the substance of what they said.
Don't say "i can help with that". Just answer. Match their energy: short
question = short answer; substantive question = full answer with the
actual data.

NEVER invent data. If a tool returns an error or empty result, say so
honestly — "rey has no recorded check-ins for may 28" beats hallucinating.

If the speaker mentions a tool/sheet/process/person/url that's clearly a
NEW workflow detail (not already logged), call save_knowledge to capture
it for that worker (use the speaker's name unless they were clearly
referring to another worker).
"""


def agent_reply(
    text: str,
    speaker_user_id: str,
    speaker_name: str,
    is_owner: bool,
    is_manager: bool,
    workers: list[dict],
    max_iterations: int = 6,
) -> str | None:
    """Run the tool-calling agent loop. Returns the final reply text, or
    None if the agent failed (caller can fall back to a canned message)."""
    if not config.GOOGLE_API_KEY or not text.strip():
        return None

    role = "OWNER" if is_owner else ("MANAGER" if is_manager else "WORKER")
    role_block = (f"\nSPEAKER: {speaker_name} ({role}). "
                   f"User ID: {speaker_user_id}.\n")
    history = get_history(speaker_user_id)

    # Build conversation as Gemini Content list
    contents: list[types.Content] = []
    for turn in history:
        contents.append(types.Content(
            role=("model" if turn["role"] == "assistant" else "user"),
            parts=[types.Part(text=turn["text"])],
        ))
    contents.append(types.Content(role="user", parts=[types.Part(text=text)]))

    tools = _build_tools()
    ctx = {
        "workers": workers, "speaker_id": speaker_user_id,
        "speaker_name": speaker_name,
    }

    try:
        client = genai.Client(api_key=config.GOOGLE_API_KEY)
    except Exception as e:
        log.warning("agent: client init failed: %s", e)
        return None

    import os
    # Default to Flash 2.5 (verified working, broadly available). Pro is
    # available via AGENT_MODEL_OVERRIDE=gemini-2.5-pro env var if the
    # project has it enabled.
    agent_model = os.environ.get("AGENT_MODEL_OVERRIDE") or "gemini-2.5-flash"

    for iteration in range(max_iterations):
        try:
            resp = client.models.generate_content(
                model=agent_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT + role_block,
                    tools=tools,
                    temperature=0.4,
                    max_output_tokens=4096,
                ),
            )
        except Exception as e:
            log.warning("agent: gen call failed on iter %d (%s): %s",
                        iteration, agent_model, e)
            # Try the OTHER model on the first iteration if we haven't yet
            if iteration == 0:
                alt = "gemini-2.5-pro" if "flash" in agent_model.lower() else "gemini-2.5-flash"
                if alt != agent_model:
                    log.info("agent: retrying with %s", alt)
                    agent_model = alt
                    continue
            return f"having trouble reaching gemini right now ({type(e).__name__}). try again in a sec?"

        if not resp.candidates:
            return None

        cand = resp.candidates[0]
        parts = getattr(cand.content, "parts", None) or []

        # Look for function calls
        fn_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

        if fn_calls:
            # Append the model's content (with function calls) to history
            contents.append(cand.content)
            # Execute each call and append the function response
            for fc in fn_calls:
                fn_name = fc.name
                fn_args = dict(fc.args) if fc.args else {}
                log.info("agent tool call: %s(%s)", fn_name, json.dumps(fn_args)[:200])
                func = _TOOL_FUNCTIONS.get(fn_name)
                if not func:
                    result = {"error": f"Unknown tool: {fn_name}"}
                else:
                    try:
                        result = func(fn_args, ctx)
                    except Exception as e:
                        log.exception("agent tool %s threw", fn_name)
                        result = {"error": f"{type(e).__name__}: {e}"}
                contents.append(types.Content(
                    role="function",
                    parts=[types.Part(function_response=types.FunctionResponse(
                        name=fn_name, response={"result": result},
                    ))],
                ))
            continue  # Loop for next model turn

        # No function calls — final text reply
        reply_text = (resp.text or "").strip()
        if not reply_text:
            return None
        # Strip wrapping quotes if any
        if (reply_text.startswith('"') and reply_text.endswith('"')) or \
           (reply_text.startswith("'") and reply_text.endswith("'")):
            reply_text = reply_text[1:-1]
        # Save the turn to history
        remember_turn(speaker_user_id, "user", text)
        remember_turn(speaker_user_id, "assistant", reply_text)
        return reply_text

    # Max iterations exhausted
    log.warning("agent: max iterations exhausted for %s", speaker_user_id)
    return "looking into that took longer than expected — try rephrasing?"
