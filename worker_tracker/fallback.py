"""Tier 3 — Last-resort fallback when Sonnet is unreachable.

Runs ONLY when:
  - Tier 1 (deterministic commands) didn't match, AND
  - Tier 2 (Sonnet agent) returned None (transient errors exhausted)

Approach: keyword-based guess at intent → direct tool call (no LLM) →
synthesize a useful reply. The user always gets real data, never a scary
error. Tone is honest about being degraded: "my brain is being slow but
here's what i can pull from the sheet directly."

This layer is intentionally narrow — it only handles the most common
question patterns. Edge cases get the polite "i'm having trouble right
now, try again in a moment" reply (still better than 'ClientError').
"""
from __future__ import annotations

import logging
import re

from . import tools

log = logging.getLogger(__name__)


_PREFIX = "_(my brain's running slow right now — pulling this from the sheet directly)_\n\n"


def _looks_like(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def _extract_worker_name(text: str, workers: list[dict]) -> dict | None:
    """Find a worker name mentioned in the text. Used to make a guess
    when the user is asking about someone but Sonnet is down."""
    low = text.lower()
    # Try every worker's first name + nicknames
    for w in workers:
        first = w["name"].split()[0].lower()
        if re.search(rf"\b{re.escape(first)}\b", low):
            return w
        for nick in (w.get("nicknames") or []):
            if re.search(rf"\b{re.escape(nick.lower())}\b", low):
                return w
    return None


def fallback_reply(
    text: str,
    speaker_user_id: str,
    speaker_name: str,
    is_owner: bool,
    is_manager: bool,
    workers: list[dict],
) -> str:
    """Always returns SOMETHING useful. Never None, never a scary error."""
    is_admin = is_owner or is_manager
    low = text.lower()
    target = _extract_worker_name(text, workers)

    try:
        # Benefit questions
        if _looks_like(text, "vacation", "sick day", "holiday", "pto",
                        "benefits", "leave", "balance"):
            if target:
                data = tools.get_worker_benefits(target["name"], workers,
                                                  is_speaker_admin=is_admin)
                if "error" in data:
                    return _PREFIX + data["error"]
                return _PREFIX + _format_benefits(data)
            # Speaker asking about themselves
            speaker_w = next((w for w in workers if w["user_id"] == speaker_user_id), None)
            if speaker_w:
                data = tools.get_worker_benefits(speaker_w["name"], workers,
                                                  is_speaker_admin=is_admin)
                return _PREFIX + _format_benefits(data)

        # Status questions about a specific worker
        if target and _looks_like(text, "working", "status", "doing", "online",
                                    "where is", "where's", "is " + target["name"].split()[0].lower()):
            data = tools.get_worker_status(target["name"], workers,
                                            is_speaker_admin=is_admin)
            if "error" in data:
                return _PREFIX + data["error"]
            return _PREFIX + _format_status(data)

        # Hours questions
        if _looks_like(text, "hours", "how many hours", "this period",
                        "pay period", "payroll"):
            who = target or next((w for w in workers if w["user_id"] == speaker_user_id), None)
            if who:
                data = tools.get_worker_hours(who["name"], "current", workers,
                                               is_speaker_admin=is_admin)
                if "error" in data:
                    return _PREFIX + data["error"]
                return _PREFIX + _format_hours(data)

        # Team status
        if _looks_like(text, "team status", "everyone", "who's working",
                        "team is working", "team is doing"):
            data = tools.get_team_status(workers)
            return _PREFIX + _format_team_status(data)

        # Send digest
        if _looks_like(text, "send digest", "digest now", "eod report", "send the digest"):
            if not is_admin:
                return "the digest is admin-only — ask Jan."
            data = tools.send_eod_digest_now()
            if data.get("ok"):
                return _PREFIX + f"✓ digest sent · {data.get('workers', 0)} workers"
            return _PREFIX + "tried to send the digest but hit an error — try again in a moment."

    except Exception:
        log.exception("fallback dispatch crashed")

    # Couldn't pattern-match — polite default
    return (
        f"hey {speaker_name} — my brain's running slow right now and i couldn't "
        f"figure out a clean answer. try rephrasing in a sec? if you want a "
        f"specific worker's status try 'is <name> working' or 'how many "
        f"vacation days does <name> have'."
    )


# ─────────────────────────────────────────────────────────────────────────
# Formatters — convert tool result dicts into Slack-friendly text
# ─────────────────────────────────────────────────────────────────────────

def _format_benefits(data: dict) -> str:
    name = data.get("name", "")
    first = name.split()[0] if name else ""
    remaining = data.get("remaining", {})
    used = data.get("used", {})
    extras = data.get("extras", {})
    lines = [f"*{first}* — leave balance for {data.get('year', '')}:"]
    for kind in ("vacation", "sick", "holiday", "pto"):
        r = remaining.get(kind, 0)
        u = used.get(kind, 0)
        if r or u:
            lines.append(f"  • {kind}: {r} left ({u} used)")
    if extras.get("hmo_reimbursement_php"):
        lines.append(f"  • HMO reimbursement: PHP {extras['hmo_reimbursement_php']:,}")
    if extras.get("performance_bonus_date"):
        lines.append(f"  • perf bonus: {extras['performance_bonus_date']}")
    return "\n".join(lines)


def _format_status(data: dict) -> str:
    name = data.get("name", "")
    state = data.get("state", "")
    if state == "not_started":
        return f"*{name}* hasn't clocked in for the current shift."
    hours = data.get("hours_so_far", 0)
    login = data.get("login_local", "")
    if state == "logged_off":
        eod = data.get("eod_local", "")
        return f"*{name}* — logged off. login {login} → EOD {eod}, {hours}h active."
    if state == "on_break":
        bmin = data.get("current_break_minutes", 0)
        return f"*{name}* — on break ({bmin}min). clocked in at {login}, {hours}h so far."
    last = data.get("last_checkin_message", "")
    last_ago = data.get("last_checkin_minutes_ago")
    last_line = f"\nlast check-in ({last_ago}m ago): \"{last}\"" if last else ""
    return f"*{name}* — working. clocked in at {login}, {hours}h on the clock.{last_line}"


def _format_hours(data: dict) -> str:
    name = data.get("name", "")
    first = name.split()[0] if name else ""
    period = data.get("period", "")
    total = data.get("total_incl_today", 0)
    today = data.get("hours_today_open_session", 0)
    completed = data.get("hours_completed_days", 0)
    today_line = f"  • {today}h on the clock right now (today)\n" if today else ""
    return (f"hey {first} — pay period {period}:\n"
            f"  • {data.get('days_completed', 0)} days completed\n"
            f"  • {completed}h from completed days\n"
            f"{today_line}"
            f"  • *{total}h total*")


def _format_team_status(data: dict) -> str:
    workers = data.get("workers", [])
    if not workers:
        return "no active workers right now."
    lines = ["*team status*"]
    for w in workers:
        state = w.get("state", "")
        first = w.get("name", "").split()[0]
        hours = w.get("hours_so_far", 0)
        if state == "working":
            lines.append(f"  🟢 {first} — working, {hours}h")
        elif state == "on_break":
            bmin = w.get("current_break_minutes", 0)
            lines.append(f"  🟡 {first} — on break ({bmin}m), {hours}h so far")
        elif state == "logged_off":
            lines.append(f"  ⚫ {first} — logged off, {hours}h today")
        else:
            lines.append(f"  ⚪ {first} — hasn't clocked in")
    return "\n".join(lines)
