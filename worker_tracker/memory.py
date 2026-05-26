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

# (cache_key, build_time) -> rendered memory text.
# Bumped 30 → 120s. A burst of admin DMs in the same conversation now all hit
# the same cached memory blob — building it requires reading 6+ tabs, so the
# difference is dramatic. Caller already passes message_text into the cache
# key, so a different worker mention reliably misses + rebuilds.
_MEMORY_CACHE: dict[tuple, tuple[float, str]] = {}
_CACHE_TTL_SECONDS = 120


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


# --- Deep dive on one worker ---

def _worker_activity_history(slack_user_id: str, days: int | None = None) -> str:
    """Every raw event for one worker. By default, ALL events (entire history).
    Pass `days` to limit to last N days if needed."""
    try:
        if days is None:
            # Pull ALL events for this worker, ever
            ws = sheets.open_tracker().worksheet(config.ACTIVITY_TAB)
            all_rows = ws.get_all_records()
            rows = [r for r in all_rows if str(r.get("Slack User ID", "")).strip() == slack_user_id]
        else:
            rows = sheets.activity_since(days, slack_user_id=slack_user_id)
    except Exception:
        return ""
    if not rows:
        return ""
    rows.sort(key=lambda r: r.get("Timestamp UTC", ""))
    scope = f"all-time, {len(rows)} events" if days is None else f"past {days} days, {len(rows)} events"
    lines = [f"FULL ACTIVITY LOG ({scope}, chronological):"]
    # Token budget: cap at 800 most recent so the context stays under ~30K tokens.
    # If they've got more events than that, keep the newest.
    events_to_show = rows[-800:] if len(rows) > 800 else rows
    if len(rows) > 800:
        lines.append(f"  ... ({len(rows) - 800} older events omitted for brevity — showing most recent 800)")
    for r in events_to_show:
        date = r.get("Local Date", "")
        time_str = r.get("Local Time", "")
        t = r.get("Type", "")
        msg = (r.get("Message") or "").strip()
        if msg:
            lines.append(f"  [{date} {time_str}] {t}: {msg[:300]}")
        else:
            lines.append(f"  [{date} {time_str}] {t}")
    return "\n".join(lines)


def _worker_daily_summaries(worker_name: str, days: int | None = None) -> str:
    """All Daily Summary rows for one worker — entire history by default."""
    try:
        ws = sheets.open_tracker().worksheet(config.SUMMARY_TAB)
        rows = ws.get_all_records()
    except Exception:
        return ""
    if not rows:
        return ""
    if days is None:
        relevant = [r for r in rows if str(r.get("Worker", "")).strip() == worker_name]
    else:
        today = datetime.now(ZoneInfo(config.MANAGER_TZ)).date()
        cutoff = (today - timedelta(days=days)).isoformat()
        relevant = [r for r in rows
                    if str(r.get("Worker", "")).strip() == worker_name
                    and str(r.get("Date", "")) >= cutoff]
    if not relevant:
        return ""
    relevant.sort(key=lambda r: str(r.get("Date", "")))
    scope = f"all-time ({len(relevant)} days)" if days is None else f"past {days} days ({len(relevant)} days)"
    lines = [f"DAILY SUMMARIES — {scope}:"]
    for r in relevant:
        date = r.get("Date", "")
        hours = r.get("Active Hours", "")
        checkins = r.get("Check-ins", "")
        status = r.get("Status", "")
        capacity = r.get("Capacity Signal", "")
        notes = (r.get("Notes") or "").strip()
        day_sum = (r.get("Day Summary") or "").strip()
        autos = (r.get("Automation Ideas") or "").strip()
        flags = (r.get("Manual Red Flags") or "").strip()
        block = [f"- [{date}] {hours}h · {checkins} check-ins · {status} · capacity={capacity}"]
        if notes:
            block.append(f"    notes: {notes}")
        if day_sum:
            block.append(f"    summary: {day_sum}")
        if autos:
            block.append(f"    automation ideas: {autos}")
        if flags:
            block.append(f"    manual red flags: {flags}")
        lines.append("\n".join(block))
    return "\n".join(lines)


def _worker_full_profile(worker_user_id: str, worker_name: str) -> str:
    """The durable Worker Profile row, expanded."""
    try:
        p = sheets.load_profile(worker_user_id)
    except Exception:
        return ""
    if not p:
        return ""
    lines = [f"WORKER PROFILE (durable, updated weekly by AI synthesizer):"]
    for label, key in [
        ("role / day-to-day", "Role / What They Do"),
        ("recurring tasks", "Recurring Tasks"),
        ("known strengths", "Known Strengths"),
        ("known blockers / skill gaps", "Known Blockers / Skill Gaps"),
        ("tools currently used", "Tools They Currently Use"),
        ("automation opportunities (open)", "Automation Opportunities (Open)"),
        ("automation opportunities (shipped)", "Automation Opportunities (Shipped)"),
        ("productivity patterns", "Productivity Patterns"),
        ("coaching notes for manager", "Coaching Notes for Manager"),
        ("first seen", "First Seen"),
        ("days tracked", "Days Tracked"),
        ("last updated", "Last Updated"),
    ]:
        v = (p.get(key) or "").strip()
        if v:
            lines.append(f"  • {label}: {v}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _worker_knowledge(worker_user_id: str) -> str:
    """All Processes & Tools entries for one worker."""
    try:
        items = sheets.list_worker_knowledge(worker_user_id)
    except Exception:
        return ""
    if not items:
        return ""
    lines = [f"TOOLS & PROCESSES they've mentioned ({len(items)} items):"]
    for it in items:
        kind = (it.get("Kind") or "").strip()
        name = (it.get("Name") or "").strip()
        url = (it.get("URL") or "").strip()
        desc = (it.get("Description") or "").strip()
        steps = (it.get("Steps / Notes") or "").strip()
        bit = f"  • {kind}: {name}"
        if url:
            bit += f" — {url}"
        if desc:
            bit += f"\n        what it is: {desc}"
        if steps:
            bit += f"\n        steps: {steps}"
        lines.append(bit)
    return "\n".join(lines)


def _worker_time_off(slack_user_id: str) -> str:
    """All Time Off entries for one worker."""
    try:
        items = sheets.time_off_for_worker(slack_user_id)
    except Exception:
        return ""
    if not items:
        return ""
    items.sort(key=lambda r: str(r.get("Start Date", "")))
    lines = [f"TIME OFF HISTORY ({len(items)} entries):"]
    for r in items:
        lines.append(f"  • {r.get('Type')} {r.get('Start Date')}"
                     + (f" → {r.get('End Date')}" if r.get('End Date') and r.get('End Date') != r.get('Start Date') else "")
                     + f" ({r.get('Days')} days, {r.get('Status')}, logged by {r.get('Logged By')})")
    return "\n".join(lines)


def build_worker_deep_dive(worker: dict, days: int | None = None) -> str:
    """Build a comprehensive dossier on one worker — everything Sam has ever tracked.
    By default pulls all-time data. Pass `days` to limit to a recent window."""
    name = worker.get("name", "")
    uid = worker.get("user_id", "")
    scope = "all-time" if days is None else f"past {days} days"
    sections = [f"=== DEEP DIVE ON {name} ({scope}) ===",
                f"Roster info: {worker.get('tz', 'UTC')}, {worker.get('pay_type', 'hourly')},"
                f" cadence={worker.get('checkin_interval_min', 120)}min"
                + (f", schedule {worker.get('expected_start', '')}-{worker.get('expected_eod', '')}"
                   if worker.get('expected_start') else "")]
    for builder in [
        lambda: _worker_full_profile(uid, name),
        lambda: _worker_daily_summaries(name, days),
        lambda: _worker_knowledge(uid),
        lambda: _worker_time_off(uid),
        lambda: _worker_activity_history(uid, days),
    ]:
        try:
            s = builder()
            if s:
                sections.append(s)
        except Exception:
            log.exception("deep dive section failed")
    return "\n\n".join(sections)


def detect_mentioned_workers(text: str, workers: list[dict], limit: int = 3) -> list[dict]:
    """Scan a message for any worker name/first-name/nickname. Returns up to `limit`
    workers that the message references. Used to load deep dives when an admin
    asks about specific people."""
    text_lower = " " + text.lower() + " "
    hits: list[dict] = []
    for w in workers:
        if w["user_id"] in config.OWNER_SLACK_IDS:
            continue
        candidates: list[str] = []
        candidates.append(w["name"].lower())
        first = w["name"].split()[0].lower()
        candidates.append(first)
        for n in (w.get("nicknames") or []):
            candidates.append(n.lower())
        # Match on word boundaries — " hannah " matches but "hannahs" doesn't
        for c in candidates:
            if not c:
                continue
            if f" {c} " in text_lower or f" {c}'" in text_lower or f" {c}?" in text_lower or f" {c}." in text_lower or f" {c}," in text_lower:
                if w not in hits:
                    hits.append(w)
                break
    return hits[:limit]


# --- Master builder ---

def build_admin_memory(admin_user_id: str, workers: list[dict],
                       today_snapshot_fn,
                       message_text: str = "") -> str:
    """Build a rich memory block for an admin chat. Cached 30 seconds per admin.

    If `message_text` mentions specific workers by name/nickname, append a
    DEEP DIVE on each of them (all activity, daily summaries, profile,
    knowledge, time off — past 60 days). So 'what does Rey do' becomes a
    real answer instead of a guess.
    """
    # Include the deep-dive workers in the cache key so two different admin
    # queries about different workers don't collide on the same cached blob
    focused = detect_mentioned_workers(message_text or "", workers, limit=3) if message_text else []
    focused_ids = tuple(sorted(w["user_id"] for w in focused))
    cache_key = ("admin_memory", admin_user_id, focused_ids)
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

    # If the admin's message references specific workers by name, append a full
    # deep dive on each one — ALL-TIME data. Sam's full memory on that worker.
    for w in focused:
        try:
            dd = build_worker_deep_dive(w, days=None)  # entire history
            if dd:
                sections.append(dd)
        except Exception:
            log.exception("deep dive failed for %s", w.get("name"))

    result = "\n\n".join(sections) if sections else "(no team data available yet)"
    _put(cache_key, result)
    return result
