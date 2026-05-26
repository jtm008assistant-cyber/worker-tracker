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

from . import config, sheets, report, analyzer, payroll, worker_views, onboarding, memory, drive_audit

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
_admin_time_off_re = re.compile("|".join(config.ADMIN_TIME_OFF_PATTERNS), re.IGNORECASE)
_admin_benefits_query_re = re.compile("|".join(config.ADMIN_BENEFITS_QUERY_PATTERNS), re.IGNORECASE)
_timeoff_balance_query_re = re.compile("|".join(config.TIMEOFF_BALANCE_QUERY_PATTERNS), re.IGNORECASE)
_admin_drive_audit_re = re.compile("|".join(config.ADMIN_DRIVE_AUDIT_PATTERNS), re.IGNORECASE)
_admin_forward_re = re.compile("|".join(config.ADMIN_FORWARD_PATTERNS), re.IGNORECASE | re.DOTALL)
_admin_relay_re = re.compile("|".join(config.ADMIN_RELAY_PATTERNS), re.IGNORECASE)

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

# Manager is in this set after Sam DMs the daily planning question; cleared
# once their reply is parsed and assignments are saved.
PENDING_DAILY_PLANNING: set[str] = set()

# Hannah (or whoever) is in this set after Sam DMs the benefits collection
# question; cleared once their reply is parsed and benefits saved to Roster.
PENDING_BENEFITS_REPLY: set[str] = set()


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
    """Fuzzy match a name query against the roster. Tries (1) full name exact,
    (2) first-name exact, (3) nickname exact, (4) substring. Returns list of
    matching worker dicts."""
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
    # 3) nickname exact match (from Roster Nicknames col)
    nick_match = [w for w in workers if q in (w.get("nicknames") or [])]
    if nick_match:
        return nick_match
    # 4) substring anywhere in full name
    substr = [w for w in workers if q in w["name"].lower()]
    return substr


def _worker_today_snapshot(worker: dict) -> dict:
    """Compute today's open-session state + hours-so-far for one worker.
    Returns dict with: state, login_ts, eod_ts, last_checkin_ts, last_checkin_msg,
    break_start_ts, hours_so_far_today, today_checkins (list of strings).
    """
    try:
        tz = ZoneInfo(worker["tz"])
    except Exception:
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date().isoformat()
    rows = [r for r in sheets.activity_rows(today) if r.get("Slack User ID") == worker["user_id"]]
    rows.sort(key=lambda r: r.get("Timestamp UTC", ""))

    login_ts = eod_ts = last_checkin_ts = None
    last_checkin_msg = ""
    break_start_ts = None
    state = "not_started"
    today_checkins: list[str] = []
    total_break_seconds = 0.0
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
            if break_start_ts:
                total_break_seconds += (ts - break_start_ts).total_seconds()
                break_start_ts = None
        elif t in ("checkin", "help_request"):
            last_checkin_ts = ts
            last_checkin_msg = msg
            if msg.strip():
                today_checkins.append(msg.strip()[:300])
        elif t == "break_start":
            break_start_ts = ts
            state = "on_break"
        elif t == "break_end":
            if break_start_ts:
                total_break_seconds += (ts - break_start_ts).total_seconds()
            break_start_ts = None
            state = "working"

    # Compute hours so far today
    hours_so_far = 0.0
    if login_ts:
        end_ts = eod_ts or datetime.now(timezone.utc)
        elapsed = (end_ts - login_ts).total_seconds()
        break_seconds = total_break_seconds
        # If currently on break, count time-on-break-so-far too
        if break_start_ts and state == "on_break":
            break_seconds += (datetime.now(timezone.utc) - break_start_ts).total_seconds()
        hours_so_far = max(0.0, round((elapsed - break_seconds) / 3600.0, 2))

    return {
        "tz": worker.get("tz", "UTC"),
        "state": state,
        "login_ts": login_ts,
        "eod_ts": eod_ts,
        "last_checkin_ts": last_checkin_ts,
        "last_checkin_msg": last_checkin_msg,
        "break_start_ts": break_start_ts,
        "hours_so_far_today": hours_so_far,
        "today_checkins": today_checkins,
    }


def _format_worker_status(worker: dict) -> str:
    """Snapshot of where a worker currently is right now. Reads today's
    Activity Log and pay-period summary, AND includes today's open-session
    hours (Daily Summary alone misses mid-shift work)."""
    from . import payroll as _payroll
    snap = _worker_today_snapshot(worker)
    state = snap["state"]
    login_ts = snap["login_ts"]
    eod_ts = snap["eod_ts"]
    last_checkin_ts = snap["last_checkin_ts"]
    last_checkin_msg = snap["last_checkin_msg"]
    break_start_ts = snap["break_start_ts"]
    hours_so_far = snap["hours_so_far_today"]
    try:
        tz = ZoneInfo(worker["tz"])
    except Exception:
        tz = ZoneInfo("UTC")

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

    # Get pay-period totals — past closed days come from Daily Summary; today's
    # open session is added on top via hours_so_far (since Daily Summary only
    # gets written at EOD).
    try:
        start, end = _payroll.current_open_period()
        totals = _payroll.worker_period_totals(worker, start, end)
        # If they're working today, today's hours aren't yet in Daily Summary —
        # add them so the displayed total is the real "hours so far this period"
        period_total = totals["total_hours"] + (hours_so_far if state != "logged_off" else 0)
        period_line = (
            f"pay period ({start} → {end}): *{period_total:.2f}h* total "
            f"({totals['total_hours']:.2f}h from previous days + {hours_so_far:.2f}h today)"
        )
    except Exception:
        period_line = ""

    login_str = login_ts.astimezone(tz).strftime("%H:%M") if login_ts else "—"
    if state == "logged_off":
        eod_str = eod_ts.astimezone(tz).strftime("%H:%M") if eod_ts else "—"
        return (
            f"⚫ *{worker['name']}* — logged off for the day\n"
            f"login {login_str} → EOD {eod_str} ({worker['tz']}) · {hours_so_far:.2f}h today\n"
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
        f"clocked in at {login_str} {worker['tz']} · {hours_so_far:.2f}h on the clock today"
        f"{last_ck_line}\n"
        f"{period_line}"
    )


def _build_team_state_for_admin() -> str:
    """Build a compact team state block to feed into admin conversational replies.
    Lets Sam answer follow-up questions like 'why did Hannah log 0 hours' or
    'is Rey done yet' without the admin re-asking 'status of X'.
    """
    if not WORKERS:
        return "(no workers loaded yet)"
    from datetime import datetime as _dt
    now_mgr = _dt.now(ZoneInfo(config.MANAGER_TZ))
    lines = [f"CURRENT TEAM STATE (manager-local time {now_mgr.strftime('%Y-%m-%d %H:%M')} {config.MANAGER_TZ}):"]
    for w in WORKERS.values():
        if w["user_id"] in config.OWNER_SLACK_IDS:
            continue  # owners aren't tracked as workers
        try:
            snap = _worker_today_snapshot(w)
        except Exception:
            continue
        tz = w.get("tz", "UTC")
        try:
            tzi = ZoneInfo(tz)
        except Exception:
            tzi = ZoneInfo("UTC")
        login_str = snap["login_ts"].astimezone(tzi).strftime("%H:%M") if snap["login_ts"] else "—"
        state = snap["state"]
        h_today = snap["hours_so_far_today"]
        bits = [f"- {w['name']} ({tz}, state={state}, today={h_today:.2f}h"]
        if snap["login_ts"]:
            bits.append(f", login={login_str}")
        if state == "on_break" and snap["break_start_ts"]:
            mins = int((datetime.now(timezone.utc) - snap["break_start_ts"]).total_seconds() / 60)
            bits.append(f", on break {mins}min")
        if snap["last_checkin_msg"]:
            mins = int((datetime.now(timezone.utc) - snap["last_checkin_ts"]).total_seconds() / 60) if snap["last_checkin_ts"] else 0
            bits.append(f", last check-in {mins}m ago: \"{snap['last_checkin_msg'][:200]}\"")
        bits.append(")")
        lines.append("".join(bits))
    # Note about period totals
    lines.append("")
    lines.append("Note: 'today=Xh' includes ALL hours worked so far today (open session counted, "
                 "breaks subtracted). Daily Summary only gets written at EOD, so before EOD the bot "
                 "calculates hours-so-far from login + break events on the Activity Log.")
    return "\n".join(lines)


def _dm(client, user_id: str, text: str, event_type: str = "sam_reply") -> bool:
    """Send a Slack DM AND log it to Activity Log. Single source of truth for
    outbound messages — so the Activity Log captures BOTH sides of every convo.
    Returns True on send success."""
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except Exception:
        log.exception("Outbound DM failed to %s (type=%s)", user_id, event_type)
        return False
    # Best-effort logging — never block on a sheet write
    try:
        worker = WORKERS.get(user_id)
        if worker:
            name = worker["name"]
            tz = worker["tz"]
        else:
            # Likely Ideen or another non-roster user (e.g. owner)
            name = user_id
            tz = config.MANAGER_TZ
        # Truncate to keep cells reasonable
        sheets.append_event(name, user_id, event_type, text[:500], tz)
    except Exception:
        log.exception("Failed to log outbound event for %s", user_id)
    return True


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

    # Try a Gemini-generated contextual prompt that references the worker's
    # most recent check-in AND any aging open commitments. Falls back to the
    # generic prompt if Gemini fails.
    text = None
    try:
        today = _local_today(worker)
        today_events = [r for r in sheets.activity_rows(today) if r.get("Slack User ID") == user_id]
        today_events.sort(key=lambda r: r.get("Timestamp UTC", ""))
        open_commits = sheets.list_open_commitments(user_id)
        text = analyzer.generate_checkin_prompt(worker, today_events, open_commitments=open_commits)
    except Exception:
        log.exception("contextual check-in prompt failed for %s; falling back", user_id)

    if not text:
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

    # Daily planning reply — fires only if this user was sent the planning question.
    # Check BEFORE admin-command parsing so a free-form planning reply doesn't
    # accidentally route through admin patterns. But check AFTER the admin-routing
    # block so admin commands still work during the planning window — done below.

    # Deferred relay — "when ger logs in tell her to do X" — needs to come
    # BEFORE the immediate forward / status checks because the trigger words
    # overlap ("tell ger ..."). If we recognize a deferred-relay phrasing, we
    # queue it instead of relaying right away.
    if is_admin and _admin_relay_re.search(text):
        if _handle_deferred_relay(user_id, text, client):
            return

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
    # 'send to X: message' / 'tell norks his hours look off' — admin relay
    if is_admin:
        fm = _admin_forward_re.match(text)
        if fm:
            groups = [g for g in fm.groups() if g]
            if len(groups) >= 2:
                target_query = groups[0].strip()
                message_to_send = groups[1].strip()
                matches = _find_worker_by_name(target_query)
                if not matches:
                    client.chat_postMessage(channel=user_id, text=f"don't know who '{target_query}' is — check the roster?")
                    return
                if len(matches) > 1:
                    names = ", ".join(w["name"] for w in matches)
                    client.chat_postMessage(channel=user_id, text=f"multiple matches for '{target_query}': {names}. be more specific?")
                    return
                target = matches[0]
                # Managers can't relay to owners
                if is_manager and not is_owner and target["user_id"] in config.OWNER_SLACK_IDS:
                    client.chat_postMessage(channel=user_id, text=f"can't relay messages to {target['name'].split()[0]} — owner-level only.")
                    return
                try:
                    sender_name = WORKERS[user_id]["name"] if user_id in WORKERS else user_id
                    client.chat_postMessage(channel=target["user_id"], text=message_to_send)
                    sheets.append_event(target["name"], target["user_id"], "admin_forward",
                                       f"from {sender_name}: {message_to_send[:100]}", target["tz"])
                    client.chat_postMessage(channel=user_id, text=f"✓ sent to {target['name']}")
                except Exception as e:
                    log.exception("admin forward failed")
                    client.chat_postMessage(channel=user_id, text=f"failed to send: {e}")
                return

    # Planning reply — if we asked this person the planning question and they
    # didn't match an admin command above, treat their message as the answer.
    if _handle_planning_reply(user_id, text, client):
        return

    # Benefits reply — if Sam asked this person about worker benefits, parse it.
    if _handle_benefits_reply(user_id, text, client):
        return

    # Admin command: log time off ("vacation for hannah dec 1-5", etc.)
    if is_admin and _admin_time_off_re.search(text):
        _handle_log_time_off(user_id, text, client, worker)
        return

    # Admin command: ask Hannah (or named manager) about benefits
    if is_owner and _admin_benefits_query_re.search(text):
        _handle_benefits_collection_request(user_id, text, client)
        return

    # Owner command: audit the drive (gold/decent/stale/garbage/orphan)
    if is_owner and _admin_drive_audit_re.search(text):
        _handle_drive_audit(user_id, client)
        return

    # Worker query: balance check (vacation/sick/pto days left)
    if _timeoff_balance_query_re.search(text):
        _handle_balance_query(user_id, client, worker)
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
            _dm(
                client, user_id,
                f"hey, I'm Sam 👋 I help the team stay in sync on what everyone's "
                f"working on. I don't have you on my list yet though — send your "
                f"manager your Slack ID so they can add you: `{user_id}`",
                event_type="sam_not_on_roster",
            )
            return

    worker = WORKERS[user_id]
    today = _local_today(worker)
    first = worker["name"].split()[0] if worker["name"] else "you"

    # Capture whether this message is a direct reply to Sam's periodic prompt.
    # If yes, ALWAYS acknowledge (silence after asking a question = rude).
    was_responding_to_prompt = user_id in PROMPT_PENDING
    PROMPT_PENDING.pop(user_id, None)
    try:
        scheduler.remove_job(f"miss:{user_id}")
    except Exception:
        pass

    # --- Break handling ---
    # Recovery: if Sam was offline when the worker started a break, ON_BREAK is
    # empty even though Activity Log shows an open break_start. If this message
    # is an explicit break-end keyword, scan today's log for an unclosed break
    # and hydrate ON_BREAK from it so the normal resume branch below runs.
    if user_id not in ON_BREAK and is_break_end(text):
        try:
            today_local = _local_today(worker)
            rows = [r for r in sheets.activity_rows(today_local)
                    if r.get("Slack User ID") == user_id]
            rows.sort(key=lambda r: r.get("Timestamp UTC", ""))
            unclosed_break_ts = None
            for r in rows:
                t = r.get("Type")
                if t == "break_start":
                    try:
                        ts = datetime.fromisoformat(str(r.get("Timestamp UTC", "")))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        unclosed_break_ts = ts
                    except Exception:
                        pass
                elif t in ("break_end", "login", "eod"):
                    unclosed_break_ts = None
            if unclosed_break_ts is not None:
                ON_BREAK[user_id] = unclosed_break_ts
                log.info("Recovered unclosed break for %s from log", worker["name"])
        except Exception:
            log.exception("Break recovery scan failed for %s", user_id)

    # If they're already on break, any message resumes them (unless it's another
    # break-start keyword, in which case just remind them they're still paused).
    if user_id in ON_BREAK:
        if is_break_start(text) and not is_break_end(text) and not is_eod(text):
            _dm(client, user_id, "already paused — message me when you're back 👌",
                event_type="sam_break_reminder")
            return
        break_start_ts = ON_BREAK.pop(user_id)
        duration_min = (datetime.now(timezone.utc) - break_start_ts).total_seconds() / 60
        sheets.append_event(
            worker["name"], user_id, "break_end",
            f"break duration: {duration_min:.0f}min",
            worker["tz"],
        )
        _dm(client, user_id,
            f"welcome back {first}! that was a {duration_min:.0f}min break — back on the clock 🙌",
            event_type="sam_resume_ack")
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
            _dm(client, user_id,
                f"got it {first} — paused the clock 🛑 message me anything when you're back",
                event_type="sam_break_ack")
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
        _dm(client, user_id,
            f"alright {first}, you're out 👋 {summary['active_hours']}h active{break_note}, "
            f"{len(summary['checkins'])} check-ins. catch you tomorrow.\n\n"
            f"if those hours look wrong (missed a break, missed a login, etc.) just message me "
            f"with details — I'll flag it for review.",
            event_type="sam_eod_ack")
        return

    # Worker asking for their current pay-period hours
    if is_hours_query(text):
        try:
            _dm(client, user_id, _format_hours_summary(worker), event_type="sam_hours_summary")
        except Exception:
            log.exception("hours summary failed for %s", user_id)
        return

    # Worker flagging an hours discrepancy — log it for manager review
    if is_discrepancy(text):
        sheets.append_event(worker["name"], user_id, "hours_discrepancy", text, worker["tz"])
        _dm(client, user_id,
            f"got it {first} — I logged that as a discrepancy for Jan to review before "
            f"payroll runs. add anything else if you want more context.",
            event_type="sam_discrepancy_ack")
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
            schedule_q = " btw, what's your usual schedule? roughly when do you start and wrap up most days?"

        # If Jan sent a focus assignment for this worker last evening, relay it.
        # Otherwise fall back to the open-ended "what's on your plate" question.
        assignment = _latest_assignment_for(user_id)
        if assignment and assignment.lower() not in ("continue",):
            plate_q = (
                f"\n\nJan asked you to focus on this today:\n> {assignment}\n\n"
                f"message me when you start working on it."
            )
        elif assignment and assignment.lower() == "continue":
            plate_q = (
                f"\n\nJan said to continue what you were already working on. "
                f"quick reminder of what that is so I can track it?"
            )
        else:
            plate_q = "\n\nso what's on your plate today? give me a quick idea of what you're tackling."

        _dm(client, user_id,
            f"hey {first}! got you in 🙌 I'll loop back every {cadence} to see "
            f"how things are going. shoot me 'EOD' whenever you wrap up."
            + view_intro
            + plate_q
            + schedule_q,
            event_type="sam_welcome")

        # Any deferred relays an admin queued for this worker get delivered
        # right after the welcome. We send a SECOND DM so the relay reads
        # as its own message, not buried in the schedule/plate ask.
        try:
            pending = sheets.list_pending_relays_for_worker(user_id)
            if pending:
                _deliver_relay(worker, pending, client)
        except Exception:
            log.exception("Relay delivery on login failed for %s", user_id)
        return

    ev_type = "help_request" if has_help(text) else "checkin"
    sheets.append_event(worker["name"], user_id, ev_type, text, worker["tz"])
    schedule_next_prompt(user_id)

    # Follow-up questions ride on the check-in cadence — only fire when this
    # message is a reply to Sam's periodic prompt. Outside that window, Sam
    # stays quiet about new tools so workers aren't getting random pings.
    _followup_eligible = was_responding_to_prompt

    if ev_type == "help_request":
        _dm(client, user_id,
            "noted — flagging this for the manager. drop more detail if you need it sooner.",
            event_type="sam_help_ack")
    else:
        # Everyone gets a real conversational reply for messages that didn't match a
        # specific command. If they were responding to Sam's periodic check-in
        # prompt, ALWAYS acknowledge — even a simple 'thanks for the update' so
        # they know Sam saw it. Otherwise respect Gemini's SKIP judgment.
        # For admins, Sam gets FULL memory: current state, 7d activity, profiles,
        # knowledge base, time off, today's assignments, conversation history.
        try:
            if is_admin:
                team_state = memory.build_admin_memory(
                    user_id, list(WORKERS.values()), _worker_today_snapshot,
                    message_text=text,
                )
            else:
                team_state = ""
            reply = analyzer.conversational_reply(
                message=text,
                speaker_name=worker["name"],
                is_owner=is_owner,
                is_manager=is_manager and not is_owner,
                is_worker=not (is_owner or is_manager),
                team_state=team_state,
            )
            if not reply and was_responding_to_prompt:
                # Gemini SKIPped but we MUST ack — fallback canned thanks
                reply = f"thanks for the update {first}! 🙌"
            if reply:
                _dm(client, user_id, reply, event_type="sam_chat")
        except Exception:
            log.exception("conversational reply failed for %s", worker["name"])
            # On exception during prompt-reply, fall back to canned ack so Sam isn't silent
            if was_responding_to_prompt:
                _dm(client, user_id, f"thanks for the update {first}! 🙌",
                    event_type="sam_chat_fallback")

    # Cross-day relay completion: if an admin queued "tell ger to fix X" and
    # ger now says "done with the listing", mark the relay done + DM the admin.
    # Cheap early-out inside the helper if the worker has no delivered relays.
    try:
        _check_and_notify_relay_completion(worker, text, client)
    except Exception:
        log.exception("Relay completion check failed for %s", user_id)

    # --- Knowledge / follow-up handling ---
    # Knowledge extraction (saving URLs / tool answers from pending follow-ups)
    # ALWAYS runs — if Sam already asked something, we save whatever the worker
    # replies regardless of timing.
    # NEW follow-up questions only fire when the worker is replying to Sam's
    # periodic check-in prompt — keeps Sam's curiosity on the check-in cadence.
    try:
        _handle_knowledge_and_followup(
            user_id, worker, text, today, client,
            ask_new_followups=_followup_eligible,
        )
    except Exception:
        log.exception("knowledge/follow-up step failed for %s", user_id)


def _reset_followups_if_new_day(user_id: str, local_date: str) -> None:
    state = FOLLOWUPS_TODAY.get(user_id)
    if not state or state.get("date") != local_date:
        FOLLOWUPS_TODAY[user_id] = {"date": local_date, "count": 0, "topics": []}


def _handle_knowledge_and_followup(user_id: str, worker: dict, text: str,
                                   today: str, client,
                                   ask_new_followups: bool = True) -> None:
    """Two-step:
    1) If we asked a follow-up earlier and they're now replying, extract any
       tools/sheets/processes from the reply and save to the Knowledge tab.
       (Always runs — if Sam asked, Sam saves the answer.)
    2) If `ask_new_followups` is True, maybe ask a NEW follow-up about
       something the worker mentioned. Gated by daily cap + cooldown.
       (Caller passes False when the worker isn't replying to Sam's
       periodic check-in prompt — keeps Sam's curiosity on cadence.)
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
            _dm(client, user_id,
                f"saved 🙌 (logged {len(items)} new {'item' if len(items)==1 else 'items'} to your tools list — thanks)",
                event_type="sam_knowledge_saved")
            # Refresh existing so we don't double-ask about something we just learned
            existing = sheets.list_worker_knowledge(user_id)

    if not ask_new_followups:
        return

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

    if not _dm(client, user_id, decision["ask"], event_type="sam_followup_question"):
        return
    PENDING_FOLLOWUP[user_id] = {"topic": decision["topic"], "asked_at": datetime.now(timezone.utc)}
    state["count"] += 1
    state["topics"].append(decision["topic"])
    LAST_FOLLOWUP_AT[user_id] = datetime.now(timezone.utc)


def restore_state() -> None:
    """On startup: any worker logged in today w/ no EOD → resume their schedule.
    Also restores ON_BREAK so a deploy mid-break doesn't lose the worker's
    pause state (the in-memory dict gets wiped on restart — without this, a
    'back' message after a redeploy would not clock them back in).
    """
    for user_id, worker in WORKERS.items():
        today = _local_today(worker)
        rows = [r for r in sheets.activity_rows(today) if r.get("Slack User ID") == user_id]
        rows.sort(key=lambda r: r.get("Timestamp UTC", ""))
        logged_in = False
        on_break_since: datetime | None = None
        for r in rows:
            t = r.get("Type")
            if t == "login":
                logged_in = True
                on_break_since = None  # new login closes any stale break state
            elif t == "eod":
                logged_in = False
                on_break_since = None
            elif t == "break_start":
                try:
                    ts = datetime.fromisoformat(str(r.get("Timestamp UTC", "")))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    on_break_since = ts
                except Exception:
                    pass
            elif t == "break_end":
                on_break_since = None
        if logged_in:
            LOGGED_IN_TODAY[user_id] = today
            if on_break_since is not None:
                ON_BREAK[user_id] = on_break_since
                log.info("Restored ON_BREAK for %s (since %s)", worker["name"], on_break_since.isoformat())
            else:
                schedule_next_prompt(user_id)
            log.info("Resumed active session for %s (on_break=%s)",
                     worker["name"], on_break_since is not None)


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


def send_daily_planning_question() -> None:
    """DM the manager (Jan by default) asking what each worker should focus on tomorrow.
    Their reply gets parsed by Gemini and saved as daily_assignment events; on the
    next login, Sam relays the assignment to each worker.
    """
    if _app is None or not config.DAILY_PLANNING_SLACK_ID:
        return
    reload_roster()
    workers = [w for w in WORKERS.values() if w["user_id"] not in config.OWNER_SLACK_IDS]
    if not workers:
        return

    lines = [
        "hey 👋 quick planning question for tomorrow's shifts.",
        "",
        "what should each worker focus on?",
        "",
    ]
    for w in workers:
        first = w["name"].split()[0]
        lines.append(f"• *{first}* — ?")
    lines.append("")
    lines.append(
        "reply with assignments per worker (e.g. \"jonny: finish the SKU audit. "
        "hannah: continue. norks: review the new product photos.\"). "
        "or just say *\"all continue\"* if nothing changes."
    )

    try:
        _app.client.chat_postMessage(channel=config.DAILY_PLANNING_SLACK_ID, text="\n".join(lines))
        PENDING_DAILY_PLANNING.add(config.DAILY_PLANNING_SLACK_ID)
        log.info("Sent daily planning question to %s", config.DAILY_PLANNING_SLACK_ID)
    except Exception:
        log.exception("Failed to send daily planning question")


def schedule_daily_planning() -> None:
    if not config.DAILY_PLANNING_SLACK_ID:
        return
    hh, mm = map(int, config.DAILY_PLANNING_TIME.split(":"))
    scheduler.add_job(
        send_daily_planning_question, "cron",
        hour=hh, minute=mm,
        timezone=ZoneInfo(config.MANAGER_TZ),
        id="daily_planning",
    )
    log.info("Daily planning question scheduled %s %s", config.DAILY_PLANNING_TIME, config.MANAGER_TZ)


def _handle_planning_reply(user_id: str, text: str, client) -> bool:
    """If `user_id` was sent a planning question and this looks like their reply,
    parse it via Gemini and save per-worker daily_assignment events.
    Returns True if handled (caller should return early)."""
    if user_id not in PENDING_DAILY_PLANNING:
        return False
    # Heuristic: if message is suspiciously short (one word), probably not the planning answer
    if len(text.strip()) < 3:
        return False

    reload_roster()
    roster = list(WORKERS.values())
    assignments = analyzer.parse_daily_assignments(text, roster)
    PENDING_DAILY_PLANNING.discard(user_id)

    if not assignments:
        # 'all continue' or unparseable — acknowledge and move on
        try:
            client.chat_postMessage(channel=user_id, text="got it — keeping everyone on their current work 👍")
        except Exception:
            pass
        return True

    # Save per worker
    saved = []
    for w in roster:
        if w["name"] in assignments:
            asg = assignments[w["name"]]
            sheets.append_event(w["name"], w["user_id"], "daily_assignment", asg, w["tz"])
            saved.append(f"{w['name'].split()[0]}: {asg[:60]}")

    try:
        body = "saved 🙌 here's what i'll relay to each worker on their next login:\n\n" + \
               "\n".join(f"• {s}" for s in saved)
        client.chat_postMessage(channel=user_id, text=body)
    except Exception:
        pass
    return True


def _handle_drive_audit(admin_user_id: str, client) -> None:
    """Run a full drive audit and DM the classified file list back to the admin."""
    _dm(client, admin_user_id, "running drive audit — give me ~10 sec while I scan everything…",
        event_type="sam_audit_starting")
    try:
        result = drive_audit.audit()
        text = drive_audit.format_audit_summary(result)
    except Exception as e:
        log.exception("Drive audit failed")
        _dm(client, admin_user_id, f"hit an error running the audit: {e}",
            event_type="sam_audit_failed")
        return
    # Slack message limit is ~3800 chars per message — split if needed
    chunks: list[str] = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        if current_len + len(line) > 3500 and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    for i, chunk in enumerate(chunks):
        _dm(client, admin_user_id, chunk, event_type="sam_audit_result")


def _handle_log_time_off(admin_user_id: str, text: str, client, worker: dict) -> None:
    """Admin says 'log vacation for hannah dec 1-5' — parse, write to Time Off tab, ack."""
    reload_roster()
    roster = list(WORKERS.values())
    from datetime import datetime as _dt
    today_iso = _dt.now(ZoneInfo(config.MANAGER_TZ)).date().isoformat()
    parsed = analyzer.parse_time_off_log(text, roster, today_iso)
    if not parsed or not parsed.get("worker_name"):
        _dm(client, admin_user_id,
            "couldn't parse that — try something like \"log vacation for hannah dec 1-5\" or "
            "\"sick day for rey today\"",
            event_type="sam_time_off_parse_failed")
        return

    # Find the target worker dict for their Slack ID
    target = next((w for w in roster if w["name"] == parsed["worker_name"]), None)
    if not target:
        _dm(client, admin_user_id,
            f"i couldn't match \"{parsed['worker_name']}\" to anyone on the roster",
            event_type="sam_time_off_no_match")
        return

    sheets.append_time_off([
        _dt.now(timezone.utc).isoformat(timespec="seconds"),
        target["name"], target["user_id"],
        parsed["type"],
        parsed["start_date"], parsed["end_date"],
        parsed["days"],
        "approved",  # admin-logged entries are pre-approved
        worker["name"],  # logged by
        parsed["notes"],
    ])
    summary = (f"logged: {target['name'].split()[0]} — {parsed['type']} "
               f"{parsed['start_date']}"
               + (f" → {parsed['end_date']}" if parsed['end_date'] != parsed['start_date'] else "")
               + f" ({parsed['days']} days)")
    _dm(client, admin_user_id, f"✓ {summary}", event_type="sam_time_off_logged")
    # Optional: notify the worker
    try:
        first = target["name"].split()[0]
        _dm(client, target["user_id"],
            f"heads up {first} — Jan logged {parsed['days']} day(s) of {parsed['type']} for you "
            f"({parsed['start_date']}"
            + (f" → {parsed['end_date']}" if parsed['end_date'] != parsed['start_date'] else "")
            + "). all approved. enjoy 🙌",
            event_type="sam_time_off_notify")
    except Exception:
        log.exception("Failed to notify worker about logged time off")


def _handle_benefits_collection_request(admin_user_id: str, text: str, client) -> None:
    """Admin says 'ask hannah about benefits' — DM Hannah the collection question."""
    # Find Hannah specifically (manager) — or fall back to the first manager on the roster
    reload_roster()
    target = None
    # Look for a manager (Hannah by default)
    for uid in config.MANAGER_SLACK_IDS:
        w = WORKERS.get(uid)
        if w:
            target = w
            break
    if not target:
        _dm(client, admin_user_id,
            "no manager on the roster to ask. add someone to MANAGER_SLACK_IDS first.",
            event_type="sam_no_manager")
        return

    # Build the question with each worker name
    workers = [w for w in WORKERS.values() if w["user_id"] not in config.OWNER_SLACK_IDS
               and w["user_id"] != target["user_id"]]
    lines = [
        f"hey {target['name'].split()[0]} 👋 Jan asked me to collect benefits info on the team.",
        "",
        "for each of these workers, what's their annual allocation?",
        "(vacation days, sick days, PTO days — or 'unlimited', or 'contractor — no benefits')",
        "",
    ]
    for w in workers:
        lines.append(f"• *{w['name'].split()[0]}*")
    lines.append("")
    lines.append("reply however's easiest — e.g. \"jonny: 10 vacation 5 sick. hannah: 15/10. "
                 "rey: contractor, no PTO. ger: 10/5. janina: 10/5. norks: 10/5.\"")
    if _dm(client, target["user_id"], "\n".join(lines), event_type="sam_benefits_question"):
        PENDING_BENEFITS_REPLY.add(target["user_id"])
        _dm(client, admin_user_id,
            f"sent the benefits collection question to {target['name'].split()[0]}. "
            f"i'll save what they tell me to the Roster + ping you when done.",
            event_type="sam_benefits_request_ack")


def _handle_benefits_reply(user_id: str, text: str, client) -> bool:
    """If user_id was sent the benefits question, parse and save to Roster."""
    if user_id not in PENDING_BENEFITS_REPLY:
        return False
    if len(text.strip()) < 5:
        return False

    reload_roster()
    roster = list(WORKERS.values())
    parsed = analyzer.parse_benefits_reply(text, roster)
    PENDING_BENEFITS_REPLY.discard(user_id)

    if not parsed:
        _dm(client, user_id,
            "couldn't quite parse that — could you try again with format like "
            "\"jonny: 10 vacation 5 sick. hannah: 15/10. rey: contractor.\"",
            event_type="sam_benefits_parse_failed")
        PENDING_BENEFITS_REPLY.add(user_id)  # re-add so they can try again
        return True

    # Write back to Roster
    ws = sheets.open_tracker().worksheet(config.ROSTER_TAB)
    rows = ws.get_all_values()
    header = rows[0]
    name_col = header.index("Name") + 1
    vac_col = header.index("Vacation Days/Year") + 1
    sick_col = header.index("Sick Days/Year") + 1
    pto_col = header.index("PTO Days/Year") + 1
    notes_col = header.index("Benefits Notes") + 1

    saved = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) < name_col:
            continue
        name = r[name_col - 1].strip()
        if name in parsed:
            b = parsed[name]
            ws.update_cell(i, vac_col, b["vacation_days"])
            ws.update_cell(i, sick_col, b["sick_days"])
            ws.update_cell(i, pto_col, b["pto_days"])
            ws.update_cell(i, notes_col, b["notes"])
            saved.append(f"{name.split()[0]}: {b['vacation_days']}v/{b['sick_days']}s/{b['pto_days']}p")

    _dm(client, user_id,
        "saved 🙌 here's what i recorded:\n\n" + "\n".join(f"• {s}" for s in saved) +
        "\n\nthanks — I'll use this when workers ask about their balance.",
        event_type="sam_benefits_saved")

    # Notify Jan
    if config.OWNER_SLACK_IDS:
        try:
            _dm(client, config.OWNER_SLACK_IDS[0],
                "FYI — benefits info just landed from Hannah:\n\n" + "\n".join(f"• {s}" for s in saved),
                event_type="sam_benefits_collected_notify")
        except Exception:
            pass
    return True


def _handle_balance_query(user_id: str, client, worker: dict) -> None:
    """Worker asks 'how many vacation days do i have left' — calc from Roster + Time Off."""
    from datetime import datetime as _dt
    year = _dt.now(ZoneInfo(worker.get("tz") or "UTC")).year
    first = worker["name"].split()[0]
    allocations = {
        "vacation": int(worker.get("vacation_days_year") or 0),
        "sick": int(worker.get("sick_days_year") or 0),
        "pto": int(worker.get("pto_days_year") or 0),
    }
    if sum(allocations.values()) == 0 and not (worker.get("benefits_notes") or "").strip():
        _dm(client, user_id,
            f"hey {first} — I don't have your benefits info on file yet. Jan or Hannah "
            f"will set that up soon. for now you can still request time off and they'll handle it.",
            event_type="sam_balance_unknown")
        return

    used = {"vacation": 0, "sick": 0, "pto": 0, "personal": 0, "unpaid": 0, "holiday": 0}
    for r in sheets.time_off_for_worker(user_id, year=year):
        t = (r.get("Type") or "").strip().lower()
        if t in used:
            try:
                used[t] += int(r.get("Days") or 0)
            except (TypeError, ValueError):
                pass

    notes = worker.get("benefits_notes") or ""
    lines = [f"hey {first}, here's your {year} balance:"]
    for kind in ("vacation", "sick", "pto"):
        alloc = allocations.get(kind, 0)
        u = used.get(kind, 0)
        if alloc or u:
            lines.append(f"• *{kind}*: {alloc - u} left ({u} used of {alloc})")
    if notes:
        lines.append(f"\n_note: {notes}_")
    lines.append("\nlet me know if you want to request time off and I'll route it to Jan.")
    _dm(client, user_id, "\n".join(lines), event_type="sam_balance_reply")


def _handle_deferred_relay(admin_user_id: str, text: str, client) -> bool:
    """Admin asked Sam to deliver a message to a worker the next time they
    log in. Parse it, queue to Relay Queue, and if the worker is already
    clocked in TODAY, deliver immediately.

    Returns True if a relay was queued (so the caller stops further routing).
    """
    reload_roster()
    roster = list(WORKERS.values())
    parsed = analyzer.parse_relay_request(text, roster)
    if not parsed:
        # Gemini couldn't pin down a worker / task — let other handlers try.
        return False

    target_uid = parsed["slack_user_id"]
    target = WORKERS.get(target_uid)
    if not target:
        # Sometimes the roster key has changed since Gemini parsed it; bail.
        _dm(client, admin_user_id,
            f"caught a 'when they log in' message but couldn't find {parsed.get('worker_name')} on the active roster — double-check the name?",
            event_type="sam_relay_unmatched")
        return True

    # Managers can't relay to owners
    is_owner = admin_user_id in config.OWNER_SLACK_IDS
    is_manager = admin_user_id in config.MANAGER_SLACK_IDS
    if is_manager and not is_owner and target_uid in config.OWNER_SLACK_IDS:
        _dm(client, admin_user_id,
            f"can't queue messages for {target['name'].split()[0]} — owner-level only.",
            event_type="sam_relay_denied")
        return True

    import uuid
    from datetime import datetime as _dt
    relay_id = "r-" + uuid.uuid4().hex[:8]
    now_iso = _dt.now(timezone.utc).isoformat(timespec="seconds")
    sender_name = WORKERS[admin_user_id]["name"] if admin_user_id in WORKERS else admin_user_id

    relay_row = [
        relay_id, now_iso, sender_name, admin_user_id,
        target["name"], target_uid,
        parsed["message"], parsed.get("estimated_time", ""),
        "pending", "", "", "", "",
    ]
    try:
        sheets.append_relay(relay_row)
    except Exception:
        log.exception("Failed to queue relay")
        _dm(client, admin_user_id, f"queue write failed — try again in a sec?",
            event_type="sam_relay_queue_failed")
        return True

    first_target = target["name"].split()[0]
    est_blurb = f" (~{parsed['estimated_time']})" if parsed.get("estimated_time") else ""

    # If the worker is currently clocked in today, deliver right now instead of waiting.
    today_local = _local_today(target)
    already_on = LOGGED_IN_TODAY.get(target_uid) == today_local
    if already_on:
        try:
            _deliver_relay(target, [{"Relay ID": relay_id, "Message": parsed["message"],
                                       "Estimated Time": parsed.get("estimated_time", ""),
                                       "From Name": sender_name}], client)
            _dm(client, admin_user_id,
                f"✓ {first_target} is already online — I sent it now{est_blurb}. I'll ping you when she confirms it's done.",
                event_type="sam_relay_delivered_now")
        except Exception:
            log.exception("Immediate relay delivery failed for %s", target_uid)
            _dm(client, admin_user_id,
                f"queued for {first_target}, but the immediate delivery hit an error — it'll fall back to next login.",
                event_type="sam_relay_partial")
    else:
        _dm(client, admin_user_id,
            f"✓ got it — I'll tell {first_target} when she next logs in{est_blurb}. I'll ping you the moment she confirms it's done.",
            event_type="sam_relay_queued")
    return True


def _deliver_relay(worker: dict, relays: list[dict], client) -> None:
    """DM a worker about one or more pending relays at login. Marks each as
    delivered on the Relay Queue tab. Best-effort — failures are logged but
    don't block the rest of the welcome flow."""
    if not relays:
        return
    first = worker["name"].split()[0]
    user_id = worker["user_id"]
    if len(relays) == 1:
        r = relays[0]
        msg = r.get("Message", "").strip()
        est = (r.get("Estimated Time") or "").strip()
        from_name = (r.get("From Name") or "").strip().split()[0] or "Jan"
        est_blurb = f" (should only take ~{est})" if est else ""
        body = (
            f"hey {first} — quick one from {from_name} before you dive in:\n"
            f"> {msg}{est_blurb}\n\n"
            f"shoot me a message when you've handled it and I'll let {from_name} know."
        )
    else:
        bullets = []
        for r in relays:
            msg = r.get("Message", "").strip()
            est = (r.get("Estimated Time") or "").strip()
            from_name = (r.get("From Name") or "").strip().split()[0] or "Jan"
            suffix = f" (~{est})" if est else ""
            bullets.append(f"• from {from_name}: {msg}{suffix}")
        body = (
            f"hey {first} — couple of things waiting for you:\n"
            + "\n".join(bullets)
            + "\n\nlet me know as you knock them out so I can update the requesters."
        )
    _dm(client, user_id, body, event_type="sam_relay_deliver")
    for r in relays:
        rid = r.get("Relay ID")
        if rid:
            try:
                sheets.mark_relay_delivered(rid)
            except Exception:
                log.exception("mark_relay_delivered failed for %s", rid)


def _check_and_notify_relay_completion(worker: dict, reply_text: str, client) -> None:
    """After a worker replies, see if they're confirming any delivered relays
    as done. For each completion, mark the row done and DM the requesting
    admin with the worker's quote.

    Cheap early-out: skip Gemini if the worker has no delivered relays.
    """
    if not reply_text or len(reply_text.strip()) < 4:
        return
    try:
        open_relays = sheets.list_delivered_relays_for_worker(worker["user_id"])
    except Exception:
        log.exception("list_delivered_relays_for_worker failed")
        return
    if not open_relays:
        return

    try:
        completions = analyzer.check_relay_completion(reply_text, open_relays)
    except Exception:
        log.exception("check_relay_completion failed")
        return
    if not completions:
        return

    relays_by_id = {r.get("Relay ID"): r for r in open_relays}
    for comp in completions:
        rid = comp["relay_id"]
        relay = relays_by_id.get(rid)
        if not relay:
            continue
        quote = comp.get("quote", "")
        try:
            sheets.mark_relay_done(rid, worker_reply=quote, notes="")
        except Exception:
            log.exception("mark_relay_done failed for %s", rid)
            continue
        # Notify the admin who requested it
        admin_uid = (relay.get("From Slack ID") or "").strip()
        if not admin_uid:
            continue
        worker_first = worker["name"].split()[0]
        ask = (relay.get("Message") or "").strip()
        quote_blurb = f"\n> {quote}" if quote else ""
        try:
            _dm(client, admin_uid,
                f"✓ {worker_first} just confirmed: \"{ask}\"{quote_blurb}",
                event_type="sam_relay_complete")
        except Exception:
            log.exception("Failed to notify admin of relay completion")


def _latest_assignment_for(user_id: str) -> str | None:
    """Look up the most recent daily_assignment for this worker (last 36 hours).
    Returns the assignment text, or None."""
    try:
        rows = sheets.activity_since(2, slack_user_id=user_id)
    except Exception:
        return None
    asgs = [r for r in rows if r.get("Type") == "daily_assignment"]
    if not asgs:
        return None
    asgs.sort(key=lambda r: r.get("Timestamp UTC", ""))
    return asgs[-1].get("Message", "").strip() or None


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
    schedule_daily_planning()
    log.info("Starting Socket Mode handler. Ctrl-C to stop.")
    SocketModeHandler(_app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
