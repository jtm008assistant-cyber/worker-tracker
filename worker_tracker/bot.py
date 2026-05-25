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

from . import config, sheets, report, analyzer, payroll, worker_views, onboarding

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("slack_bolt").setLevel(logging.WARNING)
logging.getLogger("slack_sdk").setLevel(logging.WARNING)
log = logging.getLogger("worker_tracker.bot")

_eod_re = re.compile("|".join(config.EOD_PATTERNS), re.IGNORECASE)
_help_re = re.compile("|".join(config.HELP_PATTERNS), re.IGNORECASE)
_break_start_re = re.compile("|".join(config.BREAK_START_PATTERNS), re.IGNORECASE)
_break_end_re = re.compile("|".join(config.BREAK_END_PATTERNS), re.IGNORECASE)
_hours_query_re = re.compile("|".join(config.HOURS_QUERY_PATTERNS), re.IGNORECASE)
_discrepancy_re = re.compile("|".join(config.DISCREPANCY_PATTERNS), re.IGNORECASE)
_admin_intro_re = re.compile("|".join(config.ADMIN_INTRODUCE_PATTERNS), re.IGNORECASE)
_admin_status_re = re.compile("|".join(config.ADMIN_STATUS_PATTERNS), re.IGNORECASE)

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


def is_hours_query(text: str) -> bool:
    return bool(_hours_query_re.search(text or ""))


def is_discrepancy(text: str) -> bool:
    return bool(_discrepancy_re.search(text or ""))


def _format_hours_summary(worker: dict) -> str:
    """Compute and format this worker's current pay-period hours for them."""
    start, end = payroll.current_open_period()
    totals = payroll.worker_period_totals(worker, start, end)
    first = worker["name"].split()[0] if worker["name"] else "you"
    if totals["total_hours"] == 0:
        return f"hey {first}, no hours logged yet for this period ({start} → {end}). nothing to show yet 👀"
    ot_line = ""
    if totals["overtime_hours"]:
        ot_line = f" ({totals['regular_hours']}h regular + {totals['overtime_hours']}h OT)"
    return (
        f"hey {first} — pay period {start} → {end} so far:\n"
        f"• {totals['days_worked']} days worked\n"
        f"• {totals['total_hours']}h total{ot_line}\n\n"
        f"if anything looks off (missed a break, missed a login, etc.), just message me with details "
        f"and I'll flag it for review before payroll."
    )


def _find_worker_by_name(query: str) -> list[dict]:
    """Fuzzy match a name query against the roster. Tries (1) full name exact
    match, (2) first-name exact match, (3) substring on full name. Returns
    list of matching worker dicts."""
    q = query.strip().lower()
    if not q:
        return []
    workers = list(WORKERS.values())
    # 1) exact full-name match
    exact = [w for w in workers if w["name"].lower() == q]
    if exact:
        return exact
    # 2) first-name exact match
    firstname = [w for w in workers if w["name"].split()[0].lower() == q]
    if firstname:
        return firstname
    # 3) substring anywhere in full name
    substr = [w for w in workers if q in w["name"].lower()]
    return substr


def _format_worker_status(worker: dict) -> str:
    """Snapshot of where a worker currently is right now. Reads today's
    Activity Log and pay-period summary."""
    from . import payroll as _payroll
    try:
        tz = ZoneInfo(worker["tz"])
    except Exception:
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz)
    today = now_local.date().isoformat()
    rows = [r for r in sheets.activity_rows(today) if r.get("Slack User ID") == worker["user_id"]]
    rows.sort(key=lambda r: r.get("Timestamp UTC", ""))

    login_ts = eod_ts = last_checkin_ts = None
    last_checkin_msg = ""
    break_start_ts = None
    state = "not_started"
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["Timestamp UTC"]).astimezone(timezone.utc)
        except Exception:
            continue
        t = r.get("Type", "")
        msg = r.get("Message", "")
        if t == "login":
            login_ts = login_ts or ts
            state = "working"
        elif t == "eod":
            eod_ts = ts
            state = "logged_off"
            break_start_ts = None
        elif t == "checkin" or t == "help_request":
            last_checkin_ts = ts
            last_checkin_msg = msg
        elif t == "break_start":
            break_start_ts = ts
            state = "on_break"
        elif t == "break_end":
            break_start_ts = None
            state = "working"

    first = worker["name"].split()[0] if worker["name"] else worker["name"]

    if state == "not_started":
        # Look for last activity in last 7 days
        recent = sheets.activity_since(7, slack_user_id=worker["user_id"])
        last_seen = ""
        if recent:
            recent.sort(key=lambda r: r.get("Timestamp UTC", ""))
            last_seen = recent[-1].get("Local Date", "")
        return (
            f"⚪ *{worker['name']}* — hasn't clocked in today\n"
            + (f"last seen: {last_seen}" if last_seen else "no recent activity recorded")
        )

    # Get pay-period totals for context
    try:
        start, end = _payroll.current_open_period()
        totals = _payroll.worker_period_totals(worker, start, end)
        period_line = f"pay period ({start} → {end}): {totals['total_hours']}h logged"
    except Exception:
        period_line = ""

    login_str = login_ts.astimezone(tz).strftime("%H:%M") if login_ts else "—"
    if state == "logged_off":
        eod_str = eod_ts.astimezone(tz).strftime("%H:%M") if eod_ts else "—"
        return (
            f"⚫ *{worker['name']}* — logged off for the day\n"
            f"login {login_str} → EOD {eod_str} ({worker['tz']})\n"
            f"{period_line}"
        )

    # Working or on break
    if state == "on_break" and break_start_ts:
        break_min = int((datetime.now(timezone.utc) - break_start_ts).total_seconds() / 60)
        header = f"🟡 *{worker['name']}* — on break ({break_min} min so far)"
    else:
        header = f"🟢 *{worker['name']}* — currently working"

    last_ck_line = ""
    if last_checkin_ts and last_checkin_msg:
        mins_ago = int((datetime.now(timezone.utc) - last_checkin_ts).total_seconds() / 60)
        last_ck_line = f"\nlast check-in ({mins_ago} min ago): \"{last_checkin_msg.strip()}\""
    elif last_checkin_ts is None:
        last_ck_line = "\nno check-in messages yet today (just logged in)"

    return (
        f"{header}\n"
        f"clocked in at {login_str} {worker['tz']}"
        f"{last_ck_line}\n"
        f"{period_line}"
    )


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
    log.info("[EVENT RECEIVED] type=%s channel_type=%s user=%s text=%r subtype=%s bot_id=%s",
             event.get("type"), event.get("channel_type"), event.get("user"),
             event.get("text"), event.get("subtype"), event.get("bot_id"))
    if event.get("channel_type") != "im":
        log.info("  -> ignored (not a DM)")
        return
    if event.get("bot_id") or event.get("subtype"):
        log.info("  -> ignored (from bot or has subtype)")
        return

    user_id = event.get("user")
    text = (event.get("text") or "").strip()
    if not user_id:
        log.info("  -> ignored (no user_id)")
        return
    log.info("  -> handling as worker DM from %s", user_id)

    # Admin commands — owners (Jan/Ideen) full power, managers (Hannah) can query non-owners.
    is_owner = user_id in config.OWNER_SLACK_IDS
    is_manager = user_id in config.MANAGER_SLACK_IDS
    is_admin = is_owner or is_manager

    if is_admin:
        # "what is X doing" / "status of X" / etc.
        m = _admin_status_re.search(text)
        if m:
            query = next((g for g in m.groups() if g), "").strip()
            query = re.sub(r"\s+(?:doing|up|working|going|online|on|here)$", "", query, flags=re.IGNORECASE).strip()
            matches = _find_worker_by_name(query)
            if not matches:
                client.chat_postMessage(channel=user_id, text=f"don't know anyone named '{query}' on the roster — try just their first name?")
                return
            if len(matches) > 1:
                names = ", ".join(w["name"] for w in matches)
                client.chat_postMessage(channel=user_id, text=f"multiple matches for '{query}': {names}. ask again with a more specific name?")
                return
            target = matches[0]
            # Managers can't query owners
            if is_manager and not is_owner and target["user_id"] in config.OWNER_SLACK_IDS:
                client.chat_postMessage(channel=user_id, text=f"sorry, can't share that with you 🙅 status on {target['name'].split()[0]} is owner-level only.")
                return
            try:
                snapshot = _format_worker_status(target)
                client.chat_postMessage(channel=user_id, text=snapshot)
            except Exception as e:
                log.exception("status snapshot failed for %s", target["name"])
                client.chat_postMessage(channel=user_id, text=f"hit an error checking on {target['name']}: {e}")
            return
    # 'introduce everyone' — owners only (managers can't broadcast intros)
    if is_owner and _admin_intro_re.search(text):
        try:
            client.chat_postMessage(channel=user_id, text="on it — DMing everyone who hasn't been introduced yet… 🚀")
        except Exception:
            pass
        try:
            worker_results = onboarding.send_introductions()
            owner_results = onboarding.send_owner_introductions()
            sent_workers = [n for n, s in worker_results.items() if s == "sent"]
            sent_owners = [s for s in owner_results.values() if s.startswith("sent")]
            skipped = [n for n, s in worker_results.items() if "skipped" in s] + \
                      [k for k, s in owner_results.items() if "skipped" in s]
            failed = [(n, s) for n, s in {**worker_results, **owner_results}.items() if "failed" in s]
            summary_lines = [f"done! 🎉"]
            if sent_workers:
                summary_lines.append(f"workers intro'd ({len(sent_workers)}):")
                for n in sent_workers:
                    summary_lines.append(f"  ✓ {n}")
            if sent_owners:
                summary_lines.append(f"\nowners intro'd ({len(sent_owners)}):")
                for s in sent_owners:
                    summary_lines.append(f"  ✓ {s.replace('sent (', '').rstrip(')')}")
            if skipped:
                summary_lines.append(f"\nskipped (already introduced): {len(skipped)}")
            if failed:
                summary_lines.append("\nfailed:")
                for n, s in failed:
                    summary_lines.append(f"  ✗ {n} — {s}")
            client.chat_postMessage(channel=user_id, text="\n".join(summary_lines))
        except Exception as e:
            log.exception("admin introduce command failed")
            try:
                client.chat_postMessage(channel=user_id, text=f"hit an error running intros: {e}")
            except Exception:
                pass
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
        break_note = f" ({summary['break_hours']}h on break)" if summary.get("break_hours") else ""
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"alright {first}, you're out 👋 {summary['active_hours']}h active{break_note}, "
                f"{len(summary['checkins'])} check-ins. catch you tomorrow.\n\n"
                f"if those hours look wrong (missed a break, missed a login, etc.) just message me "
                f"with details — I'll flag it for review."
            ),
        )
        return

    # Worker asking for their current pay-period hours
    if is_hours_query(text):
        try:
            client.chat_postMessage(channel=user_id, text=_format_hours_summary(worker))
        except Exception:
            log.exception("hours summary failed for %s", user_id)
        return

    # Worker flagging an hours discrepancy — log it for manager review
    if is_discrepancy(text):
        sheets.append_event(worker["name"], user_id, "hours_discrepancy", text, worker["tz"])
        try:
            client.chat_postMessage(
                channel=user_id,
                text=(
                    f"got it {first} — I logged that as a discrepancy for Jan to review before "
                    f"payroll runs. add anything else if you want more context."
                ),
            )
        except Exception:
            pass
        # Don't return — also treat as a normal check-in (it's still activity)

    if LOGGED_IN_TODAY.get(user_id) != today:
        LOGGED_IN_TODAY[user_id] = today
        sheets.append_event(worker["name"], user_id, "login", text, worker["tz"])
        schedule_next_prompt(user_id)
        interval = _interval_for(user_id)
        hours = interval // 60
        mins = interval % 60
        cadence = f"{hours}h" if mins == 0 else f"{hours}h{mins}m"

        # Auto-create personal view sheet on first login if they don't have one
        view_url = worker.get("personal_view_url")
        view_intro = ""
        if not view_url:
            try:
                view_url = worker_views.ensure_view_for_worker(worker)
                if view_url:
                    # Update in-memory roster so subsequent messages skip this
                    worker["personal_view_url"] = view_url
                    WORKERS[user_id] = worker
                    view_intro = (
                        f"\n\nalso made you a personal sheet where you can see your own hours "
                        f"and pay history anytime: {view_url}\n"
                        f"first time you open it, you'll see a yellow banner — click 'Allow access' "
                        f"and your data shows up. only you can see this sheet."
                    )
            except Exception:
                log.exception("Failed to provision view sheet for %s", worker["name"])

        # On the very first login (no profile yet), ask their usual schedule.
        # On subsequent logins, skip — we already have it.
        schedule_q = ""
        if not sheets.load_profile(user_id):
            schedule_q = "\n\nbtw what's your usual schedule? roughly when do you start and wrap up most days?"

        client.chat_postMessage(
            channel=user_id,
            text=(
                f"hey {first}! got you in 🙌 I'll loop back every {cadence} to see "
                f"how things are going. shoot me 'EOD' whenever you wrap up."
                + view_intro
                + schedule_q
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
    elif is_admin:
        # Admins (Jan, Ideen, Hannah) get conversational replies for anything
        # that didn't match a specific command. Workers stay in the structured flow.
        try:
            reply = analyzer.conversational_reply(
                message=text,
                speaker_name=worker["name"],
                is_owner=is_owner,
                is_manager=is_manager and not is_owner,
            )
            if reply:
                client.chat_postMessage(channel=user_id, text=reply)
        except Exception:
            log.exception("conversational reply failed for %s", worker["name"])

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


def send_pre_payroll_review_dms() -> None:
    """DM every active worker their current pay-period totals, asking them to
    flag any discrepancies before payroll runs tomorrow. Fired the evening
    before payday (14th + last-day-of-month for semimonthly schedule).
    """
    if _app is None:
        return
    reload_roster()
    for user_id, worker in WORKERS.items():
        try:
            text = _format_hours_summary(worker)
            # Add a more pointed prompt since payroll is imminent
            text += "\n\npayroll runs tomorrow morning — reply by then if anything's off 🙏"
            _app.client.chat_postMessage(channel=user_id, text=text)
            sheets.append_event(worker["name"], user_id, "pre_payroll_review", "", worker["tz"])
        except Exception:
            log.exception("Pre-payroll DM failed for %s", worker["name"])


def schedule_pre_payroll_reviews() -> None:
    """For semimonthly: DM workers on the 14th and last day of month at PRE_PAYROLL_REVIEW_TIME."""
    if config.PAYROLL_PERIOD != "semimonthly":
        return
    hh, mm = map(int, config.PRE_PAYROLL_REVIEW_TIME.split(":"))
    tz = ZoneInfo(config.MANAGER_TZ)
    scheduler.add_job(
        send_pre_payroll_review_dms, "cron",
        day=15, hour=hh, minute=mm, timezone=tz,
        id="prepayroll_review_15",
    )
    scheduler.add_job(
        send_pre_payroll_review_dms, "cron",
        day="last", hour=hh, minute=mm, timezone=tz,
        id="prepayroll_review_eom",
    )
    log.info("Pre-payroll review DMs scheduled: 15th + last-of-month at %s %s", config.PRE_PAYROLL_REVIEW_TIME, config.MANAGER_TZ)


def schedule_payroll() -> None:
    if config.PAYROLL_PERIOD == "none":
        log.info("PAYROLL_PERIOD=none, payroll cron disabled")
        return
    hh, mm = map(int, config.PAYROLL_RUN_TIME.split(":"))
    tz = ZoneInfo(config.MANAGER_TZ)
    if config.PAYROLL_PERIOD == "semimonthly":
        # Pay on the 1st (covers prior 16th-EOM) and 16th (covers 1st-15th).
        for day in (1, 16):
            scheduler.add_job(
                report.run_and_send_payroll, "cron",
                day=day, hour=hh, minute=mm, timezone=tz,
                id=f"payroll_day{day}",
            )
        log.info("Payroll cron scheduled: 1st + 16th at %s %s", config.PAYROLL_RUN_TIME, config.MANAGER_TZ)
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
    schedule_pre_payroll_reviews()
    log.info("Starting Socket Mode handler. Ctrl-C to stop.")
    SocketModeHandler(_app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
