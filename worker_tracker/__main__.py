"""CLI entrypoints.

Usage:
    python -m worker_tracker bot            # start the Slack bot + scheduler (default if no args)
    python -m worker_tracker weekly         # run the weekly profile synthesis once, now
    python -m worker_tracker digest         # send the daily EOD digest once, now
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
    else:
        sys.exit(f"Unknown command: {cmd}. Try: bot | weekly | digest")


if __name__ == "__main__":
    main()
