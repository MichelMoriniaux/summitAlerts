# Summit Control gate monitor

Emails you when an HOA gate controller on Summit Control Sierra goes offline, and again when it comes back online.

It reads the same data as the **Device List** pane on
https://sierra.summitcontrol.com/#/site/dashboard, calling the site's backend API directly
instead of loading the page in a browser.

## How it stays frugal

- Once it's running, each check is **one HTTP request** (`POST /v1/device/get/sites`). The default is one check every 10 minutes, about 144 requests a day.
- Session cookies are saved in `/data` and reused, so it only logs in when the session expires. It tries the refresh-token endpoint before doing a full login.
- Your site IDs are looked up once and then cached for 24 hours.
- When checks fail, it waits longer between tries (up to 1 hour) instead of retrying quickly.

## Setup

```sh
cp .env.example .env      # then fill in your Summit login and SMTP settings
docker compose build

# 1. Check that email works
docker compose run --rm gate-monitor --test-email

# 2. Check that login works and see what it detects
docker compose run --rm gate-monitor --list

# 3. Run it in the background
docker compose up -d
docker compose logs -f
```

For Gmail, use an [App Password](https://myaccount.google.com/apppasswords) for `SMTP_PASSWORD`, not your normal password.

## Deploying to a Synology NAS

See [synology/SYNOLOGY.md](synology/SYNOLOGY.md). To rebuild the image files it uses:

```sh
docker build --platform linux/amd64 -t summit-gate-monitor:latest . && docker save -o dist/summit-gate-monitor-amd64.tar summit-gate-monitor:latest
docker build --platform linux/arm64 -t summit-gate-monitor:latest . && docker save -o dist/summit-gate-monitor-arm64.tar summit-gate-monitor:latest
```

## Alert logic

- A device counts as **down** when its `badge_status` is anything other than `badge-online`. This is the dashboard's "Status" column: Offline, Connecting, Connected or Warning.
- If you set `STALE_MINUTES`, a device that shows Online but has a "last seen" time older than that also counts as down.
- An email is sent only after `CONFIRM_POLLS` consecutive down checks. This filters out short blips.
- You get one email when a gate goes down and one when it recovers. You don't get repeated emails while it stays down.
- If the monitor itself can't reach Summit Control (for example, because your password changed), it emails you after `ERROR_ALERT_AFTER` failures in a row.

Alert state is saved in the `/data` volume, so restarting the container doesn't send duplicate alerts.

See [.env.example](.env.example) for all settings.
