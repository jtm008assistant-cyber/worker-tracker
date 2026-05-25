# Worker Tracker — 24/7 Cloud Deploy

Your laptop being on is currently a single point of failure. This guide
gets the bot running in the cloud so it survives reboots, crashes, ISP
outages, and you closing the lid.

There are three viable hosts. Pick one — the rest of this guide covers
**Fly.io** because it's the cheapest set-and-forget option for this kind
of always-on bot. If you want a different host, the Dockerfile +
requirements.txt work anywhere.

| Host | Cost | Friction | Notes |
| --- | --- | --- | --- |
| **Fly.io** ⭐ | Free tier covers it (~$0–3/mo) | Low | Dockerfile + 5 CLI commands. Persistent volume for the SA key. **Recommended.** |
| Railway | $5/mo min | Lowest | Connect GitHub, set env vars, push. No CLI needed. |
| Cheap VPS (Hetzner, DigitalOcean) | $4–6/mo | Higher | SSH access, full control, but you manage updates + restarts. Use `systemd` or `docker compose`. |

---

## Fly.io deploy (recommended path)

### 1. One-time install + login

Install `flyctl` (it's a single binary):

**Windows (PowerShell):**
```
iwr https://fly.io/install.ps1 -useb | iex
```

Then:
```
fly auth signup     # or `fly auth login` if you already have an account
```

(Fly asks for a credit card to deter abuse, but the free tier covers a
single small VM running 24/7.)

### 2. Encode your service account key

The SA key (`C:\Ace\sa-key.json`) can't sit inside the repo or the image.
We'll set it as a Fly secret (base64 so it survives shell escaping).

**PowerShell:**
```
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Ace\sa-key.json"))
$b64 | Set-Clipboard
```

(That copies the base64 string to your clipboard. You'll paste it in step 4.)

### 3. Launch the app

From `C:\Ace`:
```
fly launch --copy-config --no-deploy --dockerfile worker_tracker/Dockerfile
```

When it asks "Would you like to copy its configuration to the new app?" →
**yes** (uses our `fly.toml`).
When it asks region → pick one near you (`iad` for US East, `lax` for
US West, etc.).
When it asks to set up a Postgres database or Upstash Redis → **no** to both.

This creates the Fly app and the persistent volume defined in `fly.toml`.

### 4. Set secrets (env vars)

```
fly secrets set `
  SLACK_BOT_TOKEN="xoxb-..." `
  SLACK_APP_TOKEN="xapp-..." `
  GOOGLE_API_KEY="..." `
  GMAIL_USER="jtm008assistant@gmail.com" `
  GMAIL_APP_PASSWORD="..." `
  REPORT_RECIPIENT="jtm008assistant@gmail.com" `
  WORKER_TRACKER_SHEET_ID="..." `
  SHARED_DRIVE_ID="0AALqx1PEg1X5Uk9PVA" `
  SHEETS_FOLDER_ID="1JDaPOTsq5zixN2xBBHMVv8leE5xwXD-g" `
  MANAGER_TZ="America/New_York" `
  REPORT_TIME_LOCAL="22:00" `
  CHECKIN_INTERVAL_MINUTES="90" `
  SERVICE_ACCOUNT_JSON_B64="<paste the base64 string from step 2>"
```

(Backtick is PowerShell's line continuation. On macOS/Linux use `\`.)

On startup, the bot decodes `SERVICE_ACCOUNT_JSON_B64` to
`/data/sa-key.json` (path set in the Dockerfile). Persistent volume means
it survives restarts.

### 5. Deploy

```
fly deploy --dockerfile worker_tracker/Dockerfile
```

This builds the image, pushes it, starts the machine. Takes ~2 min the
first time.

### 6. Verify it's alive

```
fly logs
```

You should see:
```
INFO worker_tracker.bot: Roster loaded: N active workers
INFO worker_tracker.bot: Daily digest scheduled 22:00 America/New_York
INFO worker_tracker.bot: Weekly synthesis scheduled dow=6 21:00 America/New_York
INFO worker_tracker.bot: Starting Socket Mode handler. Ctrl-C to stop.
```

DM the bot from Slack — it should reply within a few seconds.

### 7. Day-to-day ops

```
fly logs               # tail logs
fly status             # is it running?
fly ssh console        # shell into the machine
fly restart            # force restart
fly secrets list       # see which secrets are set
fly deploy             # re-deploy after code changes
```

To run weekly synthesis manually (e.g. first time, before Sunday):
```
fly ssh console --command "python -m worker_tracker weekly"
```

To send today's digest on demand:
```
fly ssh console --command "python -m worker_tracker digest"
```

### Cost

The smallest Fly machine (`shared-cpu-1x` with 256MB RAM) is free for the
first ~3 machines per account. This bot uses one. You'll pay $0 unless
your usage explodes. Even paid, it's ~$2-3/month.

---

## Alternative: Railway

If you don't want a CLI, Railway is dead simple:

1. Push this repo (or just the `worker_tracker/` folder + the Dockerfile)
   to a private GitHub repo.
2. Sign in at <https://railway.app> with GitHub.
3. **New Project** → **Deploy from GitHub repo** → pick your repo.
4. Railway auto-detects the Dockerfile. In **Variables** tab, paste every
   env var from step 4 above.
5. **Volumes** tab → create a 1GB volume mounted at `/data`.
6. **Deploy**. Done.

Railway has a $5/mo minimum but the bot uses maybe $1-2 of that.

---

## Alternative: cheap VPS (Hetzner / DigitalOcean / Linode)

If you already have a VPS or want SSH access:

```
ssh user@your-vps
git clone <your-repo> ace
cd ace
cp sa-key.json /etc/worker_tracker/sa-key.json     # whatever path
sudo nano /etc/worker_tracker/env                  # set the env vars
docker build -t worker-tracker -f worker_tracker/Dockerfile .
docker run -d --restart=always \
  --name worker-tracker \
  --env-file /etc/worker_tracker/env \
  -v /etc/worker_tracker:/data \
  worker-tracker
```

`--restart=always` makes it survive reboots. To upgrade after code
changes: `git pull && docker build ... && docker rm -f worker-tracker && docker run ...`.

---

## Things to remember

- The bot reads the **Roster** tab on every incoming DM, so you can add/remove
  workers in Google Sheets without redeploying.
- Daily and weekly schedules are cron-based inside the bot — restarting the
  bot resets in-memory check-in timers, but the sheet is authoritative so no
  history is lost. Workers logged in when you restart will get their next
  prompt scheduled fresh.
- If you change code, just `fly deploy` again. No state loss.
- If a worker's timezone or expected hours change, edit the Roster tab —
  no restart needed.
- The bot logs everything to stdout. `fly logs --tail` to follow live.
