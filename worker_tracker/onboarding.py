"""One-shot onboarding: DM every active worker a Sam introduction.

Idempotent — checks Activity Log for an 'introduced' event per worker and
skips anyone who's already been introduced. Logs an 'introduced' event
after sending so re-runs are safe.

Usage:
    python -m worker_tracker introduce            # intro everyone who hasn't been
    python -m worker_tracker introduce --force    # re-intro everyone (use sparingly)
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Iterable

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import config, sheets, worker_views

log = logging.getLogger(__name__)


def _already_introduced(slack_user_id: str) -> bool:
    """True if Activity Log has an 'introduced' event for this user."""
    try:
        rows = sheets.activity_rows()
    except Exception:
        log.exception("Could not read Activity Log")
        return False
    for r in rows:
        if (str(r.get("Slack User ID", "")).strip() == slack_user_id
                and str(r.get("Type", "")).strip() == "introduced"):
            return True
    return False


def _intro_message(worker: dict, view_url: str | None) -> str:
    first = worker["name"].split()[0] if worker["name"] else "there"
    cadence_min = worker.get("checkin_interval_min") or config.CHECKIN_INTERVAL_MINUTES
    h = cadence_min // 60
    m = cadence_min % 60
    cadence = f"{h}h" if m == 0 else f"{h}h{m}m"

    view_block = ""
    if view_url:
        view_block = (
            f"\n\nI also made you a personal sheet where you can see your own "
            f"hours and pay history anytime:\n{view_url}\n"
            f"first time you open it, click 'Allow access' if it asks. "
            f"only you and the team admins can see this sheet."
        )

    return (
        f"hey {first} 👋 I'm Sam — I help the team stay in sync on what everyone's working on.\n\n"
        f"quick rundown so you know what to expect:\n\n"
        f"📩 *message me when you start your shift* — anything works, just say \"hi\" or \"starting\"\n"
        f"🔄 *every {cadence}* I'll check in to ask what you got done. reply with whatever you actually "
        f"did — doesn't have to be polished\n"
        f"☕ *taking a break?* just say \"break\" or \"lunch\" — I'll pause the clock until you message back\n"
        f"✅ *when you wrap up*, message \"done my shift\" or \"EOD\" — I'll log you out\n\n"
        f"things you can ask me anytime:\n"
        f"• \"hours\" — see your hours so far this pay period\n"
        f"• if any hours look wrong, just tell me what's off and I'll flag it for Jan to review before payroll runs\n"
        f"{view_block}\n\n"
        f"last thing — what's your usual schedule? roughly when do you start and wrap most days? "
        f"helps me set expectations.\n\n"
        f"we're good to go whenever you start your next shift 🙌"
    )


def send_introductions(only_worker_id: str | None = None, force: bool = False) -> dict:
    """DM each active worker an introduction. Skips already-introduced unless force=True.

    Returns {worker_name: status} where status is 'sent', 'skipped', or 'failed: <reason>'.
    """
    if not config.SLACK_BOT_TOKEN:
        sys.exit("SLACK_BOT_TOKEN not set in env — can't DM workers")
    client = WebClient(token=config.SLACK_BOT_TOKEN)

    roster = sheets.load_roster()
    if only_worker_id:
        roster = [w for w in roster if w["user_id"] == only_worker_id]

    results: dict[str, str] = {}
    for w in roster:
        name = w["name"]
        uid = w["user_id"]
        if not force and _already_introduced(uid):
            log.info("Skipping %s — already introduced", name)
            results[name] = "skipped (already introduced)"
            continue

        # Ensure they have a personal view sheet
        view_url = w.get("personal_view_url") or worker_views.ensure_view_for_worker(w) or ""

        try:
            client.chat_postMessage(channel=uid, text=_intro_message(w, view_url))
            sheets.append_event(name, uid, "introduced", "intro DM sent", w["tz"])
            log.info("Introduced %s", name)
            results[name] = "sent"
        except SlackApiError as e:
            err = e.response.get("error", str(e))
            log.exception("Failed to DM %s: %s", name, err)
            results[name] = f"failed: {err}"
        except Exception as e:
            log.exception("Unexpected error introducing %s", name)
            results[name] = f"failed: {e}"

    return results


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Re-introduce workers even if they've already been introduced")
    ap.add_argument("--user", help="Limit to one Slack User ID (testing)")
    args = ap.parse_args(argv)

    results = send_introductions(only_worker_id=args.user, force=args.force)
    print()
    print("=" * 60)
    print("Introduction results:")
    for name, status in results.items():
        marker = "✓" if status == "sent" else ("•" if "skipped" in status else "✗")
        print(f"  {marker} {name:30s} {status}")
    print("=" * 60)


if __name__ == "__main__":
    main()
