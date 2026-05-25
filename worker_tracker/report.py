"""EOD digest: per-worker summary written to Sheet + emailed to manager."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

import yagmail

from . import config, sheets, analyzer, payroll

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
    break_hours = 0.0
    open_break_start: datetime | None = None
    for r in rows:
        ts = _parse_ts(r["Timestamp UTC"])
        t = r["Type"]
        msg = r.get("Message", "")
        if t == "login" and not login_ts:
            login_ts = ts
        elif t == "eod":
            eod_ts = ts
            if open_break_start:
                break_hours += (ts - open_break_start).total_seconds() / 3600
                open_break_start = None
        elif t == "checkin":
            checkins.append((ts, msg))
        elif t == "help_request":
            help_reqs.append((ts, msg))
            checkins.append((ts, msg))
        elif t == "missed_checkin":
            missed += 1
        elif t == "break_start":
            open_break_start = ts
        elif t == "break_end":
            if open_break_start:
                break_hours += (ts - open_break_start).total_seconds() / 3600
                open_break_start = None
    # If we end the day still on break (no break_end recorded), don't count
    # the open break — most likely they forgot. Cap at "until now or EOD".
    if open_break_start and eod_ts:
        break_hours += (eod_ts - open_break_start).total_seconds() / 3600

    try:
        tz = ZoneInfo(worker["tz"])
    except Exception:
        tz = ZoneInfo("UTC")

    def hhmm(t: datetime | None) -> str:
        return t.astimezone(tz).strftime("%H:%M") if t else "—"

    end_for_hours = eod_ts or (datetime.now(timezone.utc) if login_ts else None)
    raw_hours = _hours(login_ts, end_for_hours)
    active = max(0.0, round(raw_hours - break_hours, 2))
    break_hours = round(break_hours, 2)

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
    knowledge = sheets.list_worker_knowledge(worker["user_id"])
    ai = analyzer.analyze(
        name=worker["name"],
        login_local=hhmm(login_ts),
        eod_local=hhmm(eod_ts),
        active_hours=active,
        help_count=len(help_reqs),
        missed=missed,
        checkins=local_checkins,
        profile=profile,
        knowledge=knowledge,
    )

    return {
        "worker": worker["name"],
        "date": local_date,
        "login_local": hhmm(login_ts),
        "eod_local": hhmm(eod_ts),
        "active_hours": active,
        "break_hours": break_hours,
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
    notes = s["notes"]
    if s.get("break_hours"):
        notes = (notes + "; " if notes else "") + f"{s['break_hours']}h on break"
    sheets.append_summary([
        s["date"], s["worker"], s["login_local"], s["eod_local"],
        s["active_hours"], len(s["checkins"]), len(s["help_reqs"]), s["missed"],
        s["status"], notes,
        s["day_summary"],
        " • ".join(s["automation_opportunities"]),
        " • ".join(s["manual_red_flags"]),
        s["capacity_signal"],
    ])
    return s


def send_payroll_digest(results: list[dict]) -> None:
    """Email the bookkeeper/manager a payroll summary for the just-closed period.
    Goes to PAYROLL_RECIPIENT (can be comma-separated for multiple recipients).
    """
    if not results:
        log.info("Payroll: no results, skipping email")
        return
    if not config.GMAIL_USER or not config.GMAIL_APP_PASSWORD:
        log.warning("Gmail not configured; skipping payroll email")
        return

    start = results[0]["period_start"].isoformat()
    end = results[0]["period_end"].isoformat()
    total = sum(r["calc"]["gross_pay"] for r in results)
    currency = results[0]["worker"].get("currency", config.PAYROLL_DEFAULT_CURRENCY)

    # Summary table
    rows_html = []
    for r in results:
        w = r["worker"]
        c = r["calc"]
        rate = w.get("hourly_rate") or 0
        ot_note = f"({c['regular_hours']}h reg + {c['overtime_hours']}h OT)" if c["overtime_hours"] else ""
        pay_type = w.get("pay_type", "hourly")
        rate_str = f"{currency} {rate}/h" if pay_type == "hourly" else f"{currency} {w.get('salary_per_period', 0)} salary"
        rows_html.append(
            f"<tr>"
            f"<td>{w['name']}</td>"
            f"<td>{pay_type}</td>"
            f"<td>{c['days_worked']}</td>"
            f"<td>{c['total_hours']}h {ot_note}</td>"
            f"<td>{rate_str}</td>"
            f"<td><b>{currency} {c['gross_pay']:,.2f}</b></td>"
            f"</tr>"
        )

    # Per-worker daily breakdown (paste-ready for bookkeeper)
    daily_html = []
    for r in results:
        w = r["worker"]
        c = r["calc"]
        pay_type = w.get("pay_type", "hourly")
        rate = float(w.get("hourly_rate") or 0)
        day_rows = []
        for row in r.get("daily_rows", []):
            d = str(row.get("Date", ""))
            try:
                dow = datetime.fromisoformat(d).strftime("%a")
            except Exception:
                dow = ""
            hours = row.get("Active Hours", "")
            login_l = row.get("Login (local)", "")
            eod_l = row.get("EOD (local)", "")
            notes = row.get("Notes", "")
            try:
                pay_for_day = f"{currency} {float(hours) * rate:,.2f}" if pay_type == "hourly" and hours else ""
            except (TypeError, ValueError):
                pay_for_day = ""
            day_rows.append(
                f"<tr>"
                f"<td>{d}</td><td>{dow}</td>"
                f"<td>{login_l} → {eod_l}</td>"
                f"<td>{hours}h</td>"
                f"<td>{pay_for_day}</td>"
                f"<td style='color:#888'>{notes}</td>"
                f"</tr>"
            )
        salary_line = ""
        if pay_type == "salaried":
            salary_line = f"<p style='margin:6px 0;color:#1565c0'><b>Salary this period: {currency} {w.get('salary_per_period', 0):,.2f}</b></p>"
        daily_html.append(
            f"<div style='border-left:4px solid #1565c0;padding:8px 14px;margin:18px 0;background:#fafafa'>"
            f"<h3 style='margin:0 0 4px 0'>{w['name']} — {currency} {c['gross_pay']:,.2f}</h3>"
            f"<p style='margin:2px 0;color:#444'>{c['days_worked']} days worked · {c['total_hours']}h total"
            + (f" ({c['regular_hours']}h reg + {c['overtime_hours']}h OT × {w.get('ot_multiplier', 1.5)})" if c["overtime_hours"] else "")
            + "</p>"
            f"{salary_line}"
            f"<table cellpadding='6' style='border-collapse:collapse;font-family:sans-serif;font-size:13px;margin-top:8px;width:100%'>"
            f"<tr style='background:#eee'><th>Date</th><th>Day</th><th>In → Out</th><th>Hours</th><th>Day Pay</th><th>Notes</th></tr>"
            f"{''.join(day_rows)}"
            f"</table></div>"
        )

    html = (
        f"<h2 style='font-family:sans-serif'>Payroll — {start} → {end}</h2>"
        f"<p style='font-family:sans-serif;color:#555'>Auto-generated. Detail in the Timesheet tab of the Payroll sheet.</p>"

        f"<h3 style='font-family:sans-serif;margin-top:18px'>Summary</h3>"
        f"<table border='1' cellpadding='8' style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
        f"<tr style='background:#222;color:#fff'>"
        f"<th>Worker</th><th>Pay Type</th><th>Days</th><th>Hours</th><th>Rate / Salary</th><th>Gross</th>"
        f"</tr>"
        f"{''.join(rows_html)}"
        f"</table>"
        f"<p style='font-family:sans-serif;font-size:130%;margin:14px 0'>"
        f"Total payout: <b>{currency} {total:,.2f}</b></p>"

        f"<h3 style='font-family:sans-serif;margin-top:24px'>Per-worker daily breakdown</h3>"
        f"{''.join(daily_html)}"
    )

    recipients = [e.strip() for e in str(config.PAYROLL_RECIPIENT or "").split(",") if e.strip()]
    if not recipients:
        log.warning("PAYROLL_RECIPIENT empty, skipping email")
        return

    yag = yagmail.SMTP(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
    yag.send(
        to=recipients,
        subject=f"Payroll {start} → {end} — {currency} {total:,.2f}",
        contents=html,
    )
    log.info("Payroll digest emailed to %s (%d workers, %s %.2f total)",
             recipients, len(results), currency, total)


def run_and_send_payroll() -> None:
    """Cron entry point: run payroll for the just-closed period and email it."""
    results = payroll.run_payroll()
    send_payroll_digest(results)


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
            f"<b>{s['active_hours']}h</b>"
            + (f" <span style='color:#888'>({s['break_hours']}h on break)</span>" if s.get('break_hours') else "")
            + f" · {len(s['checkins'])} check-ins · {len(s['help_reqs'])} help · {s['missed']} missed</p>"
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
