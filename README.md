# Fast-track Tracker

Checks selected NZ Fast-track approval project pages for:

- new project subpages
- new files or links on tracked pages
- removed files or links
- changed link text
- changed project or subpage text

Page text changes include a compact diff showing removed lines with `-` and added
lines with `+`.

Updates are sent to Discord using a webhook. The tracker stores its previous run in
`fasttrack_state.json` and writes runtime logs to `fasttrack_tracker.log`.

## First-time setup

These commands assume the project lives at `/home/pi/FastTrack_Tracker` on the
Raspberry Pi.

```bash
cd /home/pi/FastTrack_Tracker
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Create a `.env` file:

```bash
nano .env
```

Add your Discord webhook URL:

```text
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Run the tracker once manually:

```bash
./venv/bin/python tracker.py
```

The first successful run creates `fasttrack_state.json` as the baseline. It should
not alert for all existing content on that first run. Future runs compare against
that baseline.

## Changing Tracked Projects

Edit the `PROJECTS` list in `tracker.py` and add or remove Fast-track project URLs.

After changing the list, run:

```bash
./venv/bin/python tracker.py
```

New projects will be baselined on their first run.

## Cron Setup

Open the Pi user's crontab:

```bash
crontab -e
```

Example: run every hour:

```cron
0 * * * * cd /home/pi/FastTrack_Tracker && /home/pi/FastTrack_Tracker/venv/bin/python /home/pi/FastTrack_Tracker/tracker.py >> /home/pi/FastTrack_Tracker/cron.log 2>&1
```

The script also writes its own log file:

```bash
tail -f /home/pi/FastTrack_Tracker/fasttrack_tracker.log
```

The cron redirection above writes a separate `cron.log`, which is useful for
cron-level errors such as a bad path or missing Python executable.

## Logs

Use these to confirm cron is running:

```bash
tail -n 100 /home/pi/FastTrack_Tracker/fasttrack_tracker.log
tail -n 100 /home/pi/FastTrack_Tracker/cron.log
```

If a project scrape fails or the script hits a fatal error, the traceback is logged
locally and sent to Discord.

## Notes

This tracker intentionally uses `curl_cffi` with Chrome impersonation. The
Fast-track site blocks normal `requests` traffic, so `requests` is not used as a
fallback.
