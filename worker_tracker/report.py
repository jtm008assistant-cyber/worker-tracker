"""EOD digest: per-worker summary written to Sheet + emailed to manager."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

import yagmail

from . import config, sheets, analyzer, payroll, fx

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
    open_commitments = sheets.list_open_commitments(worker["user_id"])
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
        open_commitments=open_commitments,
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
        "new_commitments": ai.get("new_commitments", []),
        "resolved_commitments": ai.get("resolved_commitments", []),
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

    # Persist any new commitments detected today
    today_iso = datetime.now(timezone.utc).date().isoformat()
    for c in (s.get("new_commitments") or []):
        try:
            sheets.append_commitment([
                today_iso, worker["name"], worker["user_id"],
                c.get("commitment", ""),
                c.get("mentioned_person", ""),
                "open", "", "",
            ])
        except Exception:
            log.exception("Failed to log commitment for %s", worker["name"])
    # Mark resolved commitments
    for txt in (s.get("resolved_commitments") or []):
        try:
            sheets.mark_commitment_status(
                worker["user_id"], txt, "done",
                resolution_notes=f"detected as done in {local_date} check-ins",
            )
        except Exception:
            log.exception("Failed to mark resolved commitment '%s' for %s", txt, worker["name"])

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
    currency = results[0]["worker"].get("currency", config.PAYROLL_DEFAULT_CURRENCY)

    # Per-currency totals + USD-equivalent grand total
    rates = fx.get_rates()
    per_ccy_totals: dict[str, float] = {}
    total_usd = 0.0
    for r in results:
        ccy = (r["worker"].get("currency") or config.PAYROLL_DEFAULT_CURRENCY).upper()
        gross = float(r["calc"]["gross_pay"])
        per_ccy_totals[ccy] = per_ccy_totals.get(ccy, 0.0) + gross
        total_usd += fx.to_usd(gross, ccy, rates)
    total = total_usd  # used for subject line

    # Pull any discrepancy reports workers flagged during this period
    all_activity = sheets.activity_rows(None)
    discrepancies = [
        r for r in all_activity
        if r.get("Type") == "hours_discrepancy"
        and start <= str(r.get("Local Date", "")) <= end
    ]

    # Summary table
    rows_html = []
    for r in results:
        w = r["worker"]
        c = r["calc"]
        rate = w.get("hourly_rate") or 0
        ot_note = f"({c['regular_hours']}h reg + {c['overtime_hours']}h OT)" if c["overtime_hours"] else ""
        pay_type = w.get("pay_type", "hourly")
        rate_str = f"{currency} {rate}/h" if pay_type == "hourly" else f"{currency} {w.get('salary_per_period', 0)} salary"
        payout_email = w.get("payout_email") or "<i style='color:#a00'>missing — confirm before paying</i>"
        payout_method = (w.get("payout_method") or "wise").lower()
        rows_html.append(
            f"<tr>"
            f"<td>{w['name']}</td>"
            f"<td><b>{payout_method}</b><br><code>{payout_email}</code></td>"
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

    discrepancy_html = ""
    if discrepancies:
        d_rows = "".join(
            f"<tr><td style='vertical-align:top'>{d.get('Local Date', '')}</td>"
            f"<td style='vertical-align:top'><b>{d.get('Worker', '')}</b></td>"
            f"<td>{d.get('Message', '')}</td></tr>"
            for d in discrepancies
        )
        discrepancy_html = (
            f"<div style='background:#fff3e0;border-left:5px solid #ef6c00;"
            f"padding:12px 18px;margin:14px 0;font-family:sans-serif'>"
            f"<h3 style='margin:0 0 8px 0;color:#bf360c'>⚠️ {len(discrepancies)} hours discrepancy "
            f"report(s) flagged this period — review before paying</h3>"
            f"<table cellpadding='6' style='border-collapse:collapse;font-size:14px;width:100%'>"
            f"<tr style='background:#ffe0b2'><th align='left'>Date</th><th align='left'>Worker</th>"
            f"<th align='left'>What they said</th></tr>"
            f"{d_rows}"
            f"</table></div>"
        )

    per_ccy_str = " · ".join(f"{ccy} {amt:,.2f}" for ccy, amt in sorted(per_ccy_totals.items()))
    usd_total_block = (
        f"<p style='font-family:sans-serif;font-size:130%;margin:14px 0'>"
        f"Total payout (USD equivalent): <b>USD {total_usd:,.2f}</b><br>"
        f"<span style='font-size:75%;color:#666'>By currency: {per_ccy_str}</span></p>"
    )

    html = (
        f"<h2 style='font-family:sans-serif'>Payroll — {start} → {end}</h2>"
        f"<p style='font-family:sans-serif;color:#555'>Auto-generated. Detail in the Timesheet tab of the Payroll sheet.</p>"
        f"{discrepancy_html}"
        f"<h3 style='font-family:sans-serif;margin-top:18px'>Summary</h3>"
        f"<table border='1' cellpadding='8' style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
        f"<tr style='background:#222;color:#fff'>"
        f"<th>Worker</th><th>Payout (method + email)</th><th>Pay Type</th><th>Days</th><th>Hours</th><th>Rate / Salary</th><th>Gross</th>"
        f"</tr>"
        f"{''.join(rows_html)}"
        f"</table>"
        f"{usd_total_block}"
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
        subject=f"Payroll {start} → {end} — USD {total_usd:,.2f}",
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
    DMs a Slack summary of profile changes to all owners.
    """
    roster = sheets.load_roster()
    if not roster:
        log.info("Weekly synth: roster empty, nothing to do")
        return

    updated = []
    for w in roster:
        try:
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
        except Exception:
            log.exception("Weekly synth failed for %s", w["name"])

    if not updated:
        log.info("Weekly synth: no profiles updated, skipping digest")
        return

    today = datetime.now(ZoneInfo(config.MANAGER_TZ)).date().isoformat()

    # Build Slack-formatted weekly profile digest
    lines = [f"*Weekly Worker Profiles — {today}*", ""]
    for w, prior, new in updated:
        lines.append(f"*{w['name']}*")
        role = new.get('Role / What They Do', '')[:200]
        if role:
            lines.append(f"  _role:_ {role}")
        tasks = new.get('Recurring Tasks', '')[:300]
        if tasks:
            lines.append(f"  _recurring:_ {tasks}")
        strengths = new.get('Known Strengths', '')[:200]
        if strengths:
            lines.append(f"  _strengths:_ {strengths}")
        blockers = new.get('Known Blockers / Skill Gaps', '')[:200]
        if blockers:
            lines.append(f"  :warning: _blockers/gaps:_ {blockers}")
        open_auto = new.get('Automation Opportunities (Open)', '')[:300]
        if open_auto:
            lines.append(f"  :gear: _open automation backlog:_ {open_auto}")
        shipped = new.get('Automation Opportunities (Shipped)', '')[:200]
        if shipped:
            lines.append(f"  :white_check_mark: _shipped:_ {shipped}")
        patterns = new.get('Productivity Patterns', '')[:200]
        if patterns:
            lines.append(f"  _patterns:_ {patterns}")
        coaching = new.get('Coaching Notes for Manager', '')[:300]
        if coaching:
            lines.append(f"  :bulb: _for you:_ {coaching}")
        lines.append("")

    text = "\n".join(lines)
    if not _send_slack_digest(text):
        _notify_owners_of_failure(f"Weekly profile digest failed to ship on {today}")
    else:
        log.info("Sent weekly profile digest (Slack)")


def _build_slack_digest_text(today_local: str, sections: list[dict]) -> str:
    """Build a compact Slack-formatted EOD digest. Slack DMs are best with
    light markdown (*bold*, • bullets, plain text) — no HTML.
    """
    if not sections:
        return f"*EOD Digest — {today_local}*\n\n(no workers logged in today)"

    lines = [f"*Worker Tracker EOD — {today_local}*", ""]

    # Quick summary row at the top
    total_hours = sum(s.get("active_hours", 0) or 0 for s in sections)
    needs_help = [s["worker"] for s in sections if s.get("status") == "Needs help"]
    possible_slack = [s["worker"] for s in sections if s.get("status") == "Possible slack"]
    lines.append(f"_{len(sections)} workers active · {total_hours:.1f}h total_")
    if needs_help:
        lines.append(f":sos: needs help: {', '.join(needs_help)}")
    if possible_slack:
        lines.append(f":warning: possible slack: {', '.join(possible_slack)}")
    lines.append("")

    for s in sections:
        status = s.get("status", "OK")
        emoji = {"OK": ":large_green_circle:", "Needs help": ":sos:",
                 "Possible slack": ":warning:", "ERROR": ":x:"}.get(status, ":white_circle:")
        worker = s["worker"]
        active = s.get("active_hours", 0) or 0
        login = s.get("login_local", "—")
        eod = s.get("eod_local", "—")
        cap = s.get("capacity_signal", "")
        lines.append(f"{emoji} *{worker}* — {active:.2f}h · login {login} → EOD {eod}"
                     + (f" · _{cap}_" if cap else ""))
        if s.get("day_summary"):
            lines.append(f"  {s['day_summary'][:400]}")
        if s.get("automation_opportunities"):
            ao = s["automation_opportunities"]
            if isinstance(ao, list):
                ao = "; ".join(str(x) for x in ao)
            lines.append(f"  :gear: _automation: {str(ao)[:300]}_")
        if s.get("manual_red_flags"):
            mrf = s["manual_red_flags"]
            if isinstance(mrf, list):
                mrf = "; ".join(str(x) for x in mrf)
            lines.append(f"  :rotating_light: _manual grind: {str(mrf)[:300]}_")
        if s.get("notes"):
            lines.append(f"  _notes: {s['notes']}_")
        if s.get("new_commitments"):
            nc = s["new_commitments"]
            if isinstance(nc, list) and nc:
                lines.append(f"  :pushpin: _committed: {'; '.join(str(x)[:100] for x in nc[:3])}_")
        lines.append("")

    lines.append("_Full email digest (with check-in trails) was also sent. DM Sam 'send digest' anytime to force a fresh one._")
    return "\n".join(lines)


def _send_slack_digest(text: str) -> bool:
    """DM the digest to every owner in config.OWNER_SLACK_IDS using the bot
    token directly (so this works from scheduler context, not just inside
    bot.py's bolt app). Returns True if at least one DM succeeded."""
    if not config.OWNER_SLACK_IDS or not config.SLACK_BOT_TOKEN:
        log.warning("Slack digest skipped: OWNER_SLACK_IDS or SLACK_BOT_TOKEN unset")
        return False
    try:
        from slack_sdk import WebClient
    except ImportError:
        log.warning("slack_sdk not installed; cannot send Slack digest")
        return False
    client = WebClient(token=config.SLACK_BOT_TOKEN)
    ok = False
    for owner_id in config.OWNER_SLACK_IDS:
        try:
            client.chat_postMessage(channel=owner_id, text=text)
            log.info("Sent Slack EOD digest to %s", owner_id)
            ok = True
        except Exception:
            log.exception("Slack digest DM to %s failed", owner_id)
    return ok


def _notify_owners_of_failure(error_msg: str) -> None:
    """Last-resort: if the digest can't ship via any channel, DM owners the
    error directly so we never have a silent miss day."""
    if not config.OWNER_SLACK_IDS or not config.SLACK_BOT_TOKEN:
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=config.SLACK_BOT_TOKEN)
        for owner_id in config.OWNER_SLACK_IDS:
            try:
                client.chat_postMessage(
                    channel=owner_id,
                    text=f":x: EOD digest failed to send today.\n```{error_msg[:1500]}```",
                )
            except Exception:
                pass
    except Exception:
        log.exception("Could not even send failure notification")


def send_daily_digest() -> dict:
    """Build and ship today's EOD digest as a Slack DM to all owners.

    Slack-only by design (Jan opted out of email — DMs are his primary
    inbox). Bulletproof flow:
      - Per-worker collect_worker_day() errors are caught — one bad
        worker shows up in the digest as 'ERROR', the rest still ship.
      - If Slack delivery fails, Sam DMs the failure error to owners
        so we never have a silent miss day.

    Returns a dict with delivery status for debugging / manual triggers.
    """
    mgr_tz = ZoneInfo(config.MANAGER_TZ)
    today_local = datetime.now(mgr_tz).date().isoformat()
    result = {"date": today_local, "workers": 0, "slack": False, "errors": []}

    try:
        roster = sheets.load_roster()
    except Exception as e:
        err = f"load_roster failed: {type(e).__name__}: {e}"
        log.exception("EOD digest: %s", err)
        result["errors"].append(err)
        _notify_owners_of_failure(err)
        return result

    sections = []
    for w in roster:
        try:
            s = collect_worker_day(w, today_local)
        except Exception as e:
            log.exception("collect_worker_day failed for %s", w["name"])
            # Still include them in the digest as ERROR so we know
            sections.append({
                "worker": w["name"], "date": today_local,
                "login_local": "ERROR", "eod_local": "—",
                "active_hours": 0, "status": "ERROR",
                "day_summary": f"data collection failed: {type(e).__name__}",
                "automation_opportunities": "", "manual_red_flags": str(e)[:200],
                "capacity_signal": "", "notes": "", "new_commitments": [],
                "resolved_commitments": [], "checkins": [], "help_reqs": [],
                "missed": 0, "break_hours": 0, "profile": None,
            })
            continue
        if s["login_local"] == "—":
            continue
        sections.append(s)
    result["workers"] = len(sections)

    # ---- Slack delivery (sole channel) ----
    try:
        slack_text = _build_slack_digest_text(today_local, sections)
        if _send_slack_digest(slack_text):
            result["slack"] = True
        else:
            result["errors"].append("Slack send returned False (no OWNER_SLACK_IDS or all DMs failed)")
    except Exception as e:
        err = f"slack send failed: {type(e).__name__}: {e}"
        log.exception("EOD digest: %s", err)
        result["errors"].append(err)

    if not result["slack"]:
        _notify_owners_of_failure(
            f"EOD digest failed to send for {today_local}.\n"
            f"Errors:\n" + "\n".join(result["errors"])
        )
    return result
