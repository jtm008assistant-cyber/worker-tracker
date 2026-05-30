"""Tier 1 — deterministic command layer.

These commands NEVER touch an LLM. They run sub-100ms, are 100% reliable,
and survive any Gemini/Anthropic outage. If a user types one of these
phrases, the bot responds instantly from real data.

Architecture: try_deterministic(text, ctx) returns either:
  - None  → not a deterministic command, fall through to Tier 2 (agent)
  - str   → reply text; the action has already been performed (digest sent,
            task logged, etc.); caller just needs to send the reply DM.

Every handler wrapped in try/except so a bug in one command can't break
the bot. On unexpected error, returns None (falls through to agent) +
logs the exception.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import config, sheets

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Command pattern set — must be unambiguous, message-anchored
# ─────────────────────────────────────────────────────────────────────────

# These ONLY match if the message IS the command (with maybe a question
# mark or trailing whitespace). They never match if the command words
# appear inside a longer message — that's the "regex matched the wrong
# word in the body" class of bug we keep getting.

PAT_SEND_DIGEST = re.compile(
    r"^\s*(?:send|run|trigger|gimme|give me)?\s*(?:the\s+|today'?s\s+)?"
    r"(?:eod|EOD|daily)?\s*(?:digest|report|summary)\s*(?:now|please)?\s*\??$",
    re.IGNORECASE,
)
PAT_DIGEST_SHORTHAND = re.compile(
    r"^\s*(?:send\s+)?digest\s*\??$",
    re.IGNORECASE,
)
PAT_EOD_REPORT_NOW = re.compile(
    r"^\s*(?:eod|EOD)\s+(?:report|digest|summary)\s+now\s*\??$",
    re.IGNORECASE,
)

PAT_TEAM_STATUS = re.compile(
    r"^\s*(?:team\s+status|status\s+of\s+(?:the\s+)?team|"
    r"who'?s?\s+(?:working|online|on\s+the\s+clock|active|in)|"
    r"who\s+(?:is\s+)?(?:working|online|in)|"
    r"is\s+anyone\s+(?:working|online|on|here|around)|"
    r"how\s+(?:is|are)\s+(?:everyone|the\s+team|everybody)\s*(?:doing|today)?|"
    r"did\s+(?:everyone|everybody)\s+(?:log\s*in|clock\s*in|sign\s*in|start)|"
    r"who\s+(?:hasn'?t|has\s+not|hadn'?t|had\s+not)\s+(?:logged\s*in|clocked\s*in|signed\s*in|come\s*in|started))"
    r"(?:\s+(?:today|yet|now))?\s*\??$",
    re.IGNORECASE,
)

PAT_MY_TASKS = re.compile(
    r"^\s*(?:my\s+(?:tasks?|list|checklist|todo|to[\s-]?do|plate|queue|things)|"
    r"what'?s?\s+on\s+my\s+(?:plate|list|checklist|todo|to[\s-]?do)|"
    r"what\s+do\s+i\s+have(?:\s+to\s+do)?|"
    r"show\s+me\s+(?:my|the)\s+(?:tasks?|list|checklist|todo)|"
    r"tasks?|todo|to[\s-]?do|checklist|my\s+list)"
    r"\s*\??$",
    re.IGNORECASE,
)

PAT_MY_HOURS = re.compile(
    r"^\s*(?:my\s+hours?|hours?|how\s+(?:many\s+)?hours?(?:\s+(?:have|did)\s+i\s+(?:worked?|logged))?|"
    r"hours?\s+(?:this\s+period|so\s+far|today)|"
    r"how\s+many\s+hours?\s+(?:have|did)\s+i\s+(?:work|worked|log|logged))"
    r"\s*\??$",
    re.IGNORECASE,
)

PAT_MY_BENEFITS = re.compile(
    r"^\s*(?:my\s+(?:benefits|vacation|sick|holiday|pto|leave|time\s+off)(?:\s+(?:days?|balance|left|remaining))?|"
    r"(?:how\s+many\s+|how\s+much\s+)?(?:vacation|sick|holiday|pto|leave)\s+(?:days?|balance|left|remaining)?(?:\s+do\s+i\s+have)?|"
    r"vacation\s+balance|pto\s+balance|"
    r"how\s+(?:many|much)\s+(?:vacation|sick|holiday|pto)(?:\s+(?:days?|do\s+i\s+have))?)"
    r"\s*\??$",
    re.IGNORECASE,
)

PAT_MY_ACTIVITY = re.compile(
    r"^\s*(?:my\s+(?:activity|trail|recap|day|history|check[-\s]?ins)|"
    r"what\s+did\s+i\s+do\s+today|"
    r"what\s+(?:have|'?ve)\s+i\s+(?:done|been\s+(?:doing|working\s+on))(?:\s+today)?|"
    r"show\s+(?:me\s+)?my\s+(?:day|today|activity|trail|recap)|"
    r"recap\s+(?:my|of\s+my)\s+(?:day|today))"
    r"\s*\??$",
    re.IGNORECASE,
)

PAT_MY_YESTERDAY = re.compile(
    r"^\s*(?:what\s+did\s+i\s+do\s+yesterday|"
    r"my\s+yesterday|"
    r"yesterday'?s?\s+(?:activity|recap|trail))"
    r"\s*\??$",
    re.IGNORECASE,
)

PAT_MY_KNOWLEDGE = re.compile(
    r"^\s*(?:my\s+(?:tools?|notes?|knowledge|processes?|workflow)|"
    r"what\s+(?:tools?|processes?|notes?|knowledge)\s+(?:have|did)\s+i\s+(?:shared?|told|sent)|"
    r"what\s+do\s+you\s+know\s+about\s+me|"
    r"what'?s?\s+in\s+my\s+(?:knowledge|notes|profile|tools?))"
    r"\s*\??$",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────
# Command handlers — each returns the reply text after performing the action
# ─────────────────────────────────────────────────────────────────────────

def _cmd_send_digest(ctx: dict) -> str:
    """Trigger the daily EOD digest immediately."""
    from . import report
    result = report.send_daily_digest()
    status = "✓" if result.get("slack") else "✗"
    workers = result.get("workers", 0)
    errors = result.get("errors", []) or []
    out = f"{status} digest sent · {workers} workers"
    if errors:
        out += "\n_errors:_\n" + "\n".join(f"  • {e}" for e in errors[:3])
    return out


def _cmd_team_status(ctx: dict) -> str:
    """Snapshot of the whole team's current state."""
    from . import bot  # circular at import-time avoided by deferring
    return bot._format_team_status()


def _cmd_my_tasks(ctx: dict) -> str:
    """Speaker's own pending + active tasks + commitments."""
    speaker = ctx["speaker_worker"]
    if not speaker:
        return "you're not on the active roster — ask Jan to add you."
    from . import bot
    audience = "admin" if (ctx["is_owner"] or ctx["is_manager"]) else "worker"
    return bot._format_task_list(speaker, audience=audience)


def _cmd_my_hours(ctx: dict) -> str:
    """Speaker's own pay-period hours including today's open session."""
    speaker = ctx["speaker_worker"]
    if not speaker:
        return "you're not on the active roster yet."
    from . import bot
    return bot._format_hours_summary(speaker)


def _cmd_my_benefits(ctx: dict) -> str:
    """Speaker's own vacation/sick/holiday/PTO balance + benefits."""
    speaker = ctx["speaker_worker"]
    if not speaker:
        return "you're not on the active roster yet."
    first = speaker["name"].split()[0] if speaker.get("name") else "you"
    year = datetime.now(ZoneInfo(speaker.get("tz") or "UTC")).year

    alloc = {
        "vacation": int(speaker.get("vacation_days_year") or 0),
        "sick": int(speaker.get("sick_days_year") or 0),
        "holiday": int(speaker.get("holiday_days_year") or 0),
        "pto": int(speaker.get("pto_days_year") or 0),
    }
    used = {"vacation": 0, "sick": 0, "pto": 0, "holiday": 0}
    try:
        for r in sheets.time_off_for_worker(speaker["user_id"], year=year):
            t = (r.get("Type") or "").strip().lower()
            if t in used:
                try:
                    used[t] += int(r.get("Days") or 0)
                except (TypeError, ValueError):
                    pass
    except Exception:
        log.exception("benefits used-lookup failed")

    if sum(alloc.values()) == 0 and not (speaker.get("benefits_notes") or "").strip():
        return (f"hey {first} — i don't have your benefits info on file yet. "
                f"Jan or Hannah will set that up soon.")

    lines = [f"hey {first}, here's your {year} balance:"]
    for kind in ("vacation", "sick", "holiday", "pto"):
        a = alloc.get(kind, 0)
        u = used.get(kind, 0)
        if a or u:
            lines.append(f"• *{kind}*: {a - u} left ({u} used of {a})")
    notes = speaker.get("benefits_notes") or ""
    if notes:
        lines.append(f"\n_note: {notes[:300]}_")
    return "\n".join(lines)


def _cmd_my_activity(ctx: dict, days_back: int = 0) -> str:
    """Speaker's own activity trail for today (days_back=0) or yesterday (1)."""
    speaker = ctx["speaker_worker"]
    if not speaker:
        return "you're not on the active roster yet."
    from . import bot
    return bot._format_self_history(speaker, days_back=days_back)


def _cmd_my_knowledge(ctx: dict) -> str:
    """Speaker's own Knowledge Base entries grouped by kind."""
    speaker = ctx["speaker_worker"]
    if not speaker:
        return "you're not on the active roster yet."
    from . import bot
    return bot._format_worker_knowledge(speaker)


# ─────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────

# (pattern, handler, optional kwargs) — checked in order, first match wins.
_COMMAND_TABLE = [
    (PAT_DIGEST_SHORTHAND,  _cmd_send_digest,     {}),
    (PAT_EOD_REPORT_NOW,    _cmd_send_digest,     {}),
    (PAT_SEND_DIGEST,       _cmd_send_digest,     {}),
    (PAT_TEAM_STATUS,       _cmd_team_status,     {}),
    (PAT_MY_TASKS,          _cmd_my_tasks,        {}),
    (PAT_MY_HOURS,          _cmd_my_hours,        {}),
    (PAT_MY_BENEFITS,       _cmd_my_benefits,     {}),
    (PAT_MY_YESTERDAY,      _cmd_my_activity,     {"days_back": 1}),
    (PAT_MY_ACTIVITY,       _cmd_my_activity,     {"days_back": 0}),
    (PAT_MY_KNOWLEDGE,      _cmd_my_knowledge,    {}),
]


def try_deterministic(
    text: str,
    speaker_user_id: str,
    workers_list: list[dict],
    is_owner: bool,
    is_manager: bool,
) -> str | None:
    """Try to handle the message as a deterministic command.

    Returns:
      - str: reply text (action already performed, just send this DM)
      - None: not a deterministic command, fall through to Tier 2 (agent)
    """
    if not text or not text.strip():
        return None

    speaker_worker = None
    for w in workers_list:
        if w["user_id"] == speaker_user_id:
            speaker_worker = w
            break

    ctx = {
        "text": text,
        "speaker_user_id": speaker_user_id,
        "speaker_worker": speaker_worker,
        "is_owner": is_owner,
        "is_manager": is_manager,
        "workers": workers_list,
    }

    # Gate: team_status, send_digest are admin-only
    ADMIN_ONLY = {_cmd_send_digest, _cmd_team_status}
    is_admin = is_owner or is_manager

    for pattern, handler, kwargs in _COMMAND_TABLE:
        if not pattern.search(text):
            continue
        if handler in ADMIN_ONLY and not is_admin:
            continue
        try:
            log.info("commands: deterministic match %s", handler.__name__)
            return handler(ctx, **kwargs)
        except Exception:
            log.exception("commands: %s threw — falling through to agent",
                          handler.__name__)
            return None

    return None
