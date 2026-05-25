"""CLI entrypoints.

Usage:
    python -m worker_tracker bot            # start the Slack bot + scheduler (default if no args)
    python -m worker_tracker weekly         # run the weekly profile synthesis once, now
    python -m worker_tracker digest         # send the daily EOD digest once, now
    python -m worker_tracker payroll        # run payroll for the just-closed period and email it
"""
from __future__ import annotations

import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "bot"
    if cmd == "bot":
        from . import bot
        bot.main()
    elif cmd == "weekly":
        from . import report
        report.run_weekly_synthesis()
    elif cmd == "digest":
        from . import report
        report.send_daily_digest()
    elif cmd == "payroll":
        from . import report
        report.run_and_send_payroll()
    elif cmd == "create_views":
        from . import worker_views
        out = worker_views.create_views_for_all()
        if not out:
            print("Every active worker already has a personal view sheet.")
        else:
            print(f"Created {len(out)} personal view sheets:")
            for name, url in out.items():
                print(f"  {name}: {url}")
    else:
        sys.exit(f"Unknown command: {cmd}. Try: bot | weekly | digest | payroll | create_views")


if __name__ == "__main__":
    main()
