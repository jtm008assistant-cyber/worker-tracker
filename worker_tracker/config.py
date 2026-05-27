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
# Separate subfolder for per-worker view sheets so they're not mixed with admin sheets.
# Falls back to SHEETS_FOLDER_ID if not set.
WORKER_VIEWS_FOLDER_ID = os.environ.get("WORKER_VIEWS_FOLDER_ID") or os.environ.get("SHEETS_FOLDER_ID")

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
# Deep analytical brain — used for daily EOD analysis + weekly profile synthesis.
# Falls back to Gemini Pro if ANTHROPIC_API_KEY isn't set, so nothing breaks
# while you provision the Anthropic key.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

ROSTER_TAB = "Roster"
ACTIVITY_TAB = "Activity Log"
SUMMARY_TAB = "Daily Summary"
PROFILE_TAB = "Worker Profile"
KNOWLEDGE_TAB = "Processes & Tools"
TIME_OFF_TAB = "Time Off"
COMMITMENTS_TAB = "Commitments"
RELAY_TAB = "Relay Queue"
LIBRARY_TAB = "Knowledge Library"

ROSTER_HEADER = [
    "Name", "Slack User ID", "Work Email", "Payout Email", "Payout Method",
    "Timezone", "Expected Start", "Expected EOD", "Active",
    "Pay Type", "Hourly Rate", "Salary (per period)", "Currency",
    "Overtime Threshold (h/wk)", "Overtime Multiplier",
    "Check-in Frequency (min)", "Personal View Sheet URL", "Nicknames",
    "Vacation Days/Year", "Sick Days/Year", "Holiday Days/Year",
    "PTO Days/Year", "Benefits Notes",
    "Hourly Rate (from Contract)", "Pay Schedule",
    "HMO Reimbursement (PHP/yr)", "13th Month Eligible",
    "Performance Bonus Date", "Calamity Fund (PHP/yr)",
    "Contract Start Date", "Probation End Date",
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

TIME_OFF_HEADER = [
    "Date Logged", "Worker", "Slack User ID", "Type",
    "Start Date", "End Date", "Days",
    "Status", "Logged By", "Notes",
]

COMMITMENTS_HEADER = [
    "Date Created", "Worker", "Slack User ID", "Commitment",
    "Mentioned Person", "Status", "Date Resolved", "Resolution Notes",
]

# Ad-hoc relay: an admin asks Sam to pass a message to a worker the next time
# the worker logs in. Sam delivers it on clock-in, then notifies the admin
# when the worker confirms it's done.
RELAY_HEADER = [
    "Relay ID", "Date Created", "From Name", "From Slack ID",
    "To Worker", "To Slack ID", "Message", "Estimated Time",
    "Status", "Date Delivered", "Date Completed", "Worker Reply", "Notes",
]

# Aggregated company-wide view, one row per unique tool/process across all workers.
LIBRARY_HEADER = [
    "Tool / Process Name", "Kind", "URL",
    "Description (latest)", "Used By", "# of Users",
    "Total References", "First Seen", "Last Updated",
]

# Sam asks follow-up questions ONLY when a worker is replying to his periodic
# check-in prompt — that way Sam's curiosity rides on the worker's check-in
# cadence (every 90/120/180 min per worker) instead of interrupting them at
# random times. Daily cap is a safety upper bound; cooldown is a softer guard
# so two rapid replies in the same check-in window don't both trigger asks.
MAX_FOLLOWUPS_PER_DAY = int(os.environ.get("MAX_FOLLOWUPS_PER_DAY", "5"))
FOLLOWUP_COOLDOWN_MINUTES = int(os.environ.get("FOLLOWUP_COOLDOWN_MINUTES", "60"))

WEEKLY_SYNTHESIS_DOW = int(os.environ.get("WEEKLY_SYNTHESIS_DOW", "6"))  # 0=Mon, 6=Sun
WEEKLY_SYNTHESIS_TIME = os.environ.get("WEEKLY_SYNTHESIS_TIME", "21:00")

# Daily planning config defined below the admin block (needs OWNER_SLACK_IDS).

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
    # Tightened — these need specific phrasing so casual chat doesn't trigger help-flags
    r"\bneed help\b", r"\bcan you help\b", r"\bplease help\b",
    r"\bstuck on\b", r"\bblocked on\b", r"\bgetting stuck\b",
    r"\bbroken\b", r"\berror\b", r"\bdoesn'?t work\b", r"\bnot working\b",
    r"\b(?:big )?problem with\b", r"\bissue with\b",
)

BREAK_START_PATTERNS = (
    r"\bbreak\b", r"\b-break\b", r"\bbrb\b", r"\bafk\b", r"\bbio\b",
    r"\blunch\b", r"\bstepping away\b", r"\bbe right back\b",
    r"\btaking a break\b", r"\bgoing on break\b", r"\bgoing for a break\b",
    r"\bpause\b", r"\bpausing\b",
)
BREAK_END_PATTERNS = (
    # "i'm back" with or without apostrophe; also "i am back"
    r"\bi'?m back\b", r"\bi am back\b", r"\bim back\b",
    r"\bback from break\b", r"\bback from lunch\b",
    r"\bresumed\b", r"\bresuming\b", r"\bunpause\b",
    # Short / standalone forms — common after a long break
    r"^\s*back\s*[!.?]*\s*$",          # message body is literally just "back" / "back!" / "back?"
    r"\bback now\b", r"\bok back\b", r"\back i'?m back\b",
    r"\bready to (?:go|work|resume)\b",
)

# Worker asks how many hours they've worked — Sam replies with their period total
HOURS_QUERY_PATTERNS = (
    r"^\s*hours?\s*\??\s*$",
    r"\bmy hours\b", r"\bhow many hours\b", r"\bhours this period\b",
    r"\bperiod total\b", r"\bhours total\b", r"\bpay period\b",
    r"\bcheck (?:my )?hours\b",
)

# Worker flags that their tracked hours are wrong
# Two-tier admin model:
#   OWNER_SLACK_IDS  — full power. Can broadcast intros, query anyone. Cannot be queried by managers.
#   MANAGER_SLACK_IDS — can query worker status, but cannot query owners. Treated as workers for daily tracking.
# Both default + env-var overridable, comma-separated.
OWNER_SLACK_IDS = [
    s.strip() for s in os.environ.get("OWNER_SLACK_IDS", "UCXSXMU21,UCYBJC86S").split(",") if s.strip()
]
MANAGER_SLACK_IDS = [
    s.strip() for s in os.environ.get("MANAGER_SLACK_IDS", "U030Q7R9FNC").split(",") if s.strip()
]
# Union used to gate admin-command routing in the bot.
ADMIN_SLACK_IDS = list({*OWNER_SLACK_IDS, *MANAGER_SLACK_IDS})

# Daily planning: Sam asks the manager what each worker should focus on tomorrow.
# Fires every evening so the manager has time to think before workers log in.
DAILY_PLANNING_TIME = os.environ.get("DAILY_PLANNING_TIME", "20:00")  # 8pm manager local
# Slack ID of the person Sam asks. Defaults to first owner (Jan).
DAILY_PLANNING_SLACK_ID = os.environ.get("DAILY_PLANNING_SLACK_ID") or (
    OWNER_SLACK_IDS[0] if OWNER_SLACK_IDS else None
)

ADMIN_INTRODUCE_PATTERNS = (
    r"\bintroduce (?:everyone|all|workers?|the team)\b",
    r"\bsend intros?\b",
    r"\bonboard (?:everyone|all|workers?|the team)\b",
    r"\bintro all\b",
)

# Owner command: 'audit drive' / 'drive cleanup' / 'what files do we have'
ADMIN_DRIVE_AUDIT_PATTERNS = (
    r"\baudit (?:the )?drive\b",
    r"\bdrive (?:audit|cleanup|review)\b",
    r"\bwhat files (?:do we have|are gold|are garbage)\b",
    r"\bgold (?:and|vs) garbage\b",
)

# Admin command: "log vacation for hannah dec 1-5" / "sick day for rey today" / etc.
ADMIN_TIME_OFF_PATTERNS = (
    r"\blog (?:a |the )?(?:vacation|sick|pto|personal|time off|holiday|day off|leave)\b",
    r"\b(?:vacation|sick|pto|personal|holiday|leave|day off|time off)\s+for\b",
    r"\b(?:gave|giving|approving)\s+\w+\s+(?:a |the )?(?:day|days)\s+off\b",
)

# Admin command: "ask hannah about benefits" / "collect benefits" / etc.
ADMIN_BENEFITS_QUERY_PATTERNS = (
    r"\bask\s+\w+\s+about\s+benefits\b",
    r"\bcollect\s+benefits\b",
    r"\bget\s+benefits\s+info\b",
)

# Worker query: "how many vacation days do i have" / "pto balance" / etc.
TIMEOFF_BALANCE_QUERY_PATTERNS = (
    r"\bhow many\s+(?:vacation|sick|pto|personal)\s+(?:days?)?\b",
    r"\b(?:my )?(?:vacation|sick|pto)\s+balance\b",
    r"\bdays off\s+(?:left|remaining|available)\b",
)

# Admin can relay a message THROUGH Sam to a specific worker. Examples:
#   "send to Jonny: get back to work"
#   "tell norks his hours look wrong"
#   "dm hannah: nice work today"
#   "message Rey - check in pls"
ADMIN_FORWARD_PATTERNS = (
    # Strict "send to X: message" / "tell hannah, do X" — the captured
    # name must NOT be a generic pronoun (this/that/it/the/etc.) otherwise
    # phrases like "send this to ger <url>" get mis-parsed as a worker
    # named "this". Generic pronouns route to the relay handler below
    # (which uses Gemini and correctly identifies the actual worker).
    r"^(?:send|dm|message|tell|forward)\s+(?:to\s+)?"
    r"(?!(?:this|that|it|the|them|these|those|him|her|a|an|over|out)\b)"
    r"([A-Za-z][\w'.-]*)\s*[:,;-]?\s+(.+)$",
)

# Ad-hoc deferred relay — admin asks Sam to deliver something the NEXT TIME
# the worker logs in (or right now if already online). Examples:
#   "when ger logs in tell her to quickly fix the listing — should only take 15 min"
#   "next time hannah clocks in, ask her to pull the Q1 numbers"
#   "have rey upload the new thumbnails when he's online"
#   "tomorrow when jonny starts can you remind him about the wise account"
# Pattern is intentionally loose — anything matching here goes through Gemini
# parse_relay_request() to extract worker + task + optional time estimate.
ADMIN_RELAY_PATTERNS = (
    # ----- DEFERRED: "when X logs in, do Y" / "next time X is here" -----
    r"\bwhen\s+\w+\s+(?:logs?\s*in|clocks?\s*in|comes?\s+on(?:line)?|starts?|signs?\s*in|is\s+(?:on|online|available|here))\b",
    r"\bnext\s+time\s+\w+\s+(?:logs?|clocks?|signs?|is)\b",
    r"\b(?:tell|ask|have|remind|let)\s+\w+\s+(?:when|once|as soon as)\s+(?:she|he|they|s?he)\s+(?:logs?|clocks?|signs?|is|comes?|gets?)\b",
    r"\btomorrow\s+when\s+\w+\b",
    r"\bonce\s+\w+\s+(?:is|logs?|clocks?|comes?)\b",
    # Pronoun-form trailing trigger: "...when he's online", "...when she's back", "...when they're here"
    r"\bwhen\s+(?:s?he|they|he|she)'?\s*s?\s*(?:is\s+)?(?:on(?:line)?|here|available|back|free|around)\b",
    # Imperative-style with separated "when" clause: "have rey upload thumbs when hes online"
    r"\b(?:tell|ask|have|remind|let)\s+\w+\b[^.\n]{0,120}?\bwhen\s+(?:s?he|they|he|she|\w+)'?s?\b",

    # ----- IMMEDIATE: "send this to X" / "give X this link" / "share with X" -----
    # These don't have a "when" clause — admin wants delivery now (or as
    # soon as worker is online). Routes through the same handler, which
    # delivers immediately if the worker is already clocked in.
    # Catches: "send this to ger", "send ger this link", "give ger this",
    #          "share with hannah", "forward to rey", "shoot this to norks"
    r"\b(?:send|give|share|forward|shoot|pass|drop)\s+(?:this|that|it|the|these|those)\s+(?:link|message|note|file|doc|sheet|info|update)?\s*(?:to|with|over to)\s+\w+\b",
    r"\b(?:send|give|share|forward|shoot|pass|drop)\s+\w+\s+(?:this|that|the|these|those)\b",
    r"\b(?:send|forward|share|pass)\s+(?:to|with|over to)\s+\w+\b",
)

# Admins can ask "what is X doing" / "where's X" / "status of X" / etc.
# The (.+?) captures the worker name (can be partial: "Hannah" matches "Hannah May Bagares").
ADMIN_TEAM_STATUS_PATTERNS = (
    # Collective check-ins: "did everyone log in", "who clocked in", "is anyone working"
    r"\bdid\s+(?:everyone|everybody|anyone|anybody|the team|all of them|the workers?)\s+"
    r"(?:clock in|log on|log in|sign in|start|EOD|finish|wrap|come on|come online)\b",
    r"\b(?:is|are)\s+(?:everyone|everybody|anyone|anybody|the team|all of them|the workers?)\s+"
    r"(?:working|online|on|here|around|up|in|logged in|clocked in)\b",
    r"\bwho(?:'?s| is| has)?\s+"
    r"(?:working|online|here|around|up|in|logged in|clocked in|on the clock|on break|missing|done|out|finished|EOD'?d?)\b",
    r"\bwho(?:'?s| is)?\s+(?:not\s+)?(?:in|logged in|clocked in|working|here|up|on|online)\s+(?:today|yet)?\b",
    r"\bwho\s+(?:hasn'?t|has\s+not|hadn'?t|had\s+not)\s+(?:logged in|clocked in|started|signed in|shown up|come on|come in)\b",
    r"\b(?:team|everyone'?s|everybody'?s)\s+status\b",
    r"\bstatus\s+(?:of\s+)?(?:the\s+)?(?:team|everyone|everybody)\b",
    r"\bhow(?:'?s| is| are)\s+(?:everyone|everybody|the team|the workers?)\s+(?:doing|going|today)?\b",
    r"\b(?:everyone|everybody|the team|the workers?)\s+(?:doing|working|online|logged in|clocked in)\b",
)


# Words/phrases that should NEVER be treated as a worker name. When the
# per-worker status regex captures one of these as the worker, route the
# query to the team-wide handler instead of telling the admin "don't know
# anyone named 'everyone' on the roster".
TEAM_WIDE_PRONOUNS = frozenset({
    "everyone", "everybody", "anyone", "anybody", "the team", "team",
    "all", "all of them", "all of us", "the workers", "workers",
    "the squad", "squad", "the crew", "crew", "everyone's", "everybodys",
})


TASK_LIST_WORKER_PATTERNS = (
    # Worker asks Sam to show their own task list / checklist
    r"\b(?:my|whats? my|show my|see my|view my|list my)\s+(?:tasks?|list|checklist|todo|to[\s-]?do|plate|queue|things)\b",
    r"\b(?:what'?s\s+on\s+my)\s+(?:plate|list|checklist|todo|to[\s-]?do)\b",
    r"\bwhat\s+(?:do\s+i\s+have|am\s+i\s+(?:doing|working\s+on))\b",
    r"\b(?:show|give)\s+me\s+(?:my|the)\s+(?:tasks?|list|checklist|todo)\b",
    r"^(?:tasks?|todo|to[\s-]?do|checklist|my list)\s*\??\s*$",
)

TASK_LIST_ADMIN_PATTERNS = (
    # Admin asks Sam for a worker's task list
    r"\b(?:tasks?|list|checklist|todo|to[\s-]?do|plate|queue)\s+(?:for|of)\s+([A-Za-z][\w'.-]{0,30})\b",
    r"\b([A-Za-z][\w'.-]{0,30}?)(?:'s|s)\s+(?:tasks?|list|checklist|todo|to[\s-]?do|plate|queue)\b",
    r"\bwhat(?:'s| is| are)?\s+(?:on\s+)?([A-Za-z][\w'.-]{0,30}?)(?:'s|s)?\s+(?:plate|list|checklist|todo)\b",
    r"\bshow\s+(?:me\s+)?([A-Za-z][\w'.-]{0,30}?)(?:'s|s)?\s+(?:tasks?|list|checklist|todo)\b",
    # Bare name followed by a task word — "rey checklist", "hannah todo"
    r"\b([A-Za-z][\w'.-]{0,30}?)\s+(?:tasks?|list|checklist|todo|to[\s-]?do)\b",
)


ADMIN_DIGEST_NOW_PATTERNS = (
    # "send the EOD digest" / "EOD report now" / "give me today's digest" / "run digest"
    r"\b(?:send|run|trigger|give me|gimme)\s+(?:the\s+|today'?s\s+)?(?:eod|EOD|daily)\s+(?:report|digest|summary)\b",
    r"\b(?:eod|EOD)\s+(?:report|digest|summary)\s+now\b",
    r"\bdigest\s+now\b",
    r"\bsend\s+digest\b",
    r"\brun\s+the\s+digest\b",
)


ADMIN_STATUS_PATTERNS = (
    # ----- Present-tense: "what's X doing right now" -----
    # "what's X doing" / "whats X doin" / "what is X up to" / "what are workers doing"
    r"\bwhat(?:'?s| is| are)?\s+([A-Za-z][\w\s'.-]{0,30}?)\s+(?:doing|doin'?|up to|working on|workin'? on)\b",
    # "how's X doing" / "hows X going" / "how is Hannah doin"
    r"\bhow(?:'?s| is)?\s+([A-Za-z][\w\s'.-]{0,30}?)\s+(?:doing|doin'?|going|goin'?)\b",
    # "where's X" / "wheres X" / "where is X"
    r"\bwhere(?:'?s| is)?\s+([A-Za-z][\w\s'.-]{0,30}?)\b",
    # "status of X" / "status on X" / "X status"
    r"\bstatus (?:of|on|for)\s+([A-Za-z][\w\s'.-]{0,30}?)\b",
    r"\b([A-Za-z][\w\s'.-]{0,30}?)\s+status\b",
    # "check on X" / "check X"
    r"\bcheck (?:on\s+)?([A-Za-z][\w\s'.-]{0,30}?)\b",
    # "is X online/working/here"
    r"\bis\s+([A-Za-z][\w\s'.-]{0,30}?)\s+(?:online|working|workin'?|on|here|around)\b",

    # ----- Past-tense: "what did X do today" (added after Jan's silence bug) -----
    # "what did X do today" / "what did X work on" / "what did X accomplish/finish/get done"
    r"\bwhat did\s+([A-Za-z][\w\s'.-]{0,30}?)\s+(?:do|work on|accomplish|finish|get done|wrap up|knock out|handle)\b",
    # "what has X done today" / "what's X done today" / "what has X been working on"
    r"\bwhat(?:'?s| has)\s+([A-Za-z][\w\s'.-]{0,30}?)\s+(?:done|been doing|been working on|worked on)\b",
    # "did X clock in" / "did X log on/in" / "did X start" / "did X EOD"
    r"\bdid\s+([A-Za-z][\w\s'.-]{0,30}?)\s+(?:clock in|log on|log in|sign in|start (?:today|work)|EOD|finish|wrap)\b",
    # "how has X been" / "how's X been"
    r"\bhow(?:'?s| has)\s+([A-Za-z][\w\s'.-]{0,30}?)\s+been\b",
)

DISCREPANCY_PATTERNS = (
    r"\bwrong\b", r"\bincorrect\b", r"\bdiscrepancy\b",
    r"\bmissed (?:a |my |the )?(?:break|lunch|hour|time)\b",
    r"\byou missed\b", r"\bnot accurate\b", r"\bnot right\b",
    r"\bthat'?s wrong\b", r"\bthat'?s not right\b",
    r"\bactually (?:worked|i worked)\b", r"\bshould be\b",
    r"\boff by\b",
)
