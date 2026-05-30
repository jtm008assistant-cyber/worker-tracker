"""The unified message router. Replaces the legacy admin dispatcher.

When a message comes in (and the worker fast-path didn't catch it as a
login/break/eod/hours/discrepancy), the router runs through three tiers:

  Tier 1: commands.try_deterministic — instant, no LLM
  Tier 2: agent_v2.agent_reply — Sonnet 4.5 tool-calling
  Tier 3: fallback.fallback_reply — keyword guess + sheet lookup

Whichever produces a reply first is sent to the user. Tier 3 ALWAYS
returns something useful, so the user can never see "ClientError" or
silence again.
"""
from __future__ import annotations

import logging
from typing import Callable

from . import commands, agent_v2, fallback

log = logging.getLogger(__name__)


def route(
    text: str,
    speaker_user_id: str,
    speaker_name: str,
    is_owner: bool,
    is_manager: bool,
    workers: list[dict],
) -> str:
    """Run the message through Tier 1 → 2 → 3 and return the reply.

    Always returns a non-empty string. NEVER raises.
    """
    if not text or not text.strip():
        return ""

    # ── Tier 1: deterministic commands ────────────────────────────────────
    try:
        reply = commands.try_deterministic(
            text=text,
            speaker_user_id=speaker_user_id,
            workers_list=workers,
            is_owner=is_owner,
            is_manager=is_manager,
        )
        if reply:
            log.info("router: Tier 1 handled")
            return reply
    except Exception:
        log.exception("Tier 1 commands crashed — falling to Tier 2")

    # ── Tier 2: Sonnet agent ──────────────────────────────────────────────
    try:
        reply = agent_v2.agent_reply(
            text=text,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            is_owner=is_owner,
            is_manager=is_manager,
            workers=workers,
        )
        if reply:
            log.info("router: Tier 2 (agent) handled")
            return reply
    except Exception:
        log.exception("Tier 2 agent crashed — falling to Tier 3")

    # ── Tier 3: deterministic fallback (always returns something) ─────────
    try:
        log.warning("router: dropping to Tier 3 fallback")
        return fallback.fallback_reply(
            text=text,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            is_owner=is_owner,
            is_manager=is_manager,
            workers=workers,
        )
    except Exception:
        log.exception("Tier 3 fallback crashed")
        # Absolute last resort
        return (f"hey {speaker_name} — something's gone sideways on my end. "
                f"try again in a sec? if it keeps happening, drop Jan a message.")
