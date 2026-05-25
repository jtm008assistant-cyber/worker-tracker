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

from . import config

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


def _build_daily_prompt(name: str, login_local: str, eod_local: str, active_hours: float,
                        help_count: int, missed: int,
                        checkins: Iterable[tuple[datetime, str]],
                        profile: dict | None,
                        knowledge: list[dict] | None = None) -> str:
    lines = [
        f"[{t.strftime('%H:%M')}] {m.strip() or '(empty reply)'}"
        for t, m in checkins
    ]
    checkin_block = "\n".join(lines) if lines else "(no check-in replies recorded)"
    profile_block = _profile_context_block(profile)
    knowledge_block = _knowledge_block(knowledge or [])
    return f"""You are this worker's AI ops/HR analyst. You audit their day to help the
manager spot two things:
1. Tasks that could be AUTOMATED (specific scripts, no-code tools, AI prompts, integrations).
2. MANUAL or repetitive grunt-work that's eating their time.

You have history. Use it. If the prior profile already flagged an
automation opportunity and the worker is doing the same manual task
AGAIN today, call that out explicitly ("repeat: X was flagged on
{{date}} and they're still doing it manually").

{profile_block}

{knowledge_block}

TODAY:
Worker: {name}
Login (local): {login_local}    EOD: {eod_local}    Active: {active_hours}h
Help requests today: {help_count}    Missed prompts: {missed}

Check-in replies (chronological):
{checkin_block}

Respond as JSON ONLY:
{{
  "day_summary": "2-3 sentences: what they actually did today, referencing patterns from prior profile if relevant",
  "automation_opportunities": ["concrete bullet ≤25 words; if repeating a prior flag, prefix with 'REPEAT: '", ...],
  "manual_red_flags": ["concrete bullet ≤25 words pointing at manual/repetitive work", ...],
  "capacity_signal": "one of: spare capacity | balanced | stretched | stuck"
}}

Rules:
- Max 5 items per list. Empty list if nothing genuine.
- Each bullet must reference something they actually did/said today.
- Output ONLY the JSON. No prose before or after.
"""


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
        data = _gemini_json(_build_daily_prompt(
            name, login_local, eod_local, active_hours, help_count, missed, checkins, profile, knowledge,
        ))
    except Exception as e:
        log.warning("Gemini daily analysis failed for %s: %s", name, e)
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


def _build_weekly_prompt(name: str, prior_profile: dict | None,
                         recent_summaries: list[dict],
                         recent_activity: list[dict]) -> str:
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

    return f"""You are this worker's AI ops/HR analyst. You are doing the WEEKLY profile
update. Your job: turn the past week of activity into a durable picture of
who this worker is, what they actually do, what they're good at, what
slows them down, and what work could be automated.

You will read:
1. The prior profile (last week's snapshot — may be empty if this is week 1)
2. This week's daily summaries
3. This week's raw check-in messages

Then output a REPLACEMENT profile. Carry forward anything from the prior
profile that still seems true, REVISE anything contradicted by this week,
and ADD anything new. Be specific and concrete — no consultant-speak.

Critical: for "Automation Opportunities (Open)", carry forward unshipped
items from the prior profile UNLESS the worker clearly addressed them
this week. If they're STILL doing a task you flagged 2+ weeks ago, that's
worth noting explicitly. For "Automation Opportunities (Shipped)", add
any that were addressed.

WORKER: {name}

{prior_block}

DAILY SUMMARIES THIS WEEK:
{summary_block}

RAW CHECK-INS THIS WEEK:
{raw_block}

Respond as JSON ONLY, matching this schema exactly:
{{
  "role_what_they_do": "1-2 sentences plain English: what their actual day-to-day work is",
  "recurring_tasks": ["bullet ≤20 words", ...],
  "known_strengths": ["bullet ≤20 words", ...],
  "known_blockers": ["bullet ≤20 words", ...],
  "tools_currently_used": ["tool name, brief context", ...],
  "automation_opportunities_open": ["concrete proposal ≤25 words; if carried over, prefix '[N weeks open] '", ...],
  "automation_opportunities_shipped": ["what got automated and roughly when", ...],
  "productivity_patterns": ["bullet ≤25 words: time-of-day, day-of-week, energy patterns, etc.", ...],
  "coaching_notes_for_manager": ["bullet ≤30 words: what manager should know, ask about, or coach on", ...]
}}

Rules:
- Lists max 8 items each. Empty list ok.
- Pull evidence from THIS WEEK; do not invent.
- Output ONLY the JSON.
"""


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


def conversational_reply(message: str, speaker_name: str, is_owner: bool,
                          is_manager: bool) -> str | None:
    """Generate a natural conversational reply for messages that didn't match any
    specific command pattern. Used so admins can chat with Sam without getting
    boilerplate responses. Returns None to stay silent.
    """
    if not config.GOOGLE_API_KEY or not message.strip():
        return None

    role = "an OWNER (Jan or Ideen) — full admin access" if is_owner else (
        "a MANAGER (Hannah) — can query workers but not owners" if is_manager
        else "a worker being time-tracked")

    prompt = f"""You are Sam, an AI ops assistant for a small team at Hey Girl Tea.
Your main job is time tracking via Slack DMs: workers DM you to clock in,
take breaks, EOD, and you check in every ~2h asking what they're working on.

The person messaging you is {speaker_name}, {role}.

Their message just now:
"{message.strip()}"

This message didn't match any of your specific patterns (not a check-in,
not EOD, not a break, not an admin command, not a hours-query, not a
discrepancy report). It's probably conversational.

Respond like a warm, helpful teammate — lowercase, casual, 1-3 sentences max.
NEVER be corporate or robotic. If they ask what you can do, mention the
specific command. If it's small talk, chat back lightly. If they ask
something outside your skills (jokes, life advice, etc.), play along briefly
but don't pretend to be more than you are.

What you can do (only mention if relevant):
- Time tracking + payroll handoff to Ideen on 1st + 16th
- Admins: ask "what is X doing" / "status of Hannah" → live snapshot
- Owners: ask "introduce everyone" → broadcast onboarding intros
- Anyone: ask "hours" to see current pay-period hours
- Flag discrepancies if hours look wrong
- Daily AI EOD digest + weekly profile synthesis (sent to Jan)

Respond with ONLY the reply text — no quotes, no preamble, no JSON. If no
reply makes sense (e.g., they sent a single emoji or 'ok'), respond with
just: SKIP
"""
    try:
        client = genai.Client(api_key=config.GOOGLE_API_KEY)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.7, max_output_tokens=800),
        )
        reply = (resp.text or "").strip()
        if not reply or reply.upper() == "SKIP":
            return None
        # Strip surrounding quotes if Gemini added them
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
        data = _gemini_json(
            _build_weekly_prompt(name, prior_profile, recent_summaries, recent_activity),
            max_tokens=4096,
        )
    except Exception as e:
        log.warning("Weekly synthesis failed for %s: %s", name, e)
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
