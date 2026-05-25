"""Slack bot + scheduler.

Run with: python -m worker_tracker.bot

Workers DM the bot to clock in. Any message before they've logged in today
= login. Messages with EOD keywords end their day. Everything else is a
check-in. Bot auto-DMs each worker every CHECKIN_INTERVAL_MINUTES asking
what they did + if they need help.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, sheets, report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("slack_bolt").setLevel(logging.WARNING)
logging.getLogger("slack_sdk").setLevel(logging.WARNING)
log = logging.getLogger("worker_tracker.bot")

_eod_re = re.compile("|".join(config.EOD_PATTERNS), re.IGNORECASE)
_help_re = re.compile("|".join(config.HELP_PATTERNS), re.IGNORECASE)

scheduler = BackgroundScheduler()
_app = None  # set in main()

WORKERS: dict[str, dict] = {}          # user_id -> roster dict
LOGGED_IN_TODAY: dict[str, str] = {}   # user_id -> local_date_iso
PROMPT_PENDING: dict[str, datetime] = {}


def reload_roster() -> None:
    workers = sheets.load_roster()
    WORKERS.clear()
    for w in workers:
        WORKERS[w["user_id"]] = w
    log.info("Roster loaded: %d active workers", len(workers))


def _local_today(worker: dict) -> str:
    try:
        return datetime.now(ZoneInfo(worker["tz"])).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def is_eod(text: str) -> bool:
    return bool(_eod_re.search(text or ""))


def has_help(text: str) -> bool:
    return bool(_help_re.search(text or ""))


def schedule_next_prompt(user_id: str) -> None:
    job_id = f"prompt:{user_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    when = datetime.now(timezone.utc) + timedelta(minutes=config.CHECKIN_INTERVAL_MINUTES)
    scheduler.add_job(send_prompt, "date", run_date=when, args=[user_id], id=job_id)


def send_prompt(user_id: str) -> None:
    worker = WORKERS.get(user_id)
    if not worker or _app is None:
        return
    first = worker["name"].split()[0] if worker["name"] else "friend"
    text = (
        f"Hey {first} — quick check-in. What did you get done in the last "
        f"~{config.CHECKIN_INTERVAL_MINUTES // 60}h {config.CHECKIN_INTERVAL_MINUTES % 60}m, "
        f"and is anything blocking you?"
    )
    try:
        _app.client.chat_postMessage(channel=user_id, text=text)
        sheets.append_event(worker["name"], user_id, "prompt_sent", "", worker["tz"])
        PROMPT_PENDING[user_id] = datetime.now(timezone.utc)
        miss_id = f"miss:{user_id}"
        try:
            scheduler.remove_job(miss_id)
        except Exception:
            pass
        scheduler.add_job(
            mark_missed, "date",
            run_date=datetime.now(timezone.utc) + timedelta(minutes=config.MISSED_CHECKIN_GRACE_MINUTES),
            args=[user_id], id=miss_id,
        )
    except Exception:
        log.exception("send_prompt failed for %s", user_id)


def mark_missed(user_id: str) -> None:
    if user_id not in PROMPT_PENDING:
        return
    worker = WORKERS.get(user_id)
    if worker:
        sheets.append_event(worker["name"], user_id, "missed_checkin", "", worker["tz"])
    PROMPT_PENDING.pop(user_id, None)
    schedule_next_prompt(user_id)


def handle_message(event, client) -> None:
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return

    user_id = event.get("user")
    text = (event.get("text") or "").strip()
    if not user_id:
        return

    if user_id not in WORKERS:
        reload_roster()
        if user_id not in WORKERS:
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text=f"You're not on the roster yet. Tell your manager your Slack User ID: `{user_id}`.",
                )
            except Exception:
                pass
            return

    worker = WORKERS[user_id]
    today = _local_today(worker)

    PROMPT_PENDING.pop(user_id, None)
    try:
        scheduler.remove_job(f"miss:{user_id}")
    except Exception:
        pass

    if is_eod(text):
        sheets.append_event(worker["name"], user_id, "eod", text, worker["tz"])
        try:
            scheduler.remove_job(f"prompt:{user_id}")
        except Exception:
            pass
        LOGGED_IN_TODAY.pop(user_id, None)
        summary = report.write_worker_summary(worker)
        first = worker["name"].split()[0] if worker["name"] else "you"
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"Logged you out, {first}. {summary['active_hours']}h active, "
                f"{len(summary['checkins'])} check-ins. Talk tomorrow."
            ),
        )
        return

    if LOGGED_IN_TODAY.get(user_id) != today:
        LOGGED_IN_TODAY[user_id] = today
        sheets.append_event(worker["name"], user_id, "login", text, worker["tz"])
        schedule_next_prompt(user_id)
        first = worker["name"].split()[0] if worker["name"] else "you"
        hours = config.CHECKIN_INTERVAL_MINUTES // 60
        mins = config.CHECKIN_INTERVAL_MINUTES % 60
        cadence = f"{hours}h" if mins == 0 else f"{hours}h{mins}m"
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"Got it, {first} — clocked you in. I'll check in every {cadence}. "
                f"Message 'EOD' when you're done."
            ),
        )
        return

    ev_type = "help_request" if has_help(text) else "checkin"
    sheets.append_event(worker["name"], user_id, ev_type, text, worker["tz"])
    schedule_next_prompt(user_id)
    if ev_type == "help_request":
        try:
            client.chat_postMessage(
                channel=user_id,
                text="Flagged — manager will see this in the EOD report. Say more if you want it pinged sooner.",
            )
        except Exception:
            pass


def restore_state() -> None:
    """On startup: any worker logged in today w/ no EOD → resume their schedule."""
    for user_id, worker in WORKERS.items():
        today = _local_today(worker)
        rows = [r for r in sheets.activity_rows(today) if r.get("Slack User ID") == user_id]
        rows.sort(key=lambda r: r.get("Timestamp UTC", ""))
        logged_in = False
        for r in rows:
            t = r.get("Type")
            if t == "login":
                logged_in = True
            elif t == "eod":
                logged_in = False
        if logged_in:
            LOGGED_IN_TODAY[user_id] = today
            schedule_next_prompt(user_id)
            log.info("Resumed active session for %s", worker["name"])


def schedule_daily_digest() -> None:
    hh, mm = map(int, config.REPORT_TIME_LOCAL.split(":"))
    scheduler.add_job(
        report.send_daily_digest,
        "cron",
        hour=hh, minute=mm,
        timezone=ZoneInfo(config.MANAGER_TZ),
        id="daily_digest",
    )
    log.info("Daily digest scheduled %s %s", config.REPORT_TIME_LOCAL, config.MANAGER_TZ)


def schedule_weekly_synthesis() -> None:
    hh, mm = map(int, config.WEEKLY_SYNTHESIS_TIME.split(":"))
    dow = config.WEEKLY_SYNTHESIS_DOW
    scheduler.add_job(
        report.run_weekly_synthesis,
        "cron",
        day_of_week=dow, hour=hh, minute=mm,
        timezone=ZoneInfo(config.MANAGER_TZ),
        id="weekly_synthesis",
    )
    log.info("Weekly synthesis scheduled dow=%d %s %s", dow, config.WEEKLY_SYNTHESIS_TIME, config.MANAGER_TZ)


def main() -> None:
    global _app
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    missing = [n for n, v in [
        ("SLACK_BOT_TOKEN", config.SLACK_BOT_TOKEN),
        ("SLACK_APP_TOKEN", config.SLACK_APP_TOKEN),
        ("WORKER_TRACKER_SHEET_ID", config.TRACKER_SHEET_ID),
    ] if not v]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}. See worker_tracker/SETUP.md")

    _app = App(token=config.SLACK_BOT_TOKEN)
    _app.event("message")(handle_message)
    _app.event("app_mention")(lambda say: say("DM me — I track check-ins in DMs, not channels."))

    reload_roster()
    if not WORKERS:
        log.warning("Roster is empty — add workers to the Roster tab and restart.")
    scheduler.start()
    restore_state()
    schedule_daily_digest()
    schedule_weekly_synthesis()
    log.info("Starting Socket Mode handler. Ctrl-C to stop.")
    SocketModeHandler(_app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
