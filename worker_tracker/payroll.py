"""Payroll: sum hours from Daily Summary → compute regular + overtime pay → write Payroll tab.

Pay periods:
- weekly: Mon → Sun (manager timezone)
- biweekly: 14-day windows aligned with weeks (last completed pair)
- monthly: last calendar month

Overtime is per-week (federal-style): hours over `Overtime Threshold (h/wk)`
in a single ISO week get the multiplier. Defaults: 40h / 1.5x.

For salaried workers (Pay Type = "salaried"), hours are still tracked for
visibility but pay isn't computed (stays at $0 — they get their salary
elsewhere). Lets the manager spot under/over-working.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from . import config, sheets

log = logging.getLogger(__name__)


def period_bounds(period: str, ref: date) -> tuple[date, date]:
    """Return (start, end) of the most recently COMPLETED period as of `ref`.

    Both dates are inclusive. End is always strictly before `ref`.

    Semimonthly schedule (the default for this project):
    - Period A: 1st through 14th (paid on the 15th)
    - Period B: 15th through end-of-month (paid on the 1st of next month)
    """
    if period == "semimonthly":
        if ref.day >= 15:
            # Period A just closed (1st through 14th of this month)
            start = ref.replace(day=1)
            end = ref.replace(day=14)
            return start, end
        # ref is between the 1st and 14th, so period B of LAST month just closed
        prev_eom = ref.replace(day=1) - timedelta(days=1)
        start = prev_eom.replace(day=15)
        end = prev_eom
        return start, end
    if period == "weekly":
        days_since_mon = ref.weekday()  # 0=Mon
        last_sunday = ref - timedelta(days=days_since_mon + 1)
        last_monday = last_sunday - timedelta(days=6)
        return last_monday, last_sunday
    if period == "biweekly":
        days_since_mon = ref.weekday()
        last_sunday = ref - timedelta(days=days_since_mon + 1)
        start = last_sunday - timedelta(days=13)
        return start, last_sunday
    if period == "monthly":
        first_of_this = ref.replace(day=1)
        end = first_of_this - timedelta(days=1)
        start = end.replace(day=1)
        return start, end
    raise ValueError(f"Unknown payroll period: {period}")


def _hours_per_iso_week(daily_rows: Iterable[dict]) -> dict[tuple, float]:
    """Group hours by (iso_year, iso_week). Used so overtime is calculated per workweek."""
    week_totals: dict[tuple, float] = {}
    for r in daily_rows:
        try:
            d = date.fromisoformat(str(r.get("Date", "")).strip())
            h = float(str(r.get("Active Hours", "0") or 0).replace(",", ""))
        except (TypeError, ValueError):
            continue
        key = d.isocalendar()[:2]
        week_totals[key] = week_totals.get(key, 0.0) + h
    return week_totals


def calculate_pay(worker: dict, daily_rows: list[dict]) -> dict:
    """Compute pay components for one worker over the given Daily Summary rows."""
    rate = float(worker.get("hourly_rate") or 0)
    ot_threshold = float(worker.get("ot_threshold") or config.PAYROLL_DEFAULT_OT_THRESHOLD)
    ot_multiplier = float(worker.get("ot_multiplier") or config.PAYROLL_DEFAULT_OT_MULTIPLIER)
    pay_type = (worker.get("pay_type") or "hourly").lower()

    week_totals = _hours_per_iso_week(daily_rows)
    total_hours = sum(week_totals.values())

    regular = 0.0
    overtime = 0.0
    for h in week_totals.values():
        if h <= ot_threshold:
            regular += h
        else:
            regular += ot_threshold
            overtime += h - ot_threshold

    if pay_type == "salaried":
        salary = float(worker.get("salary_per_period") or 0)
        regular_pay = 0.0
        overtime_pay = 0.0
        gross_pay = salary
    else:
        regular_pay = regular * rate
        overtime_pay = overtime * rate * ot_multiplier
        gross_pay = regular_pay + overtime_pay

    return {
        "days_worked": len([r for r in daily_rows if str(r.get("Active Hours", "0") or 0) not in ("", "0")]),
        "total_hours": round(total_hours, 2),
        "regular_hours": round(regular, 2),
        "overtime_hours": round(overtime, 2),
        "regular_pay": round(regular_pay, 2),
        "overtime_pay": round(overtime_pay, 2),
        "gross_pay": round(gross_pay, 2),
    }


def run_payroll(period: str | None = None, ref: date | None = None) -> list[dict]:
    """Generate the most-recently-completed pay period's payroll. Returns
    a list of {worker, calc, period_start, period_end} dicts. Skips workers
    with no Daily Summary rows in the period.
    """
    period = period or config.PAYROLL_PERIOD
    if period == "none":
        log.info("PAYROLL_PERIOD=none, skipping payroll")
        return []

    ref = ref or datetime.now(ZoneInfo(config.MANAGER_TZ)).date()
    start, end = period_bounds(period, ref)
    log.info("Running %s payroll for %s → %s", period, start, end)

    roster = sheets.load_roster()
    results = []
    generated_at = datetime.now(ZoneInfo(config.MANAGER_TZ)).isoformat(timespec="seconds")

    for w in roster:
        worker_rows = [
            r for r in sheets.summaries_in_range(start.isoformat(), end.isoformat())
            if str(r.get("Worker", "")).strip() == w["name"]
        ]
        if not worker_rows:
            log.info("No activity for %s in %s → %s — skipping", w["name"], start, end)
            continue
        calc = calculate_pay(w, worker_rows)

        sheets.append_payroll([
            start.isoformat(), end.isoformat(),
            w["name"], w["user_id"],
            w.get("pay_type", "hourly"),
            calc["days_worked"], calc["total_hours"],
            calc["regular_hours"], calc["overtime_hours"],
            w.get("hourly_rate", 0),
            w.get("salary_per_period", 0),
            calc["regular_pay"], calc["overtime_pay"], calc["gross_pay"],
            w.get("currency", config.PAYROLL_DEFAULT_CURRENCY),
            "", generated_at,
        ])

        # Per-day timesheet rows for bookkeeper review
        rate = float(w.get("hourly_rate") or 0)
        pay_type = w.get("pay_type", "hourly")
        for r in worker_rows:
            try:
                day_date = date.fromisoformat(str(r.get("Date", "")).strip())
            except (TypeError, ValueError):
                continue
            try:
                hours = float(str(r.get("Active Hours", "0") or 0).replace(",", ""))
            except (TypeError, ValueError):
                hours = 0.0
            notes = str(r.get("Notes", "")).strip()
            break_h = ""
            # Daily Summary "Notes" carries "Xh on break" — parse that for the timesheet
            import re as _re
            m = _re.search(r"([\d.]+)h on break", notes)
            if m:
                try:
                    break_h = float(m.group(1))
                except ValueError:
                    break_h = ""
            daily_pay = round(hours * rate, 2) if pay_type == "hourly" else ""
            sheets.append_timesheet([
                start.isoformat(), end.isoformat(),
                w["name"], w["user_id"],
                day_date.isoformat(),
                day_date.strftime("%A"),
                str(r.get("Login (local)", "")), str(r.get("EOD (local)", "")),
                hours, break_h,
                pay_type, rate, daily_pay,
                notes,
            ])

        results.append({
            "worker": w,
            "calc": calc,
            "period_start": start,
            "period_end": end,
            "daily_rows": worker_rows,
        })

    return results
