"""Full team memory builder — turns everything Sam has tracked into a single
context block for Gemini.

For admin chats, this means: Sam sees current state, past 7 days of summarized
activity, durable worker profiles, knowledge base, time-off log, daily
assignments, and the conversation history with this admin. So when an admin
asks 'what did Rey do yesterday' or 'why is Hannah always stuck on Mondays'
or follows up on something Sam said 2 messages ago, Sam can actually answer.

Cached 30 seconds (in-memory) per call to avoid hammering Google Sheets when
an admin sends a burst of messages.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import config, sheets

log = logging.getLogger(__name__)

# (cache_key, build_time) -> rendered memory text
_MEMORY_CACHE: dict[tuple, tuple[float, str]] = {}
_CACHE_TTL_SECONDS = 30


def _cached(key: tuple, ttl: int = _CACHE_TTL_SECONDS) -> Optional[str]:
    entry = _MEMORY_CACHE.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None


def _put(key: tuple, value: str) -> None:
    _MEMORY_CACHE[(key)] = (time.time(), value)


# --- Per-section builders ---

def _today_state_section(workers: list[dict], today_snapshot_fn) -> str:
    """Compact 'where is everyone right now' block."""
    if not workers:
        return "(no active workers)"
    lines = []
    now_mgr = datetime.now(ZoneInfo(config.MANAGER_TZ)).strftime("%Y-%m-%d %H:%M")
    lines.append(f"TODAY'S TEAM STATE ({now_mgr} {config.MANAGER_TZ}):")
    for w in workers:
        if w["user_id"] in config.OWNER_SLACK_IDS:
            continue
        try:
            snap = today_snapshot_fn(w)
        except Exception:
            continue
        tz = w.get("tz", "UTC")
        try:
            tzi = ZoneInfo(tz)
        except Exception:
            tzi = ZoneInfo("UTC")
        login_str = snap["login_ts"].astimezone(tzi).strftime("%H:%M") if snap["login_ts"] else "—"
        bits = [f"- *{w['name']}* ({tz})"]
        bits.append(f"state={snap['state']}")
        bits.append(f"hours_so_far_today={snap['hours_so_far_today']:.2f}h")
        if snap["login_ts"]:
            bits.append(f"login={login_str}")
        if snap["state"] == "on_break" and snap["break_start_ts"]:
            mins = int((datetime.now(timezone.utc) - snap["break_start_ts"]).total_seconds() / 60)
            bits.append(f"break_mins={mins}")
        if snap["last_checkin_msg"]:
            mins = int((datetime.now(timezone.utc) - snap["last_checkin_ts"]).total_seconds() / 60) if snap["last_checkin_ts"] else 0
            bits.append(f"last_checkin_{mins}m_ago=\"{snap['last_checkin_msg'][:250]}\"")
        lines.append("  " + ", ".join(bits))
    return "\n".join(lines)


def _past_activity_section(days: int = 7) -> str:
    """Past N days of Daily Summary rows — one row/worker/day with AI summary."""
    try:
        ws = sheets.open_tracker().worksheet(config.SUMMARY_TAB)
        rows = ws.get_all_records()
    except Exception:
        return ""
    if not rows:
        return ""

    today = datetime.now(ZoneInfo(config.MANAGER_TZ)).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    recent = [r for r in rows if str(r.get("Date", "")) >= cutoff]
    if not recent:
        return ""

    # Group by date, then by worker (newest first)
    recent.sort(key=lambda r: (str(r.get("Date", "")), str(r.get("Worker", ""))), reverse=True)
    lines = [f"PAST {days} DAYS OF DAILY SUMMARIES (most recent first — one row per worker per day):"]
    for r in recent[:60]:  # cap volume
        date = r.get("Date", "")
        worker = r.get("Worker", "")
        hours = r.get("Active Hours", "")
        status = r.get("Status", "")
        capacity = r.get("Capacity Signal", "")
        day_sum = (r.get("Day Summary") or "")[:200]
        autos = (r.get("Automation Ideas") or "")[:150]
        flags = (r.get("Manual Red Flags") or "")[:150]
        bits = [f"- [{date}] *{worker}*: {hours}h"]
        if status:
            bits.append(f"status={status}")
        if capacity:
            bits.append(f"capacity={capacity}")
        if day_sum:
            bits.append(f"\n    summary: {day_sum}")
        if autos:
            bits.append(f"\n    automation: {autos}")
        if flags:
            bits.append(f"\n    manual flags: {flags}")
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _profiles_section(workers: list[dict]) -> str:
    """Durable per-worker profile rows (built by the weekly synthesizer)."""
    profiles_block = []
    try:
        all_profiles = sheets.all_profiles()
    except Exception:
        return ""
    by_uid = {str(p.get("Slack User ID", "")).strip(): p for p in all_profiles}
    for w in workers:
        p = by_uid.get(w["user_id"])
        if not p:
            continue
        bits = [f"- *{w['name']}*"]
        for label, key in [
            ("role", "Role / What They Do"),
            ("recurring", "Recurring Tasks"),
            ("strengths", "Known Strengths"),
            ("blockers", "Known Blockers / Skill Gaps"),
            ("tools", "Tools They Currently Use"),
            ("automation_open", "Automation Opportunities (Open)"),
            ("automation_shipped", "Automation Opportunities (Shipped)"),
            ("patterns", "Productivity Patterns"),
            ("coaching", "Coaching Notes for Manager"),
        ]:
            v = (p.get(key) or "").strip()
            if v:
                bits.append(f"\n    {label}: {v[:250]}")
        if len(bits) > 1:
            profiles_block.append("".join(bits))
    if not profiles_block:
        return ""
    return "WORKER PROFILES (durable per-worker info, updated weekly):\n" + "\n".join(profiles_block)


def _knowledge_section(workers: list[dict]) -> str:
    """Tools and processes Sam has learned about each worker."""
    try:
        ws = sheets.open_tracker().worksheet(config.KNOWLEDGE_TAB)
        rows = ws.get_all_records()
    except Exception:
        return ""
    if not rows:
        return ""
    by_worker: dict[str, list[str]] = {}
    for r in rows:
        name = (r.get("Worker") or "").strip()
        if not name:
            continue
        kind = (r.get("Kind") or "tool").strip()
        rname = (r.get("Name") or "").strip()
        url = (r.get("URL") or "").strip()
        desc = (r.get("Description") or "").strip()
        bit = f"{kind}: {rname}"
        if url:
            bit += f" ({url})"
        if desc:
            bit += f" — {desc[:150]}"
        by_worker.setdefault(name, []).append(bit)
    if not by_worker:
        return ""
    lines = ["KNOWN TOOLS & PROCESSES PER WORKER:"]
    for w in workers:
        items = by_worker.get(w["name"], [])
        if items:
            lines.append(f"- *{w['name']}*:")
            for item in items[:10]:
                lines.append(f"    • {item}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _time_off_section() -> str:
    """Recent + upcoming time off."""
    try:
        ws = sheets.open_tracker().worksheet(config.TIME_OFF_TAB)
        rows = ws.get_all_records()
    except Exception:
        return ""
    if not rows:
        return ""
    today = datetime.now(ZoneInfo(config.MANAGER_TZ)).date()
    window_start = (today - timedelta(days=14)).isoformat()
    window_end = (today + timedelta(days=30)).isoformat()
    relevant = []
    for r in rows:
        start = str(r.get("Start Date", ""))
        if start and window_start <= start <= window_end:
            relevant.append(r)
    if not relevant:
        return ""
    relevant.sort(key=lambda r: str(r.get("Start Date", "")))
    lines = ["TIME OFF (past 14 days + upcoming 30 days):"]
    for r in relevant[:30]:
        lines.append(f"- {r.get('Worker')} — {r.get('Type')} {r.get('Start Date')}"
                     + (f" → {r.get('End Date')}" if r.get('End Date') and r.get('End Date') != r.get('Start Date') else "")
                     + f" ({r.get('Days')} days, {r.get('Status')})")
    return "\n".join(lines)


def _today_assignments_section() -> str:
    """Today's daily assignments from Jan's planning DM."""
    try:
        rows = sheets.activity_rows()
    except Exception:
        return ""
    asgs = [r for r in rows if r.get("Type") == "daily_assignment"]
    if not asgs:
        return ""
    # Latest assignment per worker
    by_worker: dict[str, dict] = {}
    asgs.sort(key=lambda r: r.get("Timestamp UTC", ""))
    for r in asgs:
        by_worker[r.get("Worker", "")] = r
    if not by_worker:
        return ""
    lines = ["LATEST DAILY ASSIGNMENT PER WORKER (from Jan's evening planning DM):"]
    for name, r in by_worker.items():
        lines.append(f"- *{name}*: \"{(r.get('Message') or '')[:200]}\" (assigned {r.get('Local Date')})")
    return "\n".join(lines)


def _admin_convo_history(admin_user_id: str, limit: int = 20) -> str:
    """Last N messages between this admin and Sam — both directions. So Sam
    remembers what he just said and what the admin just asked."""
    try:
        rows = sheets.activity_rows()
    except Exception:
        return ""
    convo = [r for r in rows if str(r.get("Slack User ID", "")).strip() == admin_user_id]
    convo.sort(key=lambda r: r.get("Timestamp UTC", ""))
    convo = convo[-limit:]
    if not convo:
        return ""
    lines = [f"RECENT CONVERSATION WITH THIS ADMIN (last {len(convo)} events, oldest first):"]
    for r in convo:
        t = r.get("Type", "")
        msg = (r.get("Message") or "").strip()
        when = f"{r.get('Local Date')} {r.get('Local Time')}"
        if t.startswith("sam_"):
            speaker = "SAM"
        elif t in ("login", "checkin", "eod", "help_request", "break_start", "break_end",
                   "hours_discrepancy"):
            speaker = "ADMIN"
        else:
            speaker = t.upper()
        if msg:
            lines.append(f"  [{when}] {speaker}: {msg[:300]}")
    return "\n".join(lines)


# --- Master builder ---

def build_admin_memory(admin_user_id: str, workers: list[dict],
                       today_snapshot_fn) -> str:
    """Build a rich memory block for an admin chat. Cached 30 seconds per admin."""
    cache_key = ("admin_memory", admin_user_id)
    cached = _cached(cache_key)
    if cached:
        return cached

    sections = []
    try:
        s = _today_state_section(workers, today_snapshot_fn)
        if s:
            sections.append(s)
    except Exception:
        log.exception("today state section failed")
    try:
        s = _today_assignments_section()
        if s:
            sections.append(s)
    except Exception:
        log.exception("assignments section failed")
    try:
        s = _past_activity_section(days=7)
        if s:
            sections.append(s)
    except Exception:
        log.exception("past activity section failed")
    try:
        s = _profiles_section(workers)
        if s:
            sections.append(s)
    except Exception:
        log.exception("profiles section failed")
    try:
        s = _knowledge_section(workers)
        if s:
            sections.append(s)
    except Exception:
        log.exception("knowledge section failed")
    try:
        s = _time_off_section()
        if s:
            sections.append(s)
    except Exception:
        log.exception("time off section failed")
    try:
        s = _admin_convo_history(admin_user_id)
        if s:
            sections.append(s)
    except Exception:
        log.exception("convo history section failed")

    result = "\n\n".join(sections) if sections else "(no team data available yet)"
    _put(cache_key, result)
    return result
