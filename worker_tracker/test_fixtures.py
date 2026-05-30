"""Regression test fixtures — every Slack message that broke Sam this week.

Run before every deploy:
  python -m worker_tracker.test_fixtures

Each fixture: (message, speaker_role, must_contain_any | must_not_contain_any).
A fixture passes if the router returns a reply that satisfies the constraints.

These are LIVE tests — they make real Sonnet calls and real sheet reads.
That's intentional: we want to catch regressions that integration tests
would miss.
"""
from __future__ import annotations

import sys
from dotenv import load_dotenv

load_dotenv("C:/Ace/.env", override=True)

from worker_tracker import router, sheets

# Speaker IDs (real, so permission gates resolve correctly)
JAN_ID = "UCXSXMU21"
GER_ID = "U045QKFK857"
HANNAH_ID = "U030Q7R9FNC"
REY_ID = "U06H8LP7GBE"


# (label, speaker_id, speaker_name, is_owner, is_manager, message,
#  must_contain_any, must_not_contain_any)
FIXTURES = [
    # ── Bug: "How many vacation days does Rey have?" returned UnboundLocalError ──
    ("vacation_days_rey", JAN_ID, "Jan", True, False,
     "How many vacation days does Rey have?",
     ["7", "pto", "rey"], ["UnboundLocalError", "ClientError", "error"]),

    # ── Bug: pronoun "his" wasn't resolved ──
    # (Note: needs prior context — see test runner below for multi-turn)

    # ── Bug: "send digest" returned "ClientError" ──
    ("send_digest", JAN_ID, "Jan", True, False,
     "send digest",
     ["digest", "✓", "workers"], ["ClientError", "having trouble"]),

    # ── Bug: "tell Norks. this message is a test ... waitin on" parsed
    # "a test from jan. if he waitin" as worker name ──
    ("tell_norks_long", JAN_ID, "Jan", True, False,
     "tell Norks. this message is a test from jan. if he waitin on seeddance.. he can fap or dance for 20 min. if this worked norks let jan know",
     ["norks", "norlan", "✓", "queued", "sent"],
     ["don't know anyone", "a test from jan", "if he waitin"]),

    # ── Bug: "Did everyone log in today?" matched "everyone" as worker ──
    ("team_status_did_everyone", JAN_ID, "Jan", True, False,
     "Did everyone log in today?",
     ["team", "status", "working", "logged off", "hours"],
     ["don't know anyone named 'everyone'"]),

    # ── Bug: "send this to ger https://..." parsed "this" as worker ──
    ("send_this_to_ger", JAN_ID, "Jan", True, False,
     "send this to ger https://docs.google.com/spreadsheets/d/abc/edit",
     ["ger", "gerrielyn", "queued", "sent", "✓"],
     ["don't know anyone", "this"]),

    # ── Bug: "what did you learn today from the team" hit canned fallback ──
    ("what_did_you_learn", JAN_ID, "Jan", True, False,
     "what did you learn today from the team",
     ["today", "team", "learn", "check-in"],
     ["could you try rephrasing"]),

    # ── Bug: "what did Hannah do today" hit canned fallback ──
    ("what_did_hannah_do", JAN_ID, "Jan", True, False,
     "what did Hannah do today",
     ["hannah"],
     ["could you try rephrasing", "thanks for the update"]),

    # ── Bug: "is jonyn worken" (typos) wasn't matched ──
    ("typo_jonyn_worken", JAN_ID, "Jan", True, False,
     "is jonyn worken",
     ["jonny"],
     ["don't know anyone named 'jonyn'", "ClientError"]),

    # ── Bug: Worker (Ger) said "Logout" and got canned "thanks for update" ──
    # (Note: Logout should be caught by worker fast-path BEFORE the router,
    # so this fixture tests router behavior only — Logout going to router
    # should still be handled, not error)

    # ── Bug: Worker said "I already logout earlier at 7:44am" → zero reply ──
    ("worker_retroactive_eod", GER_ID, "Gerrielyn", False, False,
     "I already logout earlier at 7:44am",
     ["got it", "logged", "7:44"],
     ["could you try rephrasing", "I don't"]),

    # ── Bug: Workers couldn't ask about other workers ──
    ("worker_asks_peer", GER_ID, "Gerrielyn", False, False,
     "is rey working?",
     ["rey"],
     ["restricted", "can't"]),

    # ── Permission: Worker asking about owner should be blocked ──
    ("worker_asks_owner_blocked", GER_ID, "Gerrielyn", False, False,
     "is jan working?",
     ["owner", "restricted", "can't", "jan"],
     ["jan's currently working", "jan is on break"]),

    # ── Bug: "Hannah's check-in body" got parsed as admin task query ──
    ("hannah_checkin_body", HANNAH_ID, "Hannah", False, True,
     "Hours: 2:40PM-7PM / 10PM onwards Tasks: SC/Groove/Shopify Msgs",
     None,  # any non-error reply is OK; this is a check-in
     ["don't know anyone named 'onward'", "don't know anyone named 'my'"]),

    # ── Cross-team comparison query ──
    ("cross_team_most_vacation", JAN_ID, "Jan", True, False,
     "who has the most vacation days?",
     ["hannah", "10"],
     ["error", "ClientError"]),

    # ── Cross-team perf bonus query ──
    ("perf_bonus_dates", JAN_ID, "Jan", True, False,
     "list everyone's perf bonus dates",
     ["dec 16", "jan 15"],
     ["error"]),

    # ── Self-history query for worker ──
    ("my_hours_self", GER_ID, "Gerrielyn", False, False,
     "my hours",
     ["pay period"],
     ["error", "ClientError"]),
]


def run_fixtures(verbose: bool = True) -> tuple[int, int, list[str]]:
    """Run all fixtures. Returns (passed, failed, failure_details)."""
    workers = sheets.load_roster()
    passed = 0
    failed = 0
    failures: list[str] = []

    for fixture in FIXTURES:
        label, sid, sname, is_owner, is_manager, message, must_contain, must_not_contain = fixture

        try:
            reply = router.route(
                text=message,
                speaker_user_id=sid,
                speaker_name=sname,
                is_owner=is_owner,
                is_manager=is_manager,
                workers=workers,
            )
        except Exception as e:
            failed += 1
            failures.append(f"  [{label}] ROUTER RAISED: {type(e).__name__}: {e}")
            if verbose:
                print(f"  [FAIL] {label}: router raised {type(e).__name__}")
            continue

        reply_low = (reply or "").lower()

        # Check must_contain (at least one)
        contain_ok = True
        if must_contain:
            contain_ok = any(s.lower() in reply_low for s in must_contain)

        # Check must_not_contain (none of them)
        not_contain_ok = True
        if must_not_contain:
            not_contain_ok = not any(s.lower() in reply_low for s in must_not_contain)

        if contain_ok and not_contain_ok:
            passed += 1
            if verbose:
                snippet = (reply or "")[:80].replace("\n", " ")
                print(f"  [PASS] {label}: {snippet}...")
        else:
            failed += 1
            reason = []
            if not contain_ok:
                reason.append(f"missing one of {must_contain}")
            if not not_contain_ok:
                forbidden = [s for s in (must_not_contain or []) if s.lower() in reply_low]
                reason.append(f"contains forbidden {forbidden}")
            failures.append(f"  [{label}] FAILED: {'; '.join(reason)}\n    reply: {reply[:200]}")
            if verbose:
                print(f"  [FAIL] {label}: {'; '.join(reason)}")
                print(f"     reply: {(reply or '')[:150]}")

    return passed, failed, failures


def run_multiturn_pronoun_test(verbose: bool = True) -> tuple[int, int, list[str]]:
    """Special test: multi-turn pronoun resolution.
    Turn 1: 'What is Rey working on?' → establishes Rey context
    Turn 2: 'what about his sick days?' → 'his' should resolve to Rey
    """
    workers = sheets.load_roster()
    from worker_tracker import agent_v2
    agent_v2._CONV_CACHE.clear()  # fresh history

    # Turn 1 — establish context
    router.route(
        text="What is Rey working on?",
        speaker_user_id=JAN_ID, speaker_name="Jan",
        is_owner=True, is_manager=False, workers=workers,
    )

    # Turn 2 — pronoun
    reply = router.route(
        text="what about his sick days?",
        speaker_user_id=JAN_ID, speaker_name="Jan",
        is_owner=True, is_manager=False, workers=workers,
    )

    reply_low = (reply or "").lower()
    if "rey" in reply_low and "3" in reply_low:
        if verbose:
            print(f"  [PASS] multiturn_pronoun: 'his' resolved to Rey")
        return 1, 0, []
    if verbose:
        print(f"  [FAIL] multiturn_pronoun: reply didn't mention Rey + 3 sick days")
        print(f"     reply: {(reply or '')[:200]}")
    return 0, 1, [f"  [multiturn_pronoun] reply: {reply[:200]}"]


if __name__ == "__main__":
    print(f"Running {len(FIXTURES)} regression fixtures...")
    print()
    passed, failed, failures = run_fixtures(verbose=True)

    print()
    print("Running multi-turn pronoun test...")
    p2, f2, fail2 = run_multiturn_pronoun_test(verbose=True)
    passed += p2
    failed += f2
    failures.extend(fail2)

    print()
    print(f"=== {passed} passed, {failed} failed ===")
    if failures:
        print()
        print("FAILURES:")
        for f in failures:
            print(f)
        sys.exit(1)
    sys.exit(0)
