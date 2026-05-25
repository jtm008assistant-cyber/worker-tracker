# Worker Tracker — Setup

End-to-end check-in tracker. Workers DM a Slack bot to clock in. The bot
auto-prompts each worker every 1.5h asking what they did + if they need help.
At day-end you get an email with a per-worker breakdown.

## 1. Create the tracking Google Sheet

```
python setup_worker_sheet.py --email you@gmail.com
```

Copy the printed sheet ID into `C:\Ace\.env`:

```
WORKER_TRACKER_SHEET_ID=<id from above>
```

Open the sheet, go to the **Roster** tab. Replace the example row with your
workers (one per row):

| Name | Slack User ID | Email | Timezone | Expected Start | Expected EOD | Active |
| --- | --- | --- | --- | --- | --- | --- |
| Alice Doe | U01ABC… | alice@co.com | America/New_York | 09:00 | 17:00 | TRUE |

Set **Active = FALSE** to pause a worker without deleting their row.
Timezones use IANA names (`America/New_York`, `Europe/London`, `Asia/Manila`).

To find a worker's Slack User ID: in Slack, click their profile → ⋮ menu →
**Copy member ID**. Starts with `U`.

## 2. Create the Slack app (one-time, ~5 min)

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name it `Worker Tracker`. Pick the workspace where your team lives.
3. **Socket Mode** (left sidebar) → toggle ON. Generate a token named
   `socket`. Scopes: `connections:write`. Copy the `xapp-…` token.
4. **OAuth & Permissions** → **Bot Token Scopes** → add:
   - `chat:write` — send DMs
   - `im:history` — read DM messages sent to the bot
   - `im:read` — see DM channels
   - `im:write` — open DM channels
   - `users:read` — look up names
   - `app_mentions:read` — handle @-mentions gracefully
5. **Install App** → Install to Workspace → approve. Copy the
   `xoxb-…` **Bot User OAuth Token**.
6. **Event Subscriptions** → toggle ON → **Subscribe to bot events**:
   - `message.im`
   - `app_mention`
   Save.
7. **App Home** → **Messages Tab** → enable, and check **"Allow users to
   send Slash commands and messages from the messages tab"**.

Add to `C:\Ace\.env`:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

## 3. Gmail app password (for EOD email)

You're using `jtm008assistant@gmail.com`. App passwords require 2-Step
Verification enabled on the Google account.

1. <https://myaccount.google.com/apppasswords> (sign in as
   jtm008assistant@gmail.com).
2. Create an app password named `Worker Tracker`. Copy the 16-char password.

Add to `.env`:

```
GMAIL_USER=jtm008assistant@gmail.com
GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
REPORT_RECIPIENT=jtm008assistant@gmail.com
```

## 4. Gemini analysis (reuses your existing `GOOGLE_API_KEY`)

Each worker's day is run through Gemini to surface:
- A 2-3 sentence plain-English summary of what they actually did
- Concrete automation opportunities (specific scripts/tools/AI prompts)
- Manual red flags (repetitive grunt-work eating their time)
- Capacity signal: `spare capacity` / `balanced` / `stretched` / `stuck`

This is wired automatically because `GOOGLE_API_KEY` is already in your
`.env` (paid Tier 1 key). If the key ever fails or the API is down, those
fields come back blank and the rest of the EOD report still ships.

To pick a different model: `GEMINI_MODEL=gemini-2.5-flash` (default).

## 5. Optional config knobs (`.env`)

```
MANAGER_TZ=America/New_York         # which timezone REPORT_TIME_LOCAL is in
REPORT_TIME_LOCAL=22:00             # 24h HH:MM when the EOD digest is emailed
CHECKIN_INTERVAL_MINUTES=90         # how often to DM workers
MISSED_CHECKIN_GRACE_MINUTES=30     # how long after a prompt before "missed"
GEMINI_MODEL=gemini-2.5-flash       # model used for automation analysis
```

## 6. Run the bot

```
python -m worker_tracker bot
```

Leave it running. It connects to Slack via Socket Mode (no public URL
needed). Stop it with Ctrl-C.

For local testing this is fine. To run **24/7 regardless of your laptop**
(recommended), see [DEPLOY.md](DEPLOY.md) — Dockerfile + `fly.toml` are
already in this folder; Fly.io is the recommended host.

Useful one-off commands:
```
python -m worker_tracker weekly      # force a weekly profile synthesis now
python -m worker_tracker digest      # force today's EOD digest now
```

## 7. Daily flow (what your workers see)

- Worker DMs the bot anything (e.g. `morning, in`). Bot replies
  *"Got it, Alice — clocked you in. I'll check in every 1h30m. Message
  'EOD' when you're done."*
- Every 90 min the bot DMs them *"What did you get done in the last ~1h30m,
  and is anything blocking you?"*
- They reply freely. If their reply contains "help", "stuck", "?", etc.,
  it's flagged.
- When done they message anything containing `EOD`, `logging off`,
  `signing off`, `done for the day`, etc. Bot logs them out and writes
  their daily summary row.
- At `REPORT_TIME_LOCAL` you get an email with every worker's day:
  login → EOD, active hours, every check-in reply, help flags, missed
  prompts, and an overall status (OK / Possible slack / Needs help).

## What's in the sheet

- **Roster** — workers (edit anytime; bot auto-reloads on next message)
- **Activity Log** — append-only log of every login, check-in, prompt
  sent, missed prompt, help request, EOD. Useful for spot-checking the
  email digest.
- **Daily Summary** — one row per worker per day, written when they EOD
  (or when you regenerate via the email digest job).

## Troubleshooting

- **Bot never replies in Slack.** Check that the bot is invited to the
  Messages tab in the worker's Slack DM with it. The first time a worker
  DMs the bot, they may need to click on the app in Slack sidebar →
  Messages tab → say hi.
- **"You're not on the roster yet" reply.** The bot prints the worker's
  Slack User ID in that reply. Paste it into the Roster tab and set
  Active=TRUE.
- **Gmail send fails.** Verify the app password works with `python -c
  "import yagmail; yagmail.SMTP('jtm008assistant@gmail.com',
  '<password>').send('jtm008assistant@gmail.com', 'test', 'hi')"`.
- **Workers in different timezones get prompted at weird hours.** That's
  expected — the check-in interval is a wall-clock 90 min counter from
  their login, not tied to any specific timezone. If a worker logs in at
  9am their local time, prompts arrive at 10:30, 12:00, 13:30 their time.
