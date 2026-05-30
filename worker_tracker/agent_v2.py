"""Tier 2 — Sonnet 4.5 tool-calling agent.

Replaces the Gemini agent. Uses the Anthropic Messages API with native
tool-use. Reads tools from tools.py.

Key features:
  - Multi-attempt retry with exponential backoff on transient errors
  - Conversation memory PERSISTED to a Conversations sheet tab (survives deploys)
  - Per-request tool result memoization (no duplicate sheet reads in a loop)
  - Prompt caching on system prompt + tool definitions (~90% input discount)
  - Never raises to the caller — returns either a real reply or None
    (caller falls through to Tier 3 fallback)
"""
from __future__ import annotations

import json
import logging
import os
import time as time_mod
from datetime import datetime, timezone
from typing import Any

import anthropic
from anthropic.types import MessageParam, ToolParam, ToolUseBlock, TextBlock

from . import config, sheets, tools

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Conversation memory (persisted to a sheet tab — survives deploys)
# ─────────────────────────────────────────────────────────────────────────

CONVERSATIONS_TAB = "Conversations"
CONVERSATIONS_HEADER = ["Timestamp UTC", "Speaker User ID", "Role", "Text"]
HISTORY_LIMIT = 14  # how many recent turns to feed back into each call

# In-memory mirror so we don't sheet-read on every turn
_CONV_CACHE: dict[str, list[dict]] = {}


def _ensure_conversations_tab() -> None:
    """Create the Conversations tab if it doesn't exist. Idempotent."""
    try:
        ss = sheets.open_tracker()
        try:
            ss.worksheet(CONVERSATIONS_TAB)
        except Exception:
            ws = ss.add_worksheet(title=CONVERSATIONS_TAB, rows=1000, cols=4)
            ws.append_row(CONVERSATIONS_HEADER, value_input_option="USER_ENTERED")
            log.info("Created Conversations tab")
    except Exception:
        log.exception("Failed to ensure Conversations tab")


def remember_turn(speaker_id: str, role: str, text: str) -> None:
    """Append a turn to memory (both in-process cache and persisted sheet).
    Best-effort — if sheet write fails, cache still works."""
    cached = _CONV_CACHE.setdefault(speaker_id, [])
    cached.append({
        "role": role, "text": text[:2000],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if len(cached) > HISTORY_LIMIT:
        _CONV_CACHE[speaker_id] = cached[-HISTORY_LIMIT:]
    # Persist
    try:
        ss = sheets.open_tracker()
        try:
            ws = ss.worksheet(CONVERSATIONS_TAB)
        except Exception:
            _ensure_conversations_tab()
            ws = ss.worksheet(CONVERSATIONS_TAB)
        ws.append_row(
            [datetime.now(timezone.utc).isoformat(timespec="seconds"),
             speaker_id, role, text[:1900]],
            value_input_option="USER_ENTERED",
        )
    except Exception:
        log.exception("Failed to persist conversation turn (cache still works)")


def get_history(speaker_id: str) -> list[dict]:
    """Get the speaker's recent turns. Falls back to sheet if cache is empty."""
    cached = _CONV_CACHE.get(speaker_id)
    if cached:
        return list(cached)
    # Cache miss — try the sheet
    try:
        ss = sheets.open_tracker()
        try:
            ws = ss.worksheet(CONVERSATIONS_TAB)
        except Exception:
            return []
        rows = ws.get_all_records()
        speaker_rows = [r for r in rows if str(r.get("Speaker User ID", "")).strip() == speaker_id]
        speaker_rows = speaker_rows[-HISTORY_LIMIT:]
        out = []
        for r in speaker_rows:
            out.append({
                "role": r.get("Role", "user"),
                "text": str(r.get("Text", "")),
                "ts": r.get("Timestamp UTC", ""),
            })
        _CONV_CACHE[speaker_id] = out
        return list(out)
    except Exception:
        log.exception("Conversation history read failed")
        return []


# ─────────────────────────────────────────────────────────────────────────
# Tool definitions for Anthropic (different schema than Gemini)
# ─────────────────────────────────────────────────────────────────────────

def _build_anthropic_tools() -> list[ToolParam]:
    return [
        {
            "name": "get_worker_status",
            "description": (
                "Get a worker's CURRENT state (working, on_break, logged_off, "
                "or not_started) including login time, hours so far this shift, "
                "last check-in message, and break duration if on break. Use for "
                "'is X working', 'what's X up to', 'how is X doing', 'where's X'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Worker name or nickname; typos OK."},
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_worker_activity",
            "description": (
                "Get a worker's chronological events (check-ins, breaks, login, "
                "EOD) for a date or date range. Use for 'what did X do today', "
                "'what did X work on yesterday', 'show X's trail on May 28', "
                "'recap X's week'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "when": {"type": "string", "description":
                             "'today', 'yesterday', 'last_week', 'this_week', "
                             "'this_month', 'last_month', 'YYYY-MM-DD', or "
                             "'YYYY-MM-DD to YYYY-MM-DD'"},
                },
                "required": ["name", "when"],
            },
        },
        {
            "name": "get_worker_hours",
            "description": (
                "Pay-period hours for a worker including today's open session. "
                "Use for 'how many hours has X worked', 'X's hours this period'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "period": {"type": "string", "description":
                               "'current' for the open period, 'previous' for prior"},
                },
                "required": ["name", "period"],
            },
        },
        {
            "name": "get_worker_benefits",
            "description": (
                "Vacation/sick/holiday/PTO allocations + used + remaining for a "
                "worker, plus HMO reimbursement, calamity fund, performance bonus "
                "date, 13th month eligibility, pay schedule, hourly rate. Use for "
                "'how many vacation days does X have', 'X's benefits', 'when is "
                "X's perf bonus', 'how much PTO does X have left'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_worker_open_tasks",
            "description": (
                "Pending relays + delivered-but-not-done relays + open "
                "self-commitments for a worker. Use for 'X's open tasks', "
                "'what's still on X's plate', 'X's checklist'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "get_worker_knowledge",
            "description": (
                "Tools, sheets, processes, people, jobs Sam has logged about a "
                "worker (their Knowledge Base entries). Use for 'what tools does "
                "X use', 'who does X coordinate with', 'X's workflow map'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "get_team_status",
            "description": (
                "Current state snapshot for EVERY active non-owner worker. Use "
                "for 'team status', 'did everyone log in', 'who's working', "
                "'who's on break', 'who hasn't logged in'."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_all_benefits",
            "description": (
                "Compact benefits table for EVERY active worker in ONE call. "
                "Use for cross-team comparisons: 'who has the most vacation', "
                "'who's eligible for 13th month', 'compare PTO across team', "
                "'who has the highest pay rate'."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_learned_today",
            "description": (
                "What Sam captured today across the team — new KB entries + "
                "substantive check-ins per worker. Use for 'what did you learn "
                "today', 'what's new', 'today's takeaways from the team'."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_roster_summary",
            "description": (
                "Full active roster with names, nicknames, TZs, roles. Useful "
                "when you need to know who's on the team or check who's an owner."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "log_retroactive_eod",
            "description": (
                "Worker says they already logged off earlier but didn't tell "
                "Sam. Use for 'I already logout earlier at 7:44am', 'I ended "
                "at 6pm', 'I EOD'd at 5'. Writes a backdated EOD."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Usually the speaker themselves"},
                    "time_hhmm": {"type": "string", "description":
                                  "'7:44am' / '18:30' / '6pm'"},
                    "date_iso": {"type": "string", "description":
                                 "Optional YYYY-MM-DD if not today"},
                },
                "required": ["name", "time_hhmm"],
            },
        },
        {
            "name": "log_retroactive_login",
            "description": (
                "Worker says they started earlier than they messaged Sam. Use "
                "for 'I started my shift at 8am', 'I came on at 22:00 yesterday'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "time_hhmm": {"type": "string"},
                    "date_iso": {"type": "string"},
                },
                "required": ["name", "time_hhmm"],
            },
        },
        {
            "name": "log_retroactive_break",
            "description": (
                "Worker tells Sam they took a break that wasn't logged. Use for "
                "'I took a break from 1-2pm' / 'I was on lunch 12 to 1'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "start_hhmm": {"type": "string"},
                    "end_hhmm": {"type": "string"},
                    "date_iso": {"type": "string"},
                },
                "required": ["name", "start_hhmm", "end_hhmm"],
            },
        },
        {
            "name": "stop_checkin_prompts",
            "description": (
                "Stop periodic check-in prompts for a worker by writing an EOD "
                "at now. Use when worker says they're done but didn't give a "
                "specific time."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "log_time_off",
            "description": (
                "Log time off for a worker. Use when an admin says 'log vacation "
                "for hannah dec 1-5' / 'sick day for rey today'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "description":
                             "vacation | sick | holiday | pto | personal | unpaid"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "days": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "type", "start_date", "end_date", "days"],
            },
        },
        {
            "name": "queue_message_for_worker",
            "description": (
                "Queue or deliver a message to a worker. If deferred=true, hold "
                "until next login. If false, deliver immediately (or queue if "
                "offline). Use for 'tell X to do Y', 'send this to X', 'when X "
                "logs in tell her Y', 'give X this link'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "to_name": {"type": "string"},
                    "message": {"type": "string", "description":
                                "The actual message text to deliver, written naturally"},
                    "deferred": {"type": "boolean", "description":
                                 "True if 'when X logs in'; False for immediate"},
                    "estimated_time": {"type": "string"},
                },
                "required": ["to_name", "message", "deferred"],
            },
        },
        {
            "name": "save_knowledge",
            "description": (
                "Save a Knowledge Base entry for a worker. Use when a worker "
                "shares a sheet URL with description, mentions a tool/process/"
                "person worth logging, or wants to explicitly record something."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "worker_name": {"type": "string"},
                    "kind": {"type": "string", "description":
                             "software | tool | sheet | doc | process | workflow | "
                             "platform | person | link | job | compliance"},
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": "string", "description":
                                    "1-2 sentences: what it is + what worker uses it for"},
                    "steps": {"type": "string"},
                },
                "required": ["worker_name", "kind", "name", "description"],
            },
        },
        {
            "name": "send_eod_digest_now",
            "description": (
                "Trigger the EOD digest immediately. Use when admin says 'send "
                "digest', 'EOD report now', 'give me today's report'."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


# Tool dispatch: name -> (callable, needs_admin_check_in_args)
def _dispatch(tool_name: str, args: dict, ctx: dict) -> dict:
    """Execute a tool by name with permission gating."""
    workers = ctx["workers"]
    is_admin = ctx["is_speaker_admin"]
    speaker_name = ctx["speaker_name"]
    speaker_id = ctx["speaker_user_id"]

    if tool_name == "get_worker_status":
        return tools.get_worker_status(args["name"], workers, is_speaker_admin=is_admin)
    if tool_name == "get_worker_activity":
        return tools.get_worker_activity(args["name"], args.get("when", "today"),
                                          workers, is_speaker_admin=is_admin)
    if tool_name == "get_worker_hours":
        return tools.get_worker_hours(args["name"], args.get("period", "current"),
                                       workers, is_speaker_admin=is_admin)
    if tool_name == "get_worker_benefits":
        return tools.get_worker_benefits(args["name"], workers, is_speaker_admin=is_admin)
    if tool_name == "get_worker_open_tasks":
        return tools.get_worker_open_tasks(args["name"], workers, is_speaker_admin=is_admin)
    if tool_name == "get_worker_knowledge":
        return tools.get_worker_knowledge(args["name"], workers, is_speaker_admin=is_admin)
    if tool_name == "get_team_status":
        return tools.get_team_status(workers)
    if tool_name == "get_all_benefits":
        return tools.get_all_benefits(workers)
    if tool_name == "get_learned_today":
        return tools.get_learned_today(workers)
    if tool_name == "get_roster_summary":
        return tools.get_roster_summary(workers)
    if tool_name == "log_retroactive_eod":
        return tools.log_retroactive_eod(args["name"], args["time_hhmm"],
                                          args.get("date_iso"), workers)
    if tool_name == "log_retroactive_login":
        return tools.log_retroactive_login(args["name"], args["time_hhmm"],
                                            args.get("date_iso"), workers)
    if tool_name == "log_retroactive_break":
        return tools.log_retroactive_break(args["name"], args["start_hhmm"],
                                            args["end_hhmm"], args.get("date_iso"), workers)
    if tool_name == "stop_checkin_prompts":
        return tools.stop_checkin_prompts(args["name"], args.get("reason", ""), workers)
    if tool_name == "log_time_off":
        if not is_admin:
            return {"error": "Only admins can log time off for workers."}
        return tools.log_time_off(args["name"], args.get("type", "vacation"),
                                   args["start_date"], args["end_date"],
                                   args.get("days", 1), args.get("notes", ""),
                                   speaker_name, workers)
    if tool_name == "queue_message_for_worker":
        if not is_admin:
            return {"error": "Only admins can send messages to other workers."}
        return tools.queue_message_for_worker(
            args["to_name"], args["message"], args.get("deferred", False),
            args.get("estimated_time", ""), speaker_name, speaker_id, workers)
    if tool_name == "save_knowledge":
        return tools.save_knowledge(args["worker_name"], args.get("kind", "tool"),
                                     args["name"], args.get("url", ""),
                                     args["description"], args.get("steps", ""),
                                     workers)
    if tool_name == "send_eod_digest_now":
        if not is_admin:
            return {"error": "Only admins can trigger the EOD digest."}
        return tools.send_eod_digest_now()

    return {"error": f"Unknown tool: {tool_name}"}


# ─────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are Sam, the AI ops assistant for Hey Girl Tea — a small remote team
with workers in the Philippines, owner Jan in Vancouver, and admin Hannah.
You're a thoughtful coworker: warm but tight, lowercase, 1-3 sentences
unless answering a question that needs length.

YOUR TOOLS GIVE YOU ALL THE TEAM DATA. Use them aggressively. Almost every
question has a tool answer — look it up before saying "i don't have access".

CONVERSATION MEMORY: prior turns are in the conversation history. When the
speaker uses pronouns ("he", "her", "they", "the same guy", "yesterday's
question"), resolve them from context. Don't ask the speaker to repeat.

MULTI-TOOL: if a question needs multiple lookups (e.g. "how is Rey doing
and is he still stuck on the Walmart case?"), call multiple tools, then
synthesize. If a question is a cross-team comparison ("who has the most
vacation"), call get_all_benefits ONCE, not get_worker_benefits N times.

DATE QUERIES: any question about a specific date or range uses
get_worker_activity with the right `when`, NOT get_worker_open_tasks
(which only shows the current queue).

WORKER-FACING RETROACTIVE LOGGING: if the speaker is a WORKER and they
tell you about an event they forgot to log:
  "i already logout earlier at 7:44am"  → log_retroactive_eod(<speaker>, "7:44am")
  "i started my shift at 22:00 yesterday" → log_retroactive_login(<speaker>, "22:00", date_iso=yesterday)
  "i took a break from 1-2pm" → log_retroactive_break(<speaker>, "1pm", "2pm")
  "i'm done for the day" (no specific time) → stop_checkin_prompts(<speaker>, reason)

CONFIRM warmly after logging: "got it, logged you out at 7:44am 🙌"

WORKERS CAN ASK ABOUT OTHER WORKERS, just not about owners (Jan, Ideen).
The tool layer enforces this. If a tool returns an "Owner-level data is
restricted" error, just relay it politely.

WORKERS asking about their OWN data should be answered by calling the
matching tool with the speaker's name.

KNOWLEDGE CAPTURE: if a worker mentions a tool/sheet/process/person/URL
that's clearly new (not already in their KB), call save_knowledge to log
it. Use the speaker's name unless they're clearly referring to a peer.

ENGAGE LIKE A COWORKER, not a chatbot:
  - Reply to the SUBSTANCE of what they said
  - Never use "thanks for the update! 🙌" on substantive content
  - For genuinely contentless messages ("ok", emoji), it's fine to stay silent
    (return an empty reply) — but for anything substantive, respond warmly
  - Acknowledge frustration directly, never saccharine

NEVER invent data. If a tool returns an error or empty result, say so
honestly: "rey has no check-ins on may 28" beats hallucinating.

OUTPUT: just the reply text — no quotes, no preamble, no JSON, no
"as an AI assistant".
"""


# ─────────────────────────────────────────────────────────────────────────
# Agent loop
# ─────────────────────────────────────────────────────────────────────────

_MODEL = os.environ.get("AGENT_MODEL_OVERRIDE") or "claude-sonnet-4-5"
_MAX_ITER = 10
_MAX_RETRIES = 5

_client_singleton: anthropic.Anthropic | None = None

def _get_client() -> anthropic.Anthropic | None:
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.error("ANTHROPIC_API_KEY not set — agent_v2 cannot run")
        return None
    try:
        _client_singleton = anthropic.Anthropic(api_key=key, max_retries=0)
        return _client_singleton
    except Exception:
        log.exception("Failed to init Anthropic client")
        return None


def _build_messages_from_history(history: list[dict], current_text: str) -> list[MessageParam]:
    """Convert our (role, text) tuples into Anthropic Messages format."""
    msgs: list[MessageParam] = []
    for turn in history:
        role = "assistant" if turn["role"] == "assistant" else "user"
        text = turn["text"]
        if not text.strip():
            continue
        msgs.append({"role": role, "content": [{"type": "text", "text": text}]})
    msgs.append({"role": "user", "content": [{"type": "text", "text": current_text}]})
    return msgs


def agent_reply(
    text: str,
    speaker_user_id: str,
    speaker_name: str,
    is_owner: bool,
    is_manager: bool,
    workers: list[dict],
) -> str | None:
    """Run the Sonnet agent loop. Returns the final reply text or None on
    hard failure (caller should fall through to Tier 3 fallback).

    NEVER raises — all exceptions caught and converted to None return."""
    client = _get_client()
    if client is None or not text.strip():
        return None

    history = get_history(speaker_user_id)
    role_label = "OWNER" if is_owner else ("MANAGER" if is_manager else "WORKER")
    system_with_role = (
        _SYSTEM_PROMPT
        + f"\n\nSPEAKER: {speaker_name} (role={role_label}). "
        + f"User ID: {speaker_user_id}.\n"
    )

    ctx = {
        "workers": workers,
        "speaker_user_id": speaker_user_id,
        "speaker_name": speaker_name,
        "is_speaker_admin": is_owner or is_manager,
    }
    anthropic_tools = _build_anthropic_tools()
    messages = _build_messages_from_history(history, text)

    for iteration in range(_MAX_ITER):
        # Retry with backoff on transient errors
        last_exc: Exception | None = None
        resp = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = client.messages.create(
                    model=_MODEL,
                    max_tokens=4096,
                    system=[{"type": "text", "text": system_with_role,
                              "cache_control": {"type": "ephemeral"}}],
                    tools=anthropic_tools,  # type: ignore[arg-type]
                    messages=messages,
                )
                break
            except (anthropic.APIConnectionError,
                    anthropic.RateLimitError,
                    anthropic.APIStatusError) as e:
                last_exc = e
                wait = (2 ** attempt) * 1.0  # 1s, 2s, 4s, 8s, 16s
                log.warning("agent: transient error attempt %d (%s) — sleeping %.0fs",
                            attempt + 1, type(e).__name__, wait)
                time_mod.sleep(wait)
            except Exception as e:
                last_exc = e
                log.exception("agent: unexpected exception")
                break

        if resp is None:
            log.warning("agent: all retries exhausted (%s)", type(last_exc).__name__ if last_exc else "?")
            return None  # Caller falls through to Tier 3

        # Process response content blocks
        text_blocks: list[str] = []
        tool_calls: list[tuple[str, str, dict]] = []  # (id, name, input)
        for block in resp.content:
            if isinstance(block, TextBlock):
                if block.text:
                    text_blocks.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append((block.id, block.name, dict(block.input) if block.input else {}))

        if not tool_calls:
            # Done — return the final text reply
            reply = "\n".join(text_blocks).strip()
            if not reply:
                return None
            # Strip wrapping quotes if Sonnet added any
            if (reply.startswith('"') and reply.endswith('"')) or \
               (reply.startswith("'") and reply.endswith("'")):
                reply = reply[1:-1]
            # Persist this turn
            try:
                remember_turn(speaker_user_id, "user", text)
                remember_turn(speaker_user_id, "assistant", reply)
            except Exception:
                log.exception("remember_turn failed (reply still returned)")
            return reply

        # Tool calls — append assistant message + tool results, then loop
        messages.append({
            "role": "assistant",
            "content": [block.model_dump() for block in resp.content],  # type: ignore[arg-type]
        })
        tool_results = []
        for call_id, tool_name, tool_args in tool_calls:
            log.info("agent tool: %s(%s)", tool_name, json.dumps(tool_args)[:200])
            try:
                result = _dispatch(tool_name, tool_args, ctx)
            except Exception as e:
                log.exception("tool %s threw", tool_name)
                result = {"error": f"{type(e).__name__}: {e}"}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": json.dumps(result),
            })
        messages.append({"role": "user", "content": tool_results})  # type: ignore[arg-type]
        # Loop continues — model will see the tool results and produce next turn

    log.warning("agent: max iterations exhausted for %s", speaker_user_id)
    return None  # Caller falls through to Tier 3
