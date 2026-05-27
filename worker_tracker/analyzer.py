"""Gemini-powered analysis. Two entry points:

- analyze() — daily, called when a worker EODs (or at digest time). Takes
  one day's check-ins + the worker's persistent profile and returns
  per-day fields for the email + Daily Summary row.

- synthesize_weekly_profile() — weekly, called Sunday night. Reads the
  past 7 days of activity + the current profile and returns an updated
  profile (recurring tasks, blockers, automation status, patterns…).

Both fail open — exceptions return empty/unchanged data so the rest of
the pipeline keeps running.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Iterable

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

from . import config, deep_brain

log = logging.getLogger(__name__)

EMPTY_DAILY = {
    "day_summary": "",
    "automation_opportunities": [],
    "manual_red_flags": [],
    "capacity_signal": "",
    "new_commitments": [],
    "resolved_commitments": [],
}

URL_RE = __import__("re").compile(r"https?://[^\s<>\"']+")


def _strip_codefence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def _extract_json_object(text: str) -> dict | None:
    """Defensive JSON extraction. Tries:
      1. Direct json.loads(strict=False) — handles control chars
      2. Strip everything outside the first balanced {...} block
      3. Trim a trailing unterminated string + close the JSON (common
         when Gemini hits max_output_tokens mid-string and we still want
         to salvage the partial)
    Returns the parsed dict or None.
    """
    text = _strip_codefence(text)
    if not text:
        return None

    # 1. Straight parse
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # 2. Find the first balanced { ... } block via depth counter
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate, strict=False)
                except json.JSONDecodeError:
                    break  # not valid, try truncation salvage

    # 3. Truncation salvage: Gemini ran out of tokens mid-string. Find the
    # last complete "key": "value" pair and close the object.
    # Pattern: walk back to the last `",` or `"]` or `"}` and close.
    salvage = text
    # Strip an opening { ... up through last complete pair
    last_safe = max(salvage.rfind('",'), salvage.rfind('"]'), salvage.rfind('"}'))
    if last_safe > 0:
        # Cut at last_safe + 1 (include the closing ") and close any open braces
        trimmed = salvage[:last_safe + 1] + "}"
        try:
            return json.loads(trimmed, strict=False)
        except json.JSONDecodeError:
            pass

    return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=2, max=10))
def _gemini_json(prompt: str, max_tokens: int = 4096) -> dict:
    """Run a Gemini call expecting JSON output. Robust to:
      - Markdown code fences around the JSON
      - Embedded newlines / control chars in strings (json.loads strict=False)
      - Truncation mid-string at max_output_tokens (salvage parser closes
        the last valid pair and trims the rest)
      - Prose preamble before the {...} block (extracts first balanced object)

    Default max_tokens bumped 2048 → 4096 because Gemini 2.5 Flash uses
    "thinking" tokens that count against the budget even though they
    don't show in the response. Most JSON output is <200 chars but the
    model can eat 1-3K tokens on reasoning. 4096 leaves comfortable
    headroom for both.
    """
    client = genai.Client(api_key=config.GOOGLE_API_KEY)
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
            max_output_tokens=max_tokens,
        ),
    )
    raw = resp.text or "{}"
    parsed = _extract_json_object(raw)
    if parsed is None:
        # Log a redacted snippet so future debugging is easier
        log.warning("_gemini_json: could not extract JSON from %d-char response: %r",
                    len(raw), raw[:200])
        raise json.JSONDecodeError("could not parse Gemini response", raw[:200], 0)
    return parsed


def _profile_context_block(profile: dict | None) -> str:
    """Render the persistent worker profile into a context block the LLM can use."""
    if not profile:
        return "PRIOR PROFILE: (none yet — this may be the first day tracking this worker)"
    fields = [
        ("Role / What they do", profile.get("Role / What They Do")),
        ("Recurring tasks", profile.get("Recurring Tasks")),
        ("Known strengths", profile.get("Known Strengths")),
        ("Known blockers / skill gaps", profile.get("Known Blockers / Skill Gaps")),
        ("Tools they use", profile.get("Tools They Currently Use")),
        ("Automation opportunities still OPEN", profile.get("Automation Opportunities (Open)")),
        ("Automation opportunities SHIPPED", profile.get("Automation Opportunities (Shipped)")),
        ("Productivity patterns", profile.get("Productivity Patterns")),
    ]
    lines = ["PRIOR PROFILE (what we already know about this worker):"]
    for label, val in fields:
        if val:
            lines.append(f"- {label}: {val}")
    return "\n".join(lines) if len(lines) > 1 else "PRIOR PROFILE: (empty — first analysis)"


_DAILY_SYSTEM_PROMPT = """You are an AI ops/HR analyst for a small team at Hey Girl Tea. Your
job is to audit each worker's day to help the manager spot three things:

1. Tasks that could be AUTOMATED (specific scripts, no-code tools, AI
   prompts, integrations).
2. MANUAL or repetitive grunt-work that's eating their time.
3. COMMITMENTS the worker made today (things they said they'd do or
   coordinate with someone about) — so Sam can follow up later.

You have history about each worker. Use it. If their prior profile already
flagged an automation opportunity and they're doing the same manual task
AGAIN today, call that out explicitly ("REPEAT: X was flagged before and
they're still doing it manually").

Reasoning approach:
- Read the worker's prior profile and known tools/processes first
- Then read today's check-ins carefully
- Cross-reference: are they describing tasks that match patterns from their
  open automation backlog? Are they using tools you already know about, or
  new ones? Are their replies getting vaguer over the day (slack signal)?
- Capacity signal calibration:
    * spare capacity = short answers, light workload, finished early
    * stretched     = lots of work, long replies, time pressure
    * stuck         = explicit help/blocker signals
    * balanced      = none of the above signals dominate

Output schema — JSON ONLY, matching this shape exactly:
{
  "day_summary": "2-3 sentences plain English: what they actually did today, referencing prior-profile patterns where relevant",
  "automation_opportunities": ["concrete bullet ≤25 words; if repeating a prior flag, prefix with 'REPEAT: '", ...],
  "manual_red_flags": ["concrete bullet ≤25 words pointing at manual/repetitive work", ...],
  "capacity_signal": "one of: spare capacity | balanced | stretched | stuck",
  "new_commitments": [
    {"commitment": "concise statement of what they said they'd do",
     "mentioned_person": "name if they're coordinating with a teammate, else empty"}
  ],
  "resolved_commitments": ["exact text of an earlier-recorded open commitment that today's check-ins indicate is now done"]
}

Rules:
- Max 5 items per analytical list. Empty list if nothing genuine.
- COMMITMENTS specifically: include things like "I'll set up the TikTok accounts
  later", "I'll talk to Rey about [X]", "going to follow up with the supplier",
  "need to fix the report tomorrow". Skip vague intent without object ("I'll do more").
- For resolved_commitments: ONLY include items that match an existing OPEN commitment
  from the prior profile / past activity that today's check-ins confirm is done.
  Match by intent, not exact text. Use the original commitment's exact text in the
  array so it can be programmatically marked done.
- Each bullet must reference something the worker actually did or said today.
- Output ONLY the JSON. No prose before or after. No code fences."""


def _build_daily_user_block(name: str, login_local: str, eod_local: str, active_hours: float,
                            help_count: int, missed: int,
                            checkins: Iterable[tuple[datetime, str]],
                            profile: dict | None,
                            knowledge: list[dict] | None = None,
                            open_commitments: list[dict] | None = None) -> str:
    """The variable per-worker context — comes AFTER the cached system prompt."""
    lines = [
        f"[{t.strftime('%H:%M')}] {m.strip() or '(empty reply)'}"
        for t, m in checkins
    ]
    checkin_block = "\n".join(lines) if lines else "(no check-in replies recorded)"
    profile_block = _profile_context_block(profile)
    knowledge_block = _knowledge_block(knowledge or [])

    commit_block = ""
    if open_commitments:
        commit_lines = ["OPEN COMMITMENTS from earlier (use to decide which are now resolved):"]
        for c in open_commitments[:20]:
            txt = (c.get("Commitment") or "").strip()
            created = (c.get("Date Created") or "").strip()
            commit_lines.append(f"- [{created}] \"{txt}\"")
        commit_block = "\n" + "\n".join(commit_lines) + "\n"

    return f"""{profile_block}

{knowledge_block}{commit_block}

TODAY:
Worker: {name}
Login (local): {login_local}    EOD: {eod_local}    Active: {active_hours}h
Help requests today: {help_count}    Missed prompts: {missed}

Check-in replies (chronological):
{checkin_block}"""


def analyze(name: str, login_local: str, eod_local: str, active_hours: float,
            help_count: int, missed: int,
            checkins: list[tuple[datetime, str]],
            profile: dict | None = None,
            knowledge: list[dict] | None = None,
            open_commitments: list[dict] | None = None) -> dict:
    """Daily analysis. Never raises. profile is the persistent Worker Profile row."""
    if not config.GOOGLE_API_KEY:
        log.info("No GOOGLE_API_KEY; skipping Gemini analysis for %s", name)
        return dict(EMPTY_DAILY)
    if not checkins:
        return dict(EMPTY_DAILY)
    try:
        user_block = _build_daily_user_block(
            name, login_local, eod_local, active_hours, help_count, missed, checkins,
            profile, knowledge, open_commitments=open_commitments,
        )
        data = deep_brain.deep_json(_DAILY_SYSTEM_PROMPT, user_block, max_tokens=4000)
    except Exception as e:
        log.warning("Deep-brain daily analysis failed for %s: %s", name, e)
        return dict(EMPTY_DAILY)

    out = dict(EMPTY_DAILY)
    if isinstance(data.get("day_summary"), str):
        out["day_summary"] = data["day_summary"].strip()
    for k in ("automation_opportunities", "manual_red_flags"):
        v = data.get(k)
        if isinstance(v, list):
            out[k] = [str(x).strip() for x in v if str(x).strip()][:5]
    if isinstance(data.get("capacity_signal"), str):
        out["capacity_signal"] = data["capacity_signal"].strip().lower()
    nc = data.get("new_commitments")
    if isinstance(nc, list):
        cleaned = []
        for c in nc[:10]:
            if isinstance(c, dict) and c.get("commitment"):
                cleaned.append({
                    "commitment": str(c["commitment"]).strip(),
                    "mentioned_person": str(c.get("mentioned_person", "")).strip(),
                })
        out["new_commitments"] = cleaned
    rc = data.get("resolved_commitments")
    if isinstance(rc, list):
        out["resolved_commitments"] = [str(x).strip() for x in rc if str(x).strip()][:10]
    return out


_WEEKLY_SYSTEM_PROMPT = """You are an AI ops/HR analyst doing the WEEKLY profile update for one
worker on a small team. Your job: turn the past week of activity into a
durable picture of who this worker is, what they actually do, what they're
good at, what slows them down, and what work could be automated.

You will read:
1. The prior profile (last week's snapshot — may be empty if this is week 1)
2. This week's daily summaries
3. This week's raw check-in messages

Then output a REPLACEMENT profile. Carry forward anything from the prior
profile that still seems true, REVISE anything contradicted by this week,
and ADD anything new. Be specific and concrete — no consultant-speak.

Critical: for "automation_opportunities_open", carry forward unshipped items
from the prior profile UNLESS the worker clearly addressed them this week.
If they're STILL doing a task you flagged 2+ weeks ago, that's worth noting
explicitly with a "[N weeks open]" prefix. For "automation_opportunities_shipped",
add any that were addressed.

Reasoning approach:
- First skim the daily summaries to get the shape of the week
- Then read the raw check-ins for texture (what they actually said)
- Compare against the prior profile — what's confirmed, what's contradicted, what's new
- For each list field, pull from concrete evidence in the data, not guesses

Output schema — JSON ONLY, matching this shape exactly:
{
  "role_what_they_do": "1-2 sentences plain English: what their actual day-to-day work is",
  "recurring_tasks": ["bullet ≤20 words", ...],
  "known_strengths": ["bullet ≤20 words", ...],
  "known_blockers": ["bullet ≤20 words", ...],
  "tools_currently_used": ["tool name, brief context", ...],
  "automation_opportunities_open": ["concrete proposal ≤25 words; if carried over, prefix '[N weeks open] '", ...],
  "automation_opportunities_shipped": ["what got automated and roughly when", ...],
  "productivity_patterns": ["bullet ≤25 words: time-of-day, day-of-week, energy patterns", ...],
  "coaching_notes_for_manager": ["bullet ≤30 words: what manager should know, ask about, or coach on", ...]
}

Rules:
- Lists max 8 items each. Empty list OK.
- Pull evidence from THIS WEEK; do not invent.
- Output ONLY the JSON. No prose before or after. No code fences."""


def _build_weekly_user_block(name: str, prior_profile: dict | None,
                             recent_summaries: list[dict],
                             recent_activity: list[dict]) -> str:
    """Variable per-worker context for the weekly synth — comes AFTER the cached system prompt."""
    prior_block = _profile_context_block(prior_profile)
    summary_lines = []
    for s in recent_summaries:
        summary_lines.append(
            f"- {s.get('Date')}: {s.get('Active Hours')}h, "
            f"{s.get('Check-ins')} checkins, status={s.get('Status')}, "
            f"capacity={s.get('Capacity Signal')}. "
            f"Summary: {s.get('Day Summary')}. "
            f"Auto-flagged: {s.get('Automation Ideas')}. "
            f"Manual: {s.get('Manual Red Flags')}."
        )
    summary_block = "\n".join(summary_lines) if summary_lines else "(no daily summaries this week)"

    raw_lines = []
    for r in recent_activity[-200:]:  # last 200 events plenty
        t = r.get("Type", "")
        if t in ("checkin", "help_request", "login", "eod"):
            raw_lines.append(f"  [{r.get('Local Date')} {r.get('Local Time')}] {t}: {r.get('Message', '')}")
    raw_block = "\n".join(raw_lines) if raw_lines else "(no raw activity)"

    return f"""WORKER: {name}

{prior_block}

DAILY SUMMARIES THIS WEEK:
{summary_block}

RAW CHECK-INS THIS WEEK:
{raw_block}"""


def _knowledge_block(knowledge: list[dict]) -> str:
    if not knowledge:
        return "Known tools/processes for this worker: (none yet — bot is still learning)"
    lines = ["Known tools/processes for this worker:"]
    for k in knowledge:
        kind = k.get("Kind", "")
        name = k.get("Name", "")
        url = k.get("URL", "")
        desc = k.get("Description", "")
        bits = [f"{kind}: {name}"]
        if url:
            bits.append(f"({url})")
        if desc:
            bits.append(f"— {desc}")
        lines.append("- " + " ".join(bits))
    return "\n".join(lines)


def maybe_ask_followup(name: str, message: str, knowledge: list[dict],
                       already_asked_today: list[str]) -> dict | None:
    """Decide whether to ask a follow-up question about something the worker
    mentioned. Returns {"ask": str, "topic": str} or None.

    The bot should only ask when something genuinely new is mentioned that
    would be useful to capture in the knowledge base. NOT for every check-in.
    """
    if not config.GOOGLE_API_KEY:
        return None
    if not message or not message.strip():
        return None

    knowledge_block = _knowledge_block(knowledge)
    already_str = ", ".join(already_asked_today) if already_asked_today else "(nothing yet)"

    prompt = f"""You are Sam — building a deep map of how every worker on this small
team gets their work done. Tools they use, sheets they reference, people they
coordinate with, links they share, jobs they handle. After a worker sends a
check-in, you decide whether to ask ONE follow-up question to capture
something you haven't logged yet.

BE AGGRESSIVELY CURIOUS — like a new coworker getting onboarded. Err HARD
on the side of asking. The goal is to build a complete picture so a future
admin (or Sam) can answer "what does X actually do all day, with what, and
with whom". If they mention literally anything you don't already know, ASK.

ASK ABOUT ANY of these when they're new (not in the known list):
- TOOLS / SOFTWARE / SaaS by name (Notion, Loom, Canva, ChatGPT, Shopify)
- INTERNAL TOOLS or apps ("the tracker", "the dashboard", "the CMS")
- SHEETS / DOCS / FILES (their lead sheet, pricing doc) — ALWAYS ask for
  the link if there is one
- PROCESSES / WORKFLOWS ("the morning audit", "Q3 review", "onboarding flow")
- JOBS / TASKS the worker described that you don't fully understand
  ("revising the second video" — what's the video about? who's it for?
  what tool are you editing in?)
- PEOPLE THEY CONTACTED OR COORDINATED WITH ("coordinated with Lance" —
  who's Lance? what's his role? how do you usually coordinate?)
- INTEGRATIONS / AUTOMATIONS / SCRIPTS / CLIs
- PLATFORMS / MARKETPLACES (Amazon Seller Central, Walmart, TikTok Shop)
- COMPLIANCE / POLICY TOPICS they referenced ("body claims",
  "ToS-safe wording") — ask for the rule sheet / checklist
- LINKS or URLs mentioned without context — ask what it is

ASK in the form of a statement of intent — Sam is mapping the workflow,
not asking permission. ALWAYS anchor to the CURRENT task they just
mentioned so it doesn't feel like out-of-context interrogation.

ALWAYS pair NAME + PURPOSE + workflow anchor. For people, also ask their
ROLE and how they coordinate. For sheets/docs, ALWAYS ask for the link.

GOOD examples (specific, anchored, captures real workflow context):
- TOOL: "quick one — what're you using to edit those videos? want to map your editing tools."
- SHEET: "what's the lead sheet exactly and what's it for? drop the link if there is one — i want to keep a record of the sheets you use day to day."
- PROCESS: "what does the morning audit involve step by step? i want to map out 'morning audit' going forward."
- PERSON: "quick — who's Lance and how do you usually coordinate with him? trying to map who you work with."
- JOB: "tell me a bit more about the second video — what's it about, who's it for, where does it post? want to capture what 'video work' actually involves for you."
- LINK: "what's that doc? mind dropping a one-liner on what's in it so i can log it?"
- COMPLIANCE: "you mentioned 'body claims' — is there a rule sheet or checklist you follow for that? want to log it."

BAD examples (avoid):
- "want me to remember that?" (asks permission, not a workflow question)
- "tell me about X" (vague, no anchor)
- "what tools do you use?" (open-ended, not anchored)
- "thanks for the update! 🙌" (NEVER. this is what we're replacing.)

DO NOT ASK IF:
- Already in the known list (no point re-asking)
- Already asked about it today (see list below)
- Pure casual content (food, mood, family, weather)
- Generic verbs with NO specific concept ("did some work", "wrote some
  emails") — wait for them to name something specific

The follow-up must be short (<= 35 words), lowercase, friendly, written
in Sam's voice. ONE concrete unknown per question — don't combine.

Worker: {name}
Their check-in: "{message.strip()}"

{knowledge_block}

Already asked about today: {already_str}

Respond as JSON ONLY:
{{
  "ask": "the follow-up message text, or null if nothing worth asking",
  "topic": "1-4 word label of what the question is about (e.g. 'video editor', 'lance contact', 'body claims policy', 'lead sheet'), or null"
}}
"""
    try:
        # 4096 — Gemini 2.5 Flash thinking tokens eat budget. Previous
        # 512 then 1024 both still got truncated mid-string. 4096 is the
        # standard everywhere now.
        data = _gemini_json(prompt, max_tokens=4096)
    except Exception as e:
        log.warning("Follow-up generation failed for %s: %s", name, e)
        return None

    ask = data.get("ask")
    topic = data.get("topic")
    if not ask or ask in (None, "null") or not topic:
        return None
    ask_text = str(ask).strip()
    topic_text = str(topic).strip().lower()
    if not ask_text or ask_text.lower() in ("null", "none"):
        return None
    return {"ask": ask_text, "topic": topic_text}


def extract_knowledge_from_reply(name: str, reply_text: str, asked_topic: str | None,
                                  existing_knowledge: list[dict]) -> list[dict]:
    """Given a worker's reply (often to a follow-up question), extract any
    tools/sheets/docs/processes they referenced. Returns a list of dicts
    matching KNOWLEDGE_HEADER columns (minus Worker/SlackID/timestamps).

    Failsafe: returns [] on any error.
    """
    if not config.GOOGLE_API_KEY or not reply_text or not reply_text.strip():
        return []

    urls = URL_RE.findall(reply_text)
    urls_block = ", ".join(urls) if urls else "(none)"
    existing_names = [k.get("Name", "") for k in existing_knowledge if k.get("Name")]
    existing_str = ", ".join(existing_names) if existing_names else "(none)"

    prompt = f"""Extract every concrete thing the worker referenced in their message —
tools, software, sheets, docs, processes, projects, PEOPLE they coordinate
with, LINKS they shared, named JOBS or RECURRING TASKS, or COMPLIANCE
TOPICS. Capture NAME + PURPOSE — the description is the most important
piece because that's how Sam will explain it to other admins later.

Worker: {name}
Their message: "{reply_text.strip()}"
URLs detected in the message: {urls_block}
Question that was just asked of them (if any): {asked_topic or "(none — they spoke unprompted)"}
Already-known item names for this worker: {existing_str}

Respond as JSON ONLY:
{{
  "items": [
    {{
      "kind": "software|tool|sheet|doc|process|workflow|project|platform|person|link|job|compliance",
      "name": "concise human-readable name",
      "url": "URL if mentioned (use one from the detected URLs if relevant) or empty",
      "description": "1-2 sentences in plain English: what it is AND what this worker uses it for",
      "steps": "optional — bullet list for processes, otherwise empty"
    }}
  ]
}}

Rules:
- Only output things genuinely referenced in their message. Don't invent.
- Max 4 items per call.
- "kind" guidance:
    software   = a third-party app (Notion, Airtable, Loom, Canva, Shopify, ChatGPT)
    tool       = internal/custom tools, scripts, integrations
    sheet      = Google Sheets URL
    doc        = Google Doc / Notion page / similar
    process    = a named workflow ('Q3 audit', 'morning audit', 'customer review')
    platform   = a marketplace ('Amazon Seller Central', 'Walmart', 'TikTok Shop')
    person     = a colleague / contact they coordinate with (Lance, Ideen, a vendor)
    link       = a standalone URL with no obvious parent kind
    job        = a recurring task / responsibility ('video editing for IG',
                 'inventory check', 'ad uploads to TikTok')
    compliance = a rule/policy they follow ('body claims policy', 'ToS-safe wording')
- For "person" kind, name=their name, description="their role + how this
  worker coordinates with them" (e.g. "Lance — vendor for tea supplies.
  Hannah emails Lance to coordinate inventory restocks").
- For "job" kind, description must explain what the recurring task ACTUALLY
  involves — the inputs, the steps, the output.
- DESCRIPTION must answer: what is it + what does THIS worker use it for.
- If they didn't actually answer (e.g. just said "ok"), return {{"items": []}}.
- Output ONLY the JSON.
"""
    try:
        data = _gemini_json(prompt, max_tokens=4096)
    except Exception as e:
        log.warning("Knowledge extraction failed for %s: %s", name, e)
        return []

    items = data.get("items")
    if not isinstance(items, list):
        return []
    cleaned = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        name_val = str(it.get("name", "")).strip()
        if not name_val:
            continue
        cleaned.append({
            "Kind": str(it.get("kind", "tool")).strip().lower(),
            "Name": name_val,
            "URL": str(it.get("url", "")).strip(),
            "Description": str(it.get("description", "")).strip(),
            "Steps / Notes": str(it.get("steps", "")).strip(),
        })
    return cleaned


def parse_time_off_log(text: str, roster: list[dict], today_iso: str) -> dict | None:
    """Parse 'log vacation for hannah dec 1-5' / 'sick day for rey today' / etc.
    Returns dict with worker_name, type, start_date, end_date, days, notes — or None.
    """
    if not config.GOOGLE_API_KEY or not text.strip():
        return None

    worker_lines = []
    for w in roster:
        nicks = (w.get("nicknames") or [])
        nick_str = f" (aka: {', '.join(nicks)})" if nicks else ""
        worker_lines.append(f"- {w['name']}{nick_str}")
    roster_block = "\n".join(worker_lines)

    prompt = f"""Parse a manager's time-off log entry into structured data.

KNOWN WORKERS (match against these — use EXACT full names):
{roster_block}

TODAY'S DATE: {today_iso}

THE LOG ENTRY:
{text.strip()}

Output JSON only:
{{
  "worker_name": "<exact full name from roster, or empty string if no match>",
  "type": "vacation | sick | pto | personal | unpaid | holiday",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "days": <integer count of weekdays, treating start and end inclusive>,
  "notes": "<any extra context from the message, or empty>"
}}

Rules:
- Match nicknames/first names to full roster name
- "today" → today's date. "tomorrow" → tomorrow's date.
- "this friday" → the upcoming Friday from today's date.
- "dec 15-20" → start=2026-12-15 end=2026-12-20 (or whatever year makes sense — usually current or next)
- Single-day entry: start_date == end_date
- "days" = number of WEEKDAYS in the range (skip weekends unless explicitly stated as weekend days)
- If you can't parse cleanly, return all-empty fields; the caller will handle it
- Output ONLY the JSON.
"""
    try:
        data = _gemini_json(prompt, max_tokens=512)
    except Exception as e:
        log.warning("Time off parse failed: %s", e)
        return None

    name = str(data.get("worker_name", "")).strip()
    if not name:
        return None
    return {
        "worker_name": name,
        "type": str(data.get("type", "vacation")).strip().lower(),
        "start_date": str(data.get("start_date", "")).strip(),
        "end_date": str(data.get("end_date", "")).strip(),
        "days": int(data.get("days", 1) or 1),
        "notes": str(data.get("notes", "")).strip(),
    }


def parse_benefits_reply(text: str, roster: list[dict]) -> dict[str, dict]:
    """Parse a benefits-info reply like 'jonny gets 10 vacation 5 sick. hannah: 15/10. rey 8 vac.'
    Returns {worker_name: {vacation_days, sick_days, pto_days, notes}}.
    """
    if not config.GOOGLE_API_KEY or not text.strip():
        return {}

    worker_lines = []
    for w in roster:
        nicks = (w.get("nicknames") or [])
        nick_str = f" (aka: {', '.join(nicks)})" if nicks else ""
        worker_lines.append(f"- {w['name']}{nick_str}")
    roster_block = "\n".join(worker_lines)

    prompt = f"""A manager replied with each worker's benefit allocations.
Parse it into structured data per worker.

KNOWN WORKERS (match against these — use EXACT full names):
{roster_block}

THE MANAGER'S REPLY:
{text.strip()}

Output JSON only:
{{
  "workers": [
    {{
      "worker_name": "<exact full name>",
      "vacation_days": <int per year>,
      "sick_days": <int per year>,
      "pto_days": <int per year — leave 0 if vacation+sick are tracked separately>,
      "notes": "<any extra context like 'rolls over', 'unlimited PTO', etc., or empty>"
    }}
  ]
}}

Rules:
- Match nicknames/first names to full roster name
- If a number isn't given for a benefit, set it to 0
- "unlimited" → set days to 0 and note "unlimited" in notes
- "no benefits" / "contractor" → all zeros, note "contractor — no PTO benefits"
- Only include workers the manager mentioned
- Output ONLY the JSON.
"""
    try:
        data = _gemini_json(prompt, max_tokens=2048)
    except Exception as e:
        log.warning("Benefits parse failed: %s", e)
        return {}

    result: dict[str, dict] = {}
    for item in (data.get("workers") or [])[:30]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("worker_name", "")).strip()
        if not name:
            continue
        result[name] = {
            "vacation_days": int(item.get("vacation_days", 0) or 0),
            "sick_days": int(item.get("sick_days", 0) or 0),
            "pto_days": int(item.get("pto_days", 0) or 0),
            "notes": str(item.get("notes", "")).strip(),
        }
    return result


def parse_daily_assignments(reply_text: str, roster: list[dict]) -> dict[str, str]:
    """Take the manager's free-form reply ('jonny: finish the report. hannah: continue.')
    and return {worker_name: assignment_string}. Empty dict if the manager said
    'all continue' / 'no changes' / similar. Failsafe: returns {} on error.
    """
    if not config.GOOGLE_API_KEY or not reply_text.strip():
        return {}

    worker_lines = []
    for w in roster:
        nicks = (w.get("nicknames") or [])
        nick_str = f" (aka: {', '.join(nicks)})" if nicks else ""
        worker_lines.append(f"- {w['name']}{nick_str}")
    roster_block = "\n".join(worker_lines)

    prompt = f"""You're parsing a manager's reply about what each worker should focus on.

KNOWN WORKERS (match against these — use EXACT full names in output):
{roster_block}

MANAGER'S REPLY:
{reply_text.strip()}

Parse this into JSON:
{{
  "assignments": [
    {{"worker_name": "<exact full name from the roster above>", "assignment": "<the task they should focus on, or 'continue' if the manager said to keep going>"}}
  ]
}}

Rules:
- Match nicknames/first names to the full roster name (e.g. "norks" → "Norlan Baluncio Burce")
- If the manager said "all continue" / "everyone good" / "no changes" / similar → return {{"assignments": []}}
- Only include workers the manager actually mentioned; omit unmentioned workers
- For workers told to "continue" / "keep going" / "same as yesterday" → set assignment to exactly "continue"
- For specific tasks, copy the manager's words verbatim (don't paraphrase)
- Output ONLY the JSON. No prose.
"""
    try:
        data = _gemini_json(prompt, max_tokens=1024)
    except Exception as e:
        log.warning("Failed to parse daily assignments: %s", e)
        return {}

    result: dict[str, str] = {}
    for item in (data.get("assignments") or [])[:30]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("worker_name", "")).strip()
        assignment = str(item.get("assignment", "")).strip()
        if name and assignment:
            result[name] = assignment
    return result


def parse_relay_request(admin_text: str, roster: list[dict]) -> dict | None:
    """Take an admin DM like 'when ger logs in tell her to fix the listing —
    should only take 15 min' and return {worker_name, slack_user_id, message,
    estimated_time}. Returns None if no actionable relay was found.

    The intent regex in config.ADMIN_RELAY_PATTERNS catches the trigger; this
    function does the structured extraction. Failsafe: returns None on error.
    """
    if not config.GOOGLE_API_KEY or not admin_text.strip():
        return None

    worker_lines = []
    for w in roster:
        nicks = (w.get("nicknames") or [])
        nick_str = f" (aka: {', '.join(nicks)})" if nicks else ""
        worker_lines.append(f"- {w['name']} [{w['user_id']}]{nick_str}")
    roster_block = "\n".join(worker_lines)

    prompt = f"""You're parsing an admin's request that should be relayed to a worker
the next time the worker logs in (or right now if already online).

KNOWN WORKERS (match against these — use EXACT full name + Slack ID in output):
{roster_block}

ADMIN'S MESSAGE:
{admin_text.strip()}

Extract the relay into JSON:
{{
  "worker_name": "<exact full name from the roster, or empty if no specific worker named>",
  "slack_user_id": "<the matching Slack ID in brackets above, or empty>",
  "message": "<the task/message to deliver to the worker, phrased as a direct ask, e.g. 'can you quickly fix the listing — should only take 15 min'>",
  "estimated_time": "<time estimate if mentioned (e.g. '15 min', '1 hour'), else empty>"
}}

Rules:
- Match nicknames/first names to full roster name (e.g. "ger" → "Ger Vargas").
- The "message" should be a clean, direct request — strip out the "when X logs in" framing because Sam will deliver it AT login time. e.g. input "when ger logs in tell her to fix the listing, should only take 15 min" → message "can you fix the listing? should only take about 15 min".
- Preserve any time estimate the admin mentioned ("15 min", "an hour", "quick task").
- If no specific worker is named or you can't identify them, return empty strings.
- Output ONLY the JSON. No prose.
"""
    try:
        # 4096 — see _gemini_json docstring re: thinking tokens.
        data = _gemini_json(prompt, max_tokens=4096)
    except Exception as e:
        log.warning("Failed to parse relay request: %s", e)
        return None

    name = str(data.get("worker_name", "")).strip()
    uid = str(data.get("slack_user_id", "")).strip()
    msg = str(data.get("message", "")).strip()
    est = str(data.get("estimated_time", "")).strip()
    if not (name and uid and msg):
        return None
    return {
        "worker_name": name,
        "slack_user_id": uid,
        "message": msg,
        "estimated_time": est,
    }


def check_relay_completion(worker_reply: str, open_relays: list[dict]) -> list[dict]:
    """Given a worker's incoming message and the list of relays delivered to
    them that aren't yet done, return a list of {relay_id, completed: bool,
    quote: str} for each relay the worker plausibly addressed. Empty list if
    nothing matches.

    Only flags relays where the worker is clearly indicating progress or
    completion — vague acknowledgements like "ok thanks" don't count.
    """
    if not config.GOOGLE_API_KEY or not worker_reply.strip() or not open_relays:
        return []

    relay_lines = []
    for r in open_relays:
        rid = r.get("Relay ID", "")
        msg = r.get("Message", "")
        delivered = r.get("Date Delivered", "")
        relay_lines.append(f"- ID={rid} | delivered {delivered} | ask: {msg}")
    relay_block = "\n".join(relay_lines)

    prompt = f"""A worker was given one or more ad-hoc tasks recently. They just sent
this message. Determine which (if any) of the open tasks they're confirming as
DONE or making meaningful progress on.

OPEN TASKS (relays delivered to this worker, awaiting completion):
{relay_block}

WORKER'S NEW MESSAGE:
{worker_reply.strip()}

Output JSON:
{{
  "completed": [
    {{"relay_id": "<ID from the list above>", "quote": "<short snippet from the worker's message that proves it's done>"}}
  ]
}}

Rules:
- Only include relays the worker actually mentioned doing / finishing / handling.
- A vague "ok will do" or "thanks" does NOT count as completion — that's just acknowledgement.
- A clear "done with the listing" / "sent it" / "fixed it" / "uploaded the file" DOES count.
- If they say "still working on it" / "in progress" — do NOT mark as completed.
- If nothing matches, return {{"completed": []}}.
- Output ONLY the JSON.
"""
    try:
        data = _gemini_json(prompt, max_tokens=512)
    except Exception as e:
        log.warning("check_relay_completion failed: %s", e)
        return []

    out: list[dict] = []
    for item in (data.get("completed") or [])[:10]:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("relay_id", "")).strip()
        quote = str(item.get("quote", "")).strip()
        if rid:
            out.append({"relay_id": rid, "quote": quote})
    return out


def generate_checkin_prompt(worker: dict, today_events: list[dict]) -> str | None:
    """Generate a contextual check-in DM that references the worker's recent
    activity. Returns None on failure so caller can fall back to generic prompt.
    """
    if not config.GOOGLE_API_KEY:
        return None

    first = (worker.get("name") or "friend").split()[0]

    # Reconstruct today's state from event log
    last_login = None
    last_checkin_text = None
    last_checkin_time = None
    plan_text = None  # the first checkin after login = their "plan for the day"
    for e in today_events:
        t = e.get("Type", "")
        ts_str = e.get("Local Time", "")
        msg = e.get("Message", "")
        if t == "login":
            last_login = ts_str
        elif t in ("checkin", "help_request"):
            if last_login and not plan_text:
                plan_text = msg
            last_checkin_text = msg
            last_checkin_time = ts_str

    context_lines = [f"Worker: {first}", f"Their TZ: {worker.get('tz', 'UTC')}"]
    if last_login:
        context_lines.append(f"Logged in today at: {last_login}")
    if plan_text and plan_text != last_checkin_text:
        context_lines.append(f"Plan they shared at login: \"{plan_text[:200]}\"")
    if last_checkin_text:
        context_lines.append(f"Their most recent check-in (at {last_checkin_time}): \"{last_checkin_text[:250]}\"")
    else:
        context_lines.append("(no check-in replies yet today)")

    prompt = f"""You are Sam, an AI ops assistant for Hey Girl Tea. You're about to send
a periodic check-in DM to a worker to see what they've been doing in the
last ~2 hours. This is the kind of DM that gets sent automatically every
couple hours during their shift.

DON'T sound like a robot. Reference what they said last (so they feel heard),
keep it warm, lowercase, casual. 1 sentence, ≤25 words. Use their first name
in lowercase.

CONTEXT:
{chr(10).join(context_lines)}

GOOD examples (warm, contextual, short):
- "hey rey, last I heard you were on the customer review pipeline — still on that or onto something else?"
- "yo norks, how's it going? any wins, any blockers from the last bit?"
- "hey ger 👋 anything to flag, or smooth sailing this stretch?"
- "hey hannah, you mentioned the lead tracker earlier — get through it, or still digging?"

CROSS-DAY FOLLOW-UPS (use when there's an aging open commitment listed above
and it fits naturally — prefer this over a generic 'how's it going' if the
commitment is fresh enough to actually be in motion):
- "hey ger, did you end up talking to rey about the tiktok accounts? curious if that got moving."
- "yo hannah, the walmart case follow-up from yesterday — any update there?"
- "hey jonny, did you get the supplier emails out you mentioned earlier?"

BAD examples (robotic, generic, doesn't reference context):
- "Hi Rey. Please describe what you have been working on."
- "hey rey 👋 quick one — what'd you knock out the last bit? all good or stuck on anything?"

Output ONLY the prompt text — no quotes, no preamble, no JSON.
"""
    try:
        client = genai.Client(api_key=config.GOOGLE_API_KEY)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.8, max_output_tokens=300),
        )
        reply = (resp.text or "").strip()
        if not reply:
            return None
        if (reply.startswith('"') and reply.endswith('"')) or (reply.startswith("'") and reply.endswith("'")):
            reply = reply[1:-1]
        return reply
    except Exception as e:
        log.warning("generate_checkin_prompt failed for %s: %s", first, e)
        return None


def classify_admin_intent(message: str, worker_names: list[str]) -> dict | None:
    """Fuzzy intent classifier for admin messages that didn't match any regex.
    Returns one of:
      {"intent": "worker_status", "worker": "Hannah", "confidence": "high"|"medium"|"low"}
      {"intent": "team_status", "confidence": "..."}
      {"intent": "digest_now", "confidence": "..."}
      {"intent": "other"}
    or None on failure.

    The point: regex patterns are brittle to typos ("watt did hannah do",
    "is jonny worken", "wuts hannah doing"). Flash is smart enough to
    understand these — we just need to ask it. This is the LAST stop before
    falling through to general conversational reply, so we use cheap
    short-prompt Gemini calls (~1s).
    """
    if not config.GOOGLE_API_KEY or not message.strip():
        return None
    names_block = ", ".join(worker_names) if worker_names else "(none)"
    prompt = f"""Classify this admin message. The admin uses Sam, a worker
tracking bot, and might be asking about a specific worker, the whole team, or
asking Sam to do something else. Worker names on the roster: {names_block}

ADMIN MESSAGE: "{message.strip()}"

Decide ONE of:
- "worker_status": admin is asking about ONE specific worker's status, hours,
  current activity, or what they did today. Even if the name is misspelled
  ("jonyn", "hanna", "rey lui"), match to the closest roster name.
- "team_status": admin is asking about EVERYONE / the whole team / who's
  working / who hasn't logged in.
- "digest_now": admin wants the EOD digest sent now ("send digest", "EOD
  report").
- "other": anything else (casual chat, command not handled by the above).

START YOUR RESPONSE WITH the open-brace character. No preamble like "Here
is the JSON" or "Sure, I'll classify". Output JSON ONLY:
{{
  "intent": "worker_status" | "team_status" | "digest_now" | "other",
  "worker": "<exact roster name if worker_status, else null>",
  "confidence": "high" | "medium" | "low"
}}

Be CONSERVATIVE: if you're not sure, return "other". If the message is just
"hey" or "how's it going" — return "other" (it's just chat, not a query).
"""
    try:
        # 4096 — Gemini 2.5 Flash thinking tokens eat budget. See
        # _gemini_json docstring.
        data = _gemini_json(prompt, max_tokens=4096)
    except Exception as e:
        log.warning("classify_admin_intent failed: %s", e)
        return None
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in ("worker_status", "team_status", "digest_now", "other"):
        return None
    worker = data.get("worker")
    if worker and not isinstance(worker, str):
        worker = None
    confidence = str(data.get("confidence", "low")).strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    return {"intent": intent, "worker": worker, "confidence": confidence}


def conversational_reply(message: str, speaker_name: str, is_owner: bool,
                          is_manager: bool, is_worker: bool = False,
                          recent_context: str = "",
                          team_state: str = "") -> str | None:
    """Generate a useful conversational reply for messages that didn't match a
    specific command pattern. Returns None to stay silent.

    team_state: for admin speakers, a compact block describing each worker's
    current state — lets Sam answer follow-up questions like 'why did Hannah
    log 0 hours' or 'is Rey done' without the admin re-issuing 'status of X'.
    """
    if not config.GOOGLE_API_KEY or not message.strip():
        return None

    if is_owner:
        role_block = (
            f"{speaker_name} is an OWNER. They can ask you about any worker, "
            f"trigger team-wide actions ('introduce everyone'), and chat casually."
        )
        capabilities = (
            "Owner-only commands:\n"
            "• 'introduce everyone' → broadcast onboarding DMs\n"
            "• 'send to <worker>: <msg>' → relay a message through you\n\n"
            "Shared admin commands (also for managers):\n"
            "• 'what is <worker> doing' / 'status of <worker>' → live snapshot\n"
            "• 'hours' → see your own pay-period hours\n"
        )
    elif is_manager:
        role_block = (
            f"{speaker_name} is a MANAGER. They can query workers but cannot "
            f"query or relay to owners. They also work normal shifts."
        )
        capabilities = (
            "Manager + worker capabilities:\n"
            "• 'what is <worker> doing' / 'status of <worker>' (workers only, not owners)\n"
            "• 'send to <worker>: <msg>' (workers only, not owners)\n"
            "• 'hours' → your own pay-period hours\n"
            "• Normal worker flow: message to clock in, 'break' to pause, 'EOD' to wrap\n"
        )
    else:
        role_block = (
            f"{speaker_name} is a WORKER being time-tracked. They DM you to "
            f"clock in, take breaks, and EOD. They CANNOT query other workers "
            f"or trigger admin actions."
        )
        capabilities = (
            "What workers can do:\n"
            "• Message you anything to clock in for the day\n"
            "• Say 'break', 'lunch', 'brb' to pause the clock\n"
            "• Any message after a break resumes the clock\n"
            "• Say 'EOD', 'done my shift', 'im out' to wrap up\n"
            "• Ask 'hours' to see their pay-period total\n"
            "• Flag discrepancies ('you missed my lunch', 'should be 8h not 7') for Jan to review\n"
        )

    ctx_block = f"\nRecent context for this worker:\n{recent_context}\n" if recent_context else ""
    team_block = f"\n{team_state}\n" if team_state else ""

    prompt = f"""You are Sam, the AI ops assistant for Hey Girl Tea. You time-track
a small remote team (workers in the Philippines, manager in Vancouver). You're
the workers' main contact for daily check-ins. Your tone is a thoughtful
coworker, not a HR bot.

WHAT YOU ACTUALLY DO (use this to answer 'what can you do' / 'do you X' /
'will you Y' / 'how does Z work' style meta questions):
- Time tracking: workers DM you to clock in, "break"/"lunch"/"brb" pauses
  the clock, "back" resumes, "EOD" ends the day. Hours go to the Timesheet
  tab for payroll on the 1st + 15th.
- Periodic check-ins every 1.5-2h asking workers what they've knocked out
  and if they're stuck. Their replies go to the Activity Log.
- Daily EOD digest sent as a Slack DM to owners — per-worker summary
  with hours, status, automation opportunities, and red flags.
- Task relays: admin says "send this to ger" or "when hannah logs in
  tell her to fix X" — you queue + deliver + track completion.
- Commitment tracking: when a worker says "I'll have X done by 3pm" in
  a check-in, you log it and follow up if it's still open the next day.
- Follow-up questions about tools/processes: when a worker mentions a
  software/tool/sheet/process you haven't catalogued, you ask a quick
  follow-up to capture name + purpose, saved to the Knowledge Base tab.
  Build a real workflow map of who uses what.
- Worker profiles: every Sunday you synthesize each worker's past 7
  days into a profile (role, recurring tasks, strengths, automation
  backlog) on the Worker Profile tab.
- Time off + benefits: workers can ask "how many vacation days do I
  have left", you net allowed days vs used. Admins log time off via
  "vacation for hannah dec 1-5". Annual reminders for HMO / perf
  bonuses on Nov 15 + Jan 5.
- Task lists: "my tasks" (worker) or "tasks for hannah" (admin)
  shows open relays + commitments with age.
- Team status: "did everyone log in" / "team status" / "who's
  working" shows everyone's current state in one DM.
- Status snapshot: "is X working" / "what did X do today" shows
  one worker's hours, login, last check-in, today's trail.

So if Jan asks "will you follow up on processes?" — YES, you do.
Explain it. Don't redirect to a status command.

WHO IS MESSAGING:
{role_block}

THEIR MESSAGE:
"{message.strip()}"
{ctx_block}{team_block}
YOUR CAPABILITIES:
{capabilities}

YOUR VOICE:
- Warm but tight. Lowercase. Like a thoughtful coworker, not a chatbot.
- 1-3 sentences max. NEVER lists/bullets in chat replies.
- Don't say "I can help with that!" or other filler — just answer.
- Concrete > generic. If they ask "what should I do" — name the actual thing.
- Match their energy: short reply for short message, longer if they ask a real question.

ENGAGE WITH THE CONTENT — DO NOT just say "thanks for the update! 🙌":
The single biggest failure mode is responding to a real progress update with
generic ack. If a worker tells you what they're working on, reply to THE
THING they mentioned. Notice it. React. Briefly comment, encourage, or ask
a tiny natural follow-up — like a coworker would. 1-2 sentences max.

GOOD examples (engages with substance):
- Worker: "im at the verge of finishing my first video, thank you for asking"
  → "oh nice — first video is the hardest one. how's the cut looking, you happy with the pacing?"
- Worker: "will be continuing the IG Posts"
  → "got it. how many posts left in this batch? lmk if you hit any blockers."
- Worker: "verified product listings, did inventory check, responded to emails"
  → "solid first half. anything weird in the inventory or did everything line up?"
- Worker: "stuck on the walmart case, customer not replying"
  → "ugh. how long since you last pinged them? sometimes a one-line nudge works."
- Worker: "done with the email batch, taking 5"
  → "nice — enjoy the break. how many emails ended up needing escalation?"
- Worker: "back" (returning from break)
  → "welcome back 🙌" (this one is short by design — no content to engage with)
- Worker: "ok" or "👍" or one-word ack
  → SKIP (truly no content)

BAD examples (avoid — generic, robotic):
- "thanks for the update Norlan! 🙌" (canned, doesn't engage with anything)
- "Great work! Keep it up!" (saccharine, hollow)
- "Noted." (terse, cold)
- "Let me know if you need anything!" (vague filler)

RULES:
- If TEAM STATE is provided and the manager asks about a specific worker (status, hours, what they did, when they started, etc.), USE THE DATA — don't say "I can't pull that." All the data you need is right there.
- If their message is genuinely about your capabilities or how to use you, answer the actual question. Don't be vague.
- If they ask about something outside your tools (jokes, life advice, weather), play along briefly but redirect to what you can do.
- If they're complaining/frustrated, acknowledge it directly. Don't be saccharine.
- For data you genuinely don't have access to, say so honestly. Don't hallucinate.
- Output the REPLY ONLY. No quotes, no preamble, no JSON.
- ONLY output SKIP if the message is literally a single emoji, "ok", "k", "👍" or similar with zero content. ANY substantive message — even a short one about work — gets a real reply.
"""
    try:
        client = genai.Client(api_key=config.GOOGLE_API_KEY)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            # 4096 — Gemini 2.5 Flash thinking tokens were eating the old 1500
            # budget, causing empty replies. Same bug class as we patched in
            # _gemini_json, maybe_ask_followup, classify_admin_intent, etc.
            config=types.GenerateContentConfig(temperature=0.6, max_output_tokens=4096),
        )
        reply = (resp.text or "").strip()
        if not reply or reply.upper() == "SKIP":
            return None
        if (reply.startswith('"') and reply.endswith('"')) or (reply.startswith("'") and reply.endswith("'")):
            reply = reply[1:-1]
        return reply
    except Exception as e:
        log.warning("conversational_reply failed for %s: %s", speaker_name, e)
        return None


def synthesize_weekly_profile(name: str, slack_user_id: str, first_seen: str,
                              prior_profile: dict | None,
                              recent_summaries: list[dict],
                              recent_activity: list[dict]) -> dict:
    """Weekly profile rebuild. Returns a dict matching PROFILE_HEADER columns.

    On Gemini failure, returns the prior_profile unchanged (or a minimal new row).
    """
    today = datetime.now().date().isoformat()
    days_tracked_prior = 0
    if prior_profile:
        try:
            days_tracked_prior = int(prior_profile.get("Days Tracked") or 0)
        except (TypeError, ValueError):
            days_tracked_prior = 0

    fallback = {
        "Worker": name,
        "Slack User ID": slack_user_id,
        "First Seen": first_seen or (prior_profile.get("First Seen") if prior_profile else today),
        "Days Tracked": days_tracked_prior + 7,
        "Role / What They Do": prior_profile.get("Role / What They Do") if prior_profile else "",
        "Recurring Tasks": prior_profile.get("Recurring Tasks") if prior_profile else "",
        "Known Strengths": prior_profile.get("Known Strengths") if prior_profile else "",
        "Known Blockers / Skill Gaps": prior_profile.get("Known Blockers / Skill Gaps") if prior_profile else "",
        "Tools They Currently Use": prior_profile.get("Tools They Currently Use") if prior_profile else "",
        "Automation Opportunities (Open)": prior_profile.get("Automation Opportunities (Open)") if prior_profile else "",
        "Automation Opportunities (Shipped)": prior_profile.get("Automation Opportunities (Shipped)") if prior_profile else "",
        "Productivity Patterns": prior_profile.get("Productivity Patterns") if prior_profile else "",
        "Coaching Notes for Manager": prior_profile.get("Coaching Notes for Manager") if prior_profile else "",
        "Last Updated": today,
    }

    if not config.GOOGLE_API_KEY or not recent_summaries and not recent_activity:
        return fallback

    try:
        user_block = _build_weekly_user_block(name, prior_profile, recent_summaries, recent_activity)
        data = deep_brain.deep_json(_WEEKLY_SYSTEM_PROMPT, user_block, max_tokens=6000)
    except Exception as e:
        log.warning("Deep-brain weekly synthesis failed for %s: %s", name, e)
        return fallback

    def bullets(key: str) -> str:
        v = data.get(key)
        if isinstance(v, list):
            return " • ".join(str(x).strip() for x in v if str(x).strip())
        if isinstance(v, str):
            return v.strip()
        return ""

    return {
        "Worker": name,
        "Slack User ID": slack_user_id,
        "First Seen": fallback["First Seen"],
        "Days Tracked": days_tracked_prior + 7,
        "Role / What They Do": str(data.get("role_what_they_do", "")).strip(),
        "Recurring Tasks": bullets("recurring_tasks"),
        "Known Strengths": bullets("known_strengths"),
        "Known Blockers / Skill Gaps": bullets("known_blockers"),
        "Tools They Currently Use": bullets("tools_currently_used"),
        "Automation Opportunities (Open)": bullets("automation_opportunities_open"),
        "Automation Opportunities (Shipped)": bullets("automation_opportunities_shipped"),
        "Productivity Patterns": bullets("productivity_patterns"),
        "Coaching Notes for Manager": bullets("coaching_notes_for_manager"),
        "Last Updated": today,
    }
