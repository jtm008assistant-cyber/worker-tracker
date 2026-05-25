"""Env + constants for worker_tracker. Reads C:\\Ace\\.env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON") or str(Path(__file__).resolve().parent.parent / "sa-key.json")


def _materialize_sa_key_from_env() -> None:
    """In cloud deploys we can't ship the JSON file. If the path doesn't
    exist but SERVICE_ACCOUNT_JSON_B64 is set, decode it to disk on
    startup. Idempotent."""
    import base64

    b64 = os.environ.get("SERVICE_ACCOUNT_JSON_B64")
    if not b64:
        return
    target = Path(SERVICE_ACCOUNT_JSON)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(b64))


_materialize_sa_key_from_env()
SHARED_DRIVE_ID = os.environ.get("SHARED_DRIVE_ID")
SHEETS_FOLDER_ID = os.environ.get("SHEETS_FOLDER_ID")

TRACKER_SHEET_ID = os.environ.get("WORKER_TRACKER_SHEET_ID")
PAYROLL_SHEET_ID = os.environ.get("PAYROLL_SHEET_ID")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
REPORT_RECIPIENT = os.environ.get("REPORT_RECIPIENT") or GMAIL_USER
# PAYROLL_RECIPIENT can be one email or comma-separated list (e.g. manager + bookkeeper)
# Falls back to REPORT_RECIPIENT if not set.
PAYROLL_RECIPIENT = os.environ.get("PAYROLL_RECIPIENT") or REPORT_RECIPIENT

MANAGER_TZ = os.environ.get("MANAGER_TZ", "America/New_York")
REPORT_TIME_LOCAL = os.environ.get("REPORT_TIME_LOCAL", "22:00")

CHECKIN_INTERVAL_MINUTES = int(os.environ.get("CHECKIN_INTERVAL_MINUTES", "120"))  # 2h default
MISSED_CHECKIN_GRACE_MINUTES = int(os.environ.get("MISSED_CHECKIN_GRACE_MINUTES", "30"))

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ROSTER_TAB = "Roster"
ACTIVITY_TAB = "Activity Log"
SUMMARY_TAB = "Daily Summary"
PROFILE_TAB = "Worker Profile"
KNOWLEDGE_TAB = "Processes & Tools"

ROSTER_HEADER = [
    "Name", "Slack User ID", "Work Email", "Payout Email", "Payout Method",
    "Timezone", "Expected Start", "Expected EOD", "Active",
    "Pay Type", "Hourly Rate", "Salary (per period)", "Currency",
    "Overtime Threshold (h/wk)", "Overtime Multiplier",
    "Check-in Frequency (min)", "Personal View Sheet URL",
]
ACTIVITY_HEADER = ["Timestamp UTC", "Local Date", "Local Time", "Worker", "Slack User ID", "Type", "Message"]
SUMMARY_HEADER = [
    "Date", "Worker", "Login (local)", "EOD (local)", "Active Hours",
    "Check-ins", "Help Requests", "Missed Prompts", "Status", "Notes",
    "Day Summary", "Automation Ideas", "Manual Red Flags", "Capacity Signal",
]

PROFILE_HEADER = [
    "Worker", "Slack User ID", "First Seen", "Days Tracked",
    "Role / What They Do", "Recurring Tasks", "Known Strengths",
    "Known Blockers / Skill Gaps", "Tools They Currently Use",
    "Automation Opportunities (Open)", "Automation Opportunities (Shipped)",
    "Productivity Patterns", "Coaching Notes for Manager", "Last Updated",
]

KNOWLEDGE_HEADER = [
    "Worker", "Slack User ID", "Kind", "Name", "URL",
    "Description", "Steps / Notes",
    "First Mentioned", "Last Updated", "Times Referenced",
]

# How aggressively Sam asks follow-up questions when workers mention unfamiliar things
MAX_FOLLOWUPS_PER_DAY = int(os.environ.get("MAX_FOLLOWUPS_PER_DAY", "2"))
# Minimum minutes between follow-ups to the same worker
FOLLOWUP_COOLDOWN_MINUTES = int(os.environ.get("FOLLOWUP_COOLDOWN_MINUTES", "60"))

WEEKLY_SYNTHESIS_DOW = int(os.environ.get("WEEKLY_SYNTHESIS_DOW", "6"))  # 0=Mon, 6=Sun
WEEKLY_SYNTHESIS_TIME = os.environ.get("WEEKLY_SYNTHESIS_TIME", "21:00")

# Payroll
PAYROLL_TAB = "Payroll"
PAYROLL_HEADER = [
    "Period Start", "Period End", "Worker", "Slack User ID", "Pay Type",
    "Days Worked", "Total Hours", "Regular Hours", "Overtime Hours",
    "Hourly Rate", "Salary (per period)",
    "Regular Pay", "Overtime Pay", "Gross Pay", "Currency",
    "Notes", "Generated At",
]
TIMESHEET_TAB = "Timesheet"
TIMESHEET_HEADER = [
    "Period Start", "Period End", "Worker", "Slack User ID", "Date",
    "Day of Week", "Login", "EOD", "Hours Worked", "Break Hours",
    "Pay Type", "Rate", "Daily Pay", "Notes",
]
PAYROLL_PERIOD = os.environ.get("PAYROLL_PERIOD", "semimonthly")  # semimonthly | weekly | biweekly | monthly | none
PAYROLL_RUN_TIME = os.environ.get("PAYROLL_RUN_TIME", "10:00")  # late enough that all yesterday EODs are written
PAYROLL_DM_WORKERS = os.environ.get("PAYROLL_DM_WORKERS", "false").lower() in ("true", "1", "yes")
PAYROLL_DEFAULT_OT_THRESHOLD = float(os.environ.get("PAYROLL_DEFAULT_OT_THRESHOLD", "40"))
PAYROLL_DEFAULT_OT_MULTIPLIER = float(os.environ.get("PAYROLL_DEFAULT_OT_MULTIPLIER", "1.5"))
PAYROLL_DEFAULT_CURRENCY = os.environ.get("PAYROLL_DEFAULT_CURRENCY", "USD")

# Pre-payroll review DM — sent the evening BEFORE payroll runs (14th + last day of month for semimonthly)
PRE_PAYROLL_REVIEW_TIME = os.environ.get("PRE_PAYROLL_REVIEW_TIME", "20:00")  # 8pm manager-local

EOD_PATTERNS = (
    r"\beod\b",
    r"\blogging off\b", r"\blog off\b", r"\bsigning off\b",
    r"\blogging out\b", r"\blogged out\b",
    r"\bdone for the day\b", r"\bdone for now\b",
    r"\bdone (?:with )?(?:my |the )?shift\b",        # "done my shift", "done with my shift"
    r"\bshift (?:done|over|ended|finished)\b",       # "shift over", "shift done"
    r"\b(?:ending|finishing|wrapping up) (?:my |the )?shift\b",
    r"\bcalling it\b", r"\bcalling it a day\b",
    r"\boff the clock\b",
    r"\bclocking out\b", r"\bclocked out\b",
    r"\bshutting down\b",
    r"\bgoodnight\b", r"\bgn\b",
    r"\bwrapping up\b",
    r"\bsee you tomorrow\b", r"\bsee ya\b",
    r"\bi'?m out\b", r"\bim out\b",
    r"\bheading out\b",
)

HELP_PATTERNS = (
    r"\bhelp\b", r"\bstuck\b", r"\bblocked\b", r"\bissue\b", r"\bproblem\b",
    r"\bcan'?t\b", r"\bnot sure\b", r"\bconfused\b", r"\bbroken\b",
    r"\berror\b", r"\bdoesn'?t work\b", r"\?",
)

BREAK_START_PATTERNS = (
    r"\bbreak\b", r"\b-break\b", r"\bbrb\b", r"\bafk\b", r"\bbio\b",
    r"\blunch\b", r"\bstepping away\b", r"\bbe right back\b",
    r"\btaking a break\b", r"\bgoing on break\b", r"\bgoing for a break\b",
    r"\bpause\b", r"\bpausing\b",
)
BREAK_END_PATTERNS = (
    r"\bi'?m back\b", r"\bback from break\b", r"\bback from lunch\b",
    r"\bresumed\b", r"\bresuming\b", r"\bunpause\b",
)

# Worker asks how many hours they've worked — Sam replies with their period total
HOURS_QUERY_PATTERNS = (
    r"^\s*hours?\s*\??\s*$",
    r"\bmy hours\b", r"\bhow many hours\b", r"\bhours this period\b",
    r"\bperiod total\b", r"\bhours total\b", r"\bpay period\b",
    r"\bcheck (?:my )?hours\b",
)

# Worker flags that their tracked hours are wrong
DISCREPANCY_PATTERNS = (
    r"\bwrong\b", r"\bincorrect\b", r"\bdiscrepancy\b",
    r"\bmissed (?:a |my |the )?(?:break|lunch|hour|time)\b",
    r"\byou missed\b", r"\bnot accurate\b", r"\bnot right\b",
    r"\bthat'?s wrong\b", r"\bthat'?s not right\b",
    r"\bactually (?:worked|i worked)\b", r"\bshould be\b",
    r"\boff by\b",
)
