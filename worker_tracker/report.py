"""EOD digest: per-worker summary written to Sheet + emailed to manager."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

import yagmail

from . import config, sheets, analyzer

log = logging.getLogger(__name__)


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _hours(t0: datetime | None, t1: datetime | None) -> float:
    if not t0 or not t1:
        return 0.0
    return round((t1 - t0).total_seconds() / 3600.0, 2)


def collect_worker_day(worker: dict, local_date: str) -> dict:
    rows = [r for r in sheets.activity_rows(local_date) if r.get("Slack User ID") == worker["user_id"]]
    rows.sort(key=lambda r: r.get("Timestamp UTC", ""))

    login_ts = eod_ts = None
    checkins: List[tuple[datetime, str]] = []
    help_reqs: List[tuple[datetime, str]] = []
    missed = 0
    for r in rows:
        ts = _parse_ts(r["Timestamp UTC"])
        t = r["Type"]
        msg = r.get("Message", "")
        if t == "login" and not login_ts:
            login_ts = ts
        elif t == "eod":
            eod_ts = ts
        elif t == "checkin":
            checkins.append((ts, msg))
        elif t == "help_request":
            help_reqs.append((ts, msg))
            checkins.append((ts, msg))
        elif t == "missed_checkin":
            missed += 1

    try:
        tz = ZoneInfo(worker["tz"])
    except Exception:
        tz = ZoneInfo("UTC")

    def hhmm(t: datetime | None) -> str:
        return t.astimezone(tz).strftime("%H:%M") if t else "—"

    end_for_hours = eod_ts or (datetime.now(timezone.utc) if login_ts else None)
    active = _hours(login_ts, end_for_hours)

    status = "OK"
    notes: List[str] = []
    if help_reqs:
        status = "Needs help"
        notes.append(f"{len(help_reqs)} help req(s)")
    if missed >= 2:
        if status == "OK":
            status = "Possible slack"
        notes.append(f"{missed} missed prompt(s)")
    if checkins:
        avg_len = sum(len(m) for _, m in checkins) / len(checkins)
        if avg_len < 25 and status == "OK":
            status = "Possible slack"
            notes.append(f"avg reply {avg_len:.0f} chars")
    if not eod_ts and login_ts:
        notes.append("no EOD signal")
    if active > 10:
        notes.append("overworking?")
    if active and active < 2 and eod_ts:
        notes.append("short day")

    local_checkins = [(t.astimezone(tz), m) for t, m in checkins]
    local_help = [(t.astimezone(tz), m) for t, m in help_reqs]

    profile = sheets.load_profile(worker["user_id"])
    ai = analyzer.analyze(
        name=worker["name"],
        login_local=hhmm(login_ts),
        eod_local=hhmm(eod_ts),
        active_hours=active,
        help_count=len(help_reqs),
        missed=missed,
        checkins=local_checkins,
        profile=profile,
    )

    return {
        "worker": worker["name"],
        "date": local_date,
        "login_local": hhmm(login_ts),
        "eod_local": hhmm(eod_ts),
        "active_hours": active,
        "checkins": local_checkins,
        "help_reqs": local_help,
        "missed": missed,
        "status": status,
        "notes": "; ".join(notes),
        "day_summary": ai["day_summary"],
        "automation_opportunities": ai["automation_opportunities"],
        "manual_red_flags": ai["manual_red_flags"],
        "capacity_signal": ai["capacity_signal"],
        "profile": profile,
    }


def write_worker_summary(worker: dict) -> dict:
    """Write one worker's daily summary row. Called when they EOD."""
    try:
        tz = ZoneInfo(worker["tz"])
    except Exception:
        tz = ZoneInfo("UTC")
    local_date = datetime.now(tz).date().isoformat()
    s = collect_worker_day(worker, local_date)
    sheets.append_summary([
        s["date"], s["worker"], s["login_local"], s["eod_local"],
        s["active_hours"], len(s["checkins"]), len(s["help_reqs"]), s["missed"],
        s["status"], s["notes"],
        s["day_summary"],
        " • ".join(s["automation_opportunities"]),
        " • ".join(s["manual_red_flags"]),
        s["capacity_signal"],
    ])
    return s


def _color(status: str) -> str:
    return {
        "OK": "#2e7d32",
        "Needs help": "#c62828",
        "Possible slack": "#ef6c00",
    }.get(status, "#444")


def _capacity_badge(signal: str) -> str:
    colors = {
        "spare capacity": ("#1565c0", "🟦"),
        "balanced": ("#2e7d32", "🟩"),
        "stretched": ("#ef6c00", "🟧"),
        "stuck": ("#c62828", "🟥"),
    }
    c, _ = colors.get(signal, ("#666", "⬜"))
    label = signal.upper() if signal else "—"
    return f"<span style='background:{c};color:#fff;padding:2px 8px;border-radius:10px;font-size:80%'>{label}</span>"


def _bullets(items: list[str]) -> str:
    if not items:
        return "<i style='color:#888'>none</i>"
    return "<ul style='margin:4px 0 4px 18px;padding:0'>" + "".join(
        f"<li>{x}</li>" for x in items
    ) + "</ul>"


def build_html(date_str: str, sections: List[dict]) -> str:
    parts = [f"<h2 style='font-family:sans-serif'>Worker Tracker — {date_str}</h2>"]
    if not sections:
        parts.append("<p><i>No activity today.</i></p>")
    for s in sections:
        ckl = "".join(
            f"<li><b>{t.strftime('%H:%M')}</b> — {m or '<i>(empty)</i>'}</li>"
            for t, m in s["checkins"]
        ) or "<li><i>no replies recorded</i></li>"
        summary_block = (
            f"<p style='margin:6px 0;color:#222'><b>Summary:</b> {s['day_summary']}</p>"
            if s["day_summary"] else ""
        )
        profile_block = ""
        p = s.get("profile") or {}
        open_autos = p.get("Automation Opportunities (Open)") or ""
        blockers = p.get("Known Blockers / Skill Gaps") or ""
        if open_autos or blockers:
            bits = []
            if open_autos:
                bits.append(f"<b>Open automation backlog:</b> <span style='color:#555'>{open_autos}</span>")
            if blockers:
                bits.append(f"<b>Known skill gaps:</b> <span style='color:#555'>{blockers}</span>")
            profile_block = (
                f"<details style='margin-top:4px;color:#444;font-size:90%'>"
                f"<summary style='cursor:pointer'>From their profile</summary>"
                f"<p style='margin:4px 0'>{'<br>'.join(bits)}</p></details>"
            )
        parts.append(
            f"<div style='font-family:sans-serif;border-left:4px solid {_color(s['status'])};"
            f"padding:8px 14px;margin:14px 0;background:#fafafa'>"
            f"<h3 style='margin:0 0 4px 0'>{s['worker']} "
            f"<span style='color:{_color(s['status'])}'>· {s['status']}</span> "
            f"{_capacity_badge(s['capacity_signal'])}</h3>"
            f"<p style='margin:0;color:#444'>Login {s['login_local']} → EOD {s['eod_local']} · "
            f"<b>{s['active_hours']}h</b> · "
            f"{len(s['checkins'])} check-ins · {len(s['help_reqs'])} help · {s['missed']} missed</p>"
            f"{summary_block}"
            f"{profile_block}"
            f"<details style='margin-top:6px'><summary style='cursor:pointer;color:#555'>Check-in replies</summary>"
            f"<ul style='margin:6px 0'>{ckl}</ul></details>"
            f"<p style='margin:8px 0 2px 0;color:#1565c0'><b>Could be automated:</b></p>{_bullets(s['automation_opportunities'])}"
            f"<p style='margin:8px 0 2px 0;color:#ef6c00'><b>Manual grind / red flags:</b></p>{_bullets(s['manual_red_flags'])}"
            f"<p style='margin:4px 0;color:#a00;font-size:90%'>{s['notes']}</p>"
            f"</div>"
        )
    return "".join(parts)


def run_weekly_synthesis() -> None:
    """Rebuild every active worker's profile from the past 7 days of activity.
    Sends a brief email summary of profile changes.
    """
    roster = sheets.load_roster()
    if not roster:
        log.info("Weekly synth: roster empty, nothing to do")
        return

    updated = []
    for w in roster:
        prior = sheets.load_profile(w["user_id"])
        recent_activity = sheets.activity_since(7, slack_user_id=w["user_id"])
        first_seen = (prior or {}).get("First Seen") or (
            min((r.get("Local Date") for r in recent_activity if r.get("Local Date")), default="")
        )
        # daily summaries for this worker over last 7 days
        all_summaries = sheets.open_tracker().worksheet(config.SUMMARY_TAB).get_all_records()
        recent_summaries = [
            r for r in all_summaries
            if r.get("Worker") == w["name"]
        ][-7:]

        new_profile = analyzer.synthesize_weekly_profile(
            name=w["name"],
            slack_user_id=w["user_id"],
            first_seen=first_seen,
            prior_profile=prior,
            recent_summaries=recent_summaries,
            recent_activity=recent_activity,
        )
        sheets.upsert_profile(new_profile)
        updated.append((w, prior, new_profile))
        log.info("Updated profile for %s", w["name"])

    if not config.GMAIL_USER or not config.GMAIL_APP_PASSWORD:
        return
    today = datetime.now(ZoneInfo(config.MANAGER_TZ)).date().isoformat()
    parts = [f"<h2 style='font-family:sans-serif'>Weekly Worker Profiles — {today}</h2>"]
    for w, prior, new in updated:
        parts.append(
            f"<div style='font-family:sans-serif;border-left:4px solid #1565c0;"
            f"padding:8px 14px;margin:14px 0;background:#fafafa'>"
            f"<h3 style='margin:0 0 4px 0'>{w['name']}</h3>"
            f"<p style='margin:4px 0'><b>Role:</b> {new.get('Role / What They Do', '')}</p>"
            f"<p style='margin:4px 0'><b>Recurring tasks:</b> {new.get('Recurring Tasks', '')}</p>"
            f"<p style='margin:4px 0'><b>Strengths:</b> {new.get('Known Strengths', '')}</p>"
            f"<p style='margin:4px 0'><b>Blockers / gaps:</b> {new.get('Known Blockers / Skill Gaps', '')}</p>"
            f"<p style='margin:4px 0;color:#1565c0'><b>Open automation backlog:</b> {new.get('Automation Opportunities (Open)', '')}</p>"
            f"<p style='margin:4px 0;color:#2e7d32'><b>Recently shipped:</b> {new.get('Automation Opportunities (Shipped)', '')}</p>"
            f"<p style='margin:4px 0'><b>Patterns:</b> {new.get('Productivity Patterns', '')}</p>"
            f"<p style='margin:4px 0;color:#ef6c00'><b>For you (coaching notes):</b> {new.get('Coaching Notes for Manager', '')}</p>"
            f"</div>"
        )
    yag = yagmail.SMTP(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
    yag.send(to=config.REPORT_RECIPIENT, subject=f"Weekly Worker Profiles — {today}", contents="".join(parts))
    log.info("Sent weekly profile digest")


def send_daily_digest() -> None:
    if not config.GMAIL_USER or not config.GMAIL_APP_PASSWORD:
        log.warning("Gmail not configured; skipping email digest")
        return
    mgr_tz = ZoneInfo(config.MANAGER_TZ)
    today_local = datetime.now(mgr_tz).date().isoformat()
    roster = sheets.load_roster()
    sections = []
    for w in roster:
        s = collect_worker_day(w, today_local)
        if s["login_local"] == "—":
            continue
        sections.append(s)
    html = build_html(today_local, sections)
    yag = yagmail.SMTP(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
    yag.send(to=config.REPORT_RECIPIENT, subject=f"Worker Tracker EOD — {today_local}", contents=html)
    log.info("Sent EOD digest to %s (%d workers)", config.REPORT_RECIPIENT, len(sections))
