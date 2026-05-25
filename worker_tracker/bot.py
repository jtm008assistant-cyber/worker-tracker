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

from . import config, sheets, report, analyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("slack_bolt").setLevel(logging.WARNING)
logging.getLogger("slack_sdk").setLevel(logging.WARNING)
log = logging.getLogger("worker_tracker.bot")

_eod_re = re.compile("|".join(config.EOD_PATTERNS), re.IGNORECASE)
_help_re = re.compile("|".join(config.HELP_PATTERNS), re.IGNORECASE)
_break_start_re = re.compile("|".join(config.BREAK_START_PATTERNS), re.IGNORECASE)
_break_end_re = re.compile("|".join(config.BREAK_END_PATTERNS), re.IGNORECASE)

scheduler = BackgroundScheduler()
_app = None  # set in main()

WORKERS: dict[str, dict] = {}          # user_id -> roster dict
LOGGED_IN_TODAY: dict[str, str] = {}   # user_id -> local_date_iso
PROMPT_PENDING: dict[str, datetime] = {}
ON_BREAK: dict[str, datetime] = {}     # user_id -> break_start_utc

# Active follow-up state. PENDING_FOLLOWUP[uid] -> {topic, asked_at}: the bot
# asked a follow-up, expects the worker's next message to be the answer.
PENDING_FOLLOWUP: dict[str, dict] = {}
# FOLLOWUPS_TODAY[uid] -> {date, count, topics}: how many we've asked today.
FOLLOWUPS_TODAY: dict[str, dict] = {}
LAST_FOLLOWUP_AT: dict[str, datetime] = {}


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


def is_break_start(text: str) -> bool:
    return bool(_break_start_re.search(text or ""))


def is_break_end(text: str) -> bool:
    return bool(_break_end_re.search(text or ""))


def _interval_for(user_id: str) -> int:
    w = WORKERS.get(user_id)
    if w and w.get("checkin_interval_min"):
        return int(w["checkin_interval_min"])
    return config.CHECKIN_INTERVAL_MINUTES


def schedule_next_prompt(user_id: str) -> None:
    job_id = f"prompt:{user_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    when = datetime.now(timezone.utc) + timedelta(minutes=_interval_for(user_id))
    scheduler.add_job(send_prompt, "date", run_date=when, args=[user_id], id=job_id)


def send_prompt(user_id: str) -> None:
    worker = WORKERS.get(user_id)
    if not worker or _app is None:
        return
    first = worker["name"].split()[0] if worker["name"] else "friend"
    text = (
        f"hey {first} 👋 quick one — what'd you knock out the last bit? "
        f"all good or stuck on anything?"
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
                    text=(
                        f"hey, I'm Sam 👋 I help the team stay in sync on what everyone's "
                        f"working on. I don't have you on my list yet though — send your "
                        f"manager your Slack ID so they can add you: `{user_id}`"
                    ),
                )
            except Exception:
                pass
            return

    worker = WORKERS[user_id]
    today = _local_today(worker)
    first = worker["name"].split()[0] if worker["name"] else "you"

    PROMPT_PENDING.pop(user_id, None)
    try:
        scheduler.remove_job(f"miss:{user_id}")
    except Exception:
        pass

    # --- Break handling ---
    # If they're already on break, any message resumes them (unless it's another
    # break-start keyword, in which case just remind them they're still paused).
    if user_id in ON_BREAK:
        if is_break_start(text) and not is_break_end(text) and not is_eod(text):
            try:
                client.chat_postMessage(channel=user_id, text="already paused — message me when you're back 👌")
            except Exception:
                pass
            return
        break_start_ts = ON_BREAK.pop(user_id)
        duration_min = (datetime.now(timezone.utc) - break_start_ts).total_seconds() / 60
        sheets.append_event(
            worker["name"], user_id, "break_end",
            f"break duration: {duration_min:.0f}min",
            worker["tz"],
        )
        try:
            client.chat_postMessage(
                channel=user_id,
                text=f"welcome back {first}! that was a {duration_min:.0f}min break — back on the clock 🙌",
            )
        except Exception:
            pass
        # Don't return — the current message also counts as a check-in (fall through)

    # If not on break, and the message looks like a break-start, pause them.
    elif is_break_start(text) and not is_eod(text):
        # Only valid if they're already clocked in today
        if LOGGED_IN_TODAY.get(user_id) == today:
            ON_BREAK[user_id] = datetime.now(timezone.utc)
            try:
                scheduler.remove_job(f"prompt:{user_id}")
            except Exception:
                pass
            sheets.append_event(worker["name"], user_id, "break_start", text, worker["tz"])
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text=f"got it {first} — paused the clock 🛑 message me anything when you're back",
                )
            except Exception:
                pass
            return

    if is_eod(text):
        sheets.append_event(worker["name"], user_id, "eod", text, worker["tz"])
        try:
            scheduler.remove_job(f"prompt:{user_id}")
        except Exception:
            pass
        LOGGED_IN_TODAY.pop(user_id, None)
        ON_BREAK.pop(user_id, None)
        summary = report.write_worker_summary(worker)
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"alright {first}, you're out 👋 {summary['active_hours']}h, "
                f"{len(summary['checkins'])} check-ins. catch you tomorrow."
            ),
        )
        return

    if LOGGED_IN_TODAY.get(user_id) != today:
        LOGGED_IN_TODAY[user_id] = today
        sheets.append_event(worker["name"], user_id, "login", text, worker["tz"])
        schedule_next_prompt(user_id)
        interval = _interval_for(user_id)
        hours = interval // 60
        mins = interval % 60
        cadence = f"{hours}h" if mins == 0 else f"{hours}h{mins}m"
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"hey {first}! got you in 🙌 I'll loop back every {cadence} to see "
                f"how things are going. shoot me 'EOD' whenever you wrap up."
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
                text="noted — flagging this for the manager. drop more detail if you need it sooner.",
            )
        except Exception:
            pass

    # --- Knowledge / follow-up handling ---
    try:
        _handle_knowledge_and_followup(user_id, worker, text, today, client)
    except Exception:
        log.exception("knowledge/follow-up step failed for %s", user_id)


def _reset_followups_if_new_day(user_id: str, local_date: str) -> None:
    state = FOLLOWUPS_TODAY.get(user_id)
    if not state or state.get("date") != local_date:
        FOLLOWUPS_TODAY[user_id] = {"date": local_date, "count": 0, "topics": []}


def _handle_knowledge_and_followup(user_id: str, worker: dict, text: str,
                                   today: str, client) -> None:
    """Two-step:
    1) If we asked a follow-up earlier and they're now replying, extract any
       tools/sheets/processes from the reply and save to the Knowledge tab.
    2) After that (or if no pending), maybe ask a NEW follow-up about
       something they mentioned. Rate-limited.
    """
    _reset_followups_if_new_day(user_id, today)
    existing = sheets.list_worker_knowledge(user_id)

    pending = PENDING_FOLLOWUP.pop(user_id, None)
    if pending:
        items = analyzer.extract_knowledge_from_reply(
            name=worker["name"],
            reply_text=text,
            asked_topic=pending.get("topic"),
            existing_knowledge=existing,
        )
        if items:
            for it in items:
                it["Worker"] = worker["name"]
                it["Slack User ID"] = user_id
                sheets.upsert_knowledge(it)
            try:
                client.chat_postMessage(
                    channel=user_id,
                    text=f"saved 🙌 (logged {len(items)} new {'item' if len(items)==1 else 'items'} to your tools list — thanks)",
                )
            except Exception:
                pass
            # Refresh existing so we don't double-ask about something we just learned
            existing = sheets.list_worker_knowledge(user_id)

    state = FOLLOWUPS_TODAY[user_id]
    if state["count"] >= config.MAX_FOLLOWUPS_PER_DAY:
        return
    last = LAST_FOLLOWUP_AT.get(user_id)
    if last and (datetime.now(timezone.utc) - last).total_seconds() < config.FOLLOWUP_COOLDOWN_MINUTES * 60:
        return

    decision = analyzer.maybe_ask_followup(
        name=worker["name"],
        message=text,
        knowledge=existing,
        already_asked_today=state["topics"],
    )
    if not decision:
        return

    try:
        client.chat_postMessage(channel=user_id, text=decision["ask"])
    except Exception:
        log.exception("Failed to send follow-up question to %s", user_id)
        return
    PENDING_FOLLOWUP[user_id] = {"topic": decision["topic"], "asked_at": datetime.now(timezone.utc)}
    state["count"] += 1
    state["topics"].append(decision["topic"])
    LAST_FOLLOWUP_AT[user_id] = datetime.now(timezone.utc)


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


def schedule_payroll() -> None:
    if config.PAYROLL_PERIOD == "none":
        log.info("PAYROLL_PERIOD=none, payroll cron disabled")
        return
    hh, mm = map(int, config.PAYROLL_RUN_TIME.split(":"))
    tz = ZoneInfo(config.MANAGER_TZ)
    if config.PAYROLL_PERIOD == "semimonthly":
        # Pay on the 1st (covers prior 15th-EOM) and 15th (covers 1st-14th).
        for day in (1, 15):
            scheduler.add_job(
                report.run_and_send_payroll, "cron",
                day=day, hour=hh, minute=mm, timezone=tz,
                id=f"payroll_day{day}",
            )
        log.info("Payroll cron scheduled: 1st + 15th at %s %s", config.PAYROLL_RUN_TIME, config.MANAGER_TZ)
        return
    if config.PAYROLL_PERIOD in ("weekly", "biweekly"):
        # Run Monday morning (after Sunday-ending workweek closes).
        scheduler.add_job(
            report.run_and_send_payroll, "cron",
            day_of_week=0, hour=hh, minute=mm, timezone=tz,
            id="payroll_weekly",
        )
        log.info("Payroll cron scheduled: Mondays at %s %s (%s)", config.PAYROLL_RUN_TIME, config.MANAGER_TZ, config.PAYROLL_PERIOD)
        return
    if config.PAYROLL_PERIOD == "monthly":
        scheduler.add_job(
            report.run_and_send_payroll, "cron",
            day=1, hour=hh, minute=mm, timezone=tz,
            id="payroll_monthly",
        )
        log.info("Payroll cron scheduled: 1st of month at %s %s", config.PAYROLL_RUN_TIME, config.MANAGER_TZ)


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
    schedule_payroll()
    log.info("Starting Socket Mode handler. Ctrl-C to stop.")
    SocketModeHandler(_app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
