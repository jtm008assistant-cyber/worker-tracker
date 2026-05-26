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
}

URL_RE = __import__("re").compile(r"https?://[^\s<>\"']+")


def _strip_codefence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=2, max=10))
def _gemini_json(prompt: str, max_tokens: int = 2048) -> dict:
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
    return json.loads(_strip_codefence(resp.text or "{}"), strict=False)


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
job is to audit each worker's day to help the manager spot two things:

1. Tasks that could be AUTOMATED (specific scripts, no-code tools, AI
   prompts, integrations).
2. MANUAL or repetitive grunt-work that's eating their time.

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
  "capacity_signal": "one of: spare capacity | balanced | stretched | stuck"
}

Rules:
- Max 5 items per list. Empty list if nothing genuine — do NOT pad with vague suggestions.
- Each bullet must reference something the worker actually did or said today.
- Be specific: "write a Python script to import LinkedIn export into HubSpot via API" beats "automate data entry".
- Output ONLY the JSON. No prose before or after. No code fences."""


def _build_daily_user_block(name: str, login_local: str, eod_local: str, active_hours: float,
                            help_count: int, missed: int,
                            checkins: Iterable[tuple[datetime, str]],
                            profile: dict | None,
                            knowledge: list[dict] | None = None) -> str:
    """The variable per-worker context — comes AFTER the cached system prompt."""
    lines = [
        f"[{t.strftime('%H:%M')}] {m.strip() or '(empty reply)'}"
        for t, m in checkins
    ]
    checkin_block = "\n".join(lines) if lines else "(no check-in replies recorded)"
    profile_block = _profile_context_block(profile)
    knowledge_block = _knowledge_block(knowledge or [])
    return f"""{profile_block}

{knowledge_block}

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
            knowledge: list[dict] | None = None) -> dict:
    """Daily analysis. Never raises. profile is the persistent Worker Profile row."""
    if not config.GOOGLE_API_KEY:
        log.info("No GOOGLE_API_KEY; skipping Gemini analysis for %s", name)
        return dict(EMPTY_DAILY)
    if not checkins:
        return dict(EMPTY_DAILY)
    try:
        user_block = _build_daily_user_block(
            name, login_local, eod_local, active_hours, help_count, missed, checkins, profile, knowledge,
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

    prompt = f"""You are Sam — an AI assistant on a small team. After a worker sends
a check-in message, you decide whether to ask ONE quick follow-up question
to learn about an unfamiliar tool, sheet, doc, process, or workflow they
just mentioned.

ASK IF: the worker mentioned a tool/sheet/doc/process by name (or with an
ambiguous reference like "the tracker", "the system", "that doc") that is
NOT already in their known list. Knowing it would help future check-ins
make sense.

DO NOT ASK IF:
- Already in the known list
- Already asked about it today (see "already asked today" below)
- Casual / personal (food, mood, family, weather)
- Just normal task description with no specific tool/process name
- You'd be asking for the sake of asking

The follow-up must be short (<= 25 words), lowercase, friendly, written in
Sam's voice. Example: "quick one — what's the tracker sheet? drop a link
if there is one, I'll remember for next time".

Worker: {name}
Their check-in: "{message.strip()}"

{knowledge_block}

Already asked about today: {already_str}

Respond as JSON ONLY:
{{
  "ask": "the follow-up message text, or null if nothing worth asking",
  "topic": "1-3 word label of what the question is about (e.g. 'tracker sheet'), or null"
}}
"""
    try:
        data = _gemini_json(prompt, max_tokens=512)
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

    prompt = f"""Extract any tools, sheets, docs, processes, workflows, or projects
the worker referenced in their message. Output structured data.

Worker: {name}
Their message: "{reply_text.strip()}"
URLs detected in the message: {urls_block}
Question that was just asked of them (if any): {asked_topic or "(none — they spoke unprompted)"}
Already-known item names for this worker: {existing_str}

Respond as JSON ONLY:
{{
  "items": [
    {{
      "kind": "tool|sheet|doc|process|workflow|project",
      "name": "concise human-readable name",
      "url": "URL (use one from the detected URLs if relevant) or empty",
      "description": "1-2 sentences in plain English",
      "steps": "optional — bullet list for processes, otherwise empty"
    }}
  ]
}}

Rules:
- Only output things genuinely referenced in their message. Don't invent.
- Max 3 items per call.
- For URLs that look like Google Sheets, set kind=sheet; Docs → doc;
  Notion/Linear/Asana → tool.
- If they didn't actually answer the question (e.g. just said "ok"),
  return {{"items": []}}.
- Output ONLY the JSON.
"""
    try:
        data = _gemini_json(prompt, max_tokens=1024)
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


def conversational_reply(message: str, speaker_name: str, is_owner: bool,
                          is_manager: bool, is_worker: bool = False,
                          recent_context: str = "") -> str | None:
    """Generate a useful conversational reply for messages that didn't match a
    specific command pattern. Returns None to stay silent.
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

    prompt = f"""You are Sam, the AI ops assistant for Hey Girl Tea. Your main
job is time tracking: workers DM you to clock in, take breaks, and end their
day. Every pay period your hour log goes to Ideen for payroll.

WHO IS MESSAGING:
{role_block}

THEIR MESSAGE:
"{message.strip()}"
{ctx_block}
YOUR CAPABILITIES:
{capabilities}

YOUR VOICE:
- Warm but tight. Lowercase. Like a thoughtful coworker, not a chatbot.
- 1-3 sentences max. NEVER lists/bullets in chat replies.
- Don't say "I can help with that!" or other filler — just answer.
- Concrete > generic. If they ask "what should I do" — name the actual thing.
- Match their energy: short reply for short message, longer if they ask a real question.

RULES:
- If their message is genuinely about your capabilities or how to use you, answer the actual question. Don't be vague.
- If they ask about something outside your tools (jokes, life advice, weather), play along briefly but redirect to what you can do.
- If they're complaining/frustrated, acknowledge it directly. Don't be saccharine.
- If they ask a question YOU can answer with data (e.g. "what's my latest check-in"), say so but flag that you can't actually pull that until they teach you how (i.e. don't hallucinate data).
- Output the REPLY ONLY. No quotes, no preamble, no JSON.
- If nothing useful to say (e.g. just an emoji or 'ok'), output: SKIP
"""
    try:
        client = genai.Client(api_key=config.GOOGLE_API_KEY)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.6, max_output_tokens=600),
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
