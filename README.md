# Worker Tracker

Slack bot that tracks worker check-ins, prompts every 1.5h, and sends a
daily EOD digest analyzed by Gemini. Persistent worker profiles are
synthesized weekly.

## Layout

- `worker_tracker/` — Python package (bot, scheduler, analyzer, sheets I/O)
- `setup_worker_sheet.py` — one-off script to bootstrap the Google Sheet
- `Dockerfile` — container build for Railway / Fly.io / any Docker host

## Setup

Read `worker_tracker/SETUP.md` for the full first-time setup (Slack app,
Gmail app password, .env keys, sheet bootstrap).

## Deploy

Read `worker_tracker/DEPLOY.md` for cloud deployment. Recommended host
is Railway.

## CLI

```
python -m worker_tracker bot      # start the Slack bot + scheduler
python -m worker_tracker digest   # send today's EOD digest now
python -m worker_tracker weekly   # run weekly profile synthesis now
```
