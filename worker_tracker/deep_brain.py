"""Deep analytical brain — Claude Opus 4.6 with adaptive thinking + prompt caching.

Used for the heavy-reasoning calls only:
- Daily EOD analysis (per-worker day summary, automation ideas, capacity signal)
- Weekly profile synthesis (rebuilding the durable Worker Profile from 7 days of data)

Quick conversational stuff stays on Gemini Flash (cheap + fast).

Prompt-caching strategy:
- The analytical framework / rules / JSON schema goes in `system` (stable across all workers)
- The per-worker context goes in the `user` message
- We cache the system block so calls 2-7 in a batch read from cache (~10% the cost)

Fails open: if the Anthropic API errors or the key isn't set, falls back to Gemini Pro
so the rest of the pipeline keeps working.
"""
from __future__ import annotations

import json
import logging

from tenacity import retry, stop_after_attempt, wait_exponential

from . import config

log = logging.getLogger(__name__)


def _strip_codefence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=2, max=10))
def _anthropic_json(system_prompt: str, user_content: str, max_tokens: int = 8000) -> dict:
    """Claude Opus 4.6 call with adaptive thinking + cached system prompt.

    The system block is identical across all workers in a batch (7 daily calls in
    <5 min for EOD, ~7 in <5 min for weekly synth) — well within the 5-min
    ephemeral cache TTL. So calls 2-7 read the system prompt from cache.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
    )

    # Pull final text (skip thinking blocks — those are reasoning trace, not output)
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    if not text:
        raise RuntimeError("Claude returned no text content")

    # Log cache + token usage so we know caching is working
    cache_create = getattr(msg.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
    log.info(
        "Claude deep call: in=%d cache_create=%d cache_read=%d out=%d (cache hit: %s)",
        msg.usage.input_tokens, cache_create, cache_read, msg.usage.output_tokens,
        "yes" if cache_read > 0 else "no — first of batch",
    )

    return json.loads(_strip_codefence(text), strict=False)


def deep_json(system_prompt: str, user_content: str, max_tokens: int = 8000) -> dict:
    """Route a heavy analytical call through the best available model.

    Prefers Claude Opus 4.6 (extended thinking + prompt caching).
    Falls back to Gemini Pro if ANTHROPIC_API_KEY isn't set, so the pipeline
    keeps working while you provision the Anthropic key.
    """
    if config.ANTHROPIC_API_KEY:
        return _anthropic_json(system_prompt, user_content, max_tokens)

    # Fallback path: same Gemini Flash everything else uses — zero incremental cost.
    # If you want the smart brain later, set ANTHROPIC_API_KEY in Railway and you're in.
    log.info("ANTHROPIC_API_KEY not set — falling back to Gemini Flash for this deep call")
    from google import genai
    from google.genai import types

    combined = system_prompt + "\n\n" + user_content
    client = genai.Client(api_key=config.GOOGLE_API_KEY)
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,  # gemini-2.5-flash by default
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=combined)])],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
            max_output_tokens=max_tokens,
        ),
    )
    return json.loads(_strip_codefence(resp.text or "{}"), strict=False)
