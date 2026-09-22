"""
Summit Control gate monitor.

Polls the Summit Control Sierra backend (the same API the dashboard's
"Device List" widget uses) and emails you when a gate goes offline and
when it comes back.

Frugality:
  * One HTTP request per poll in steady state (POST /v1/device/get/sites).
  * Session cookies are persisted to disk and reused across restarts.
  * Site IDs are cached and only re-discovered every SITE_REFRESH_HOURS.
  * On 401/403 we try the refresh-token endpoint once before a full login.
  * Failures back off exponentially (capped) instead of hammering the API.
"""

import json
import logging
import os
import pickle
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

AUTH_HOST = "https://ip-lib.summitcontrol.com:4000"
API_HOST = "https://sierra-lib.summitcontrol.com:3000"
USER_AGENT = "summit-gate-monitor/1.0"

log = logging.getLogger("summit")


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


class Config:
    username = env("SUMMIT_USERNAME", required=True)
    password = env("SUMMIT_PASSWORD", required=True)

    poll_seconds = int(env("POLL_INTERVAL_SECONDS", "600"))
    confirm_polls = int(env("CONFIRM_POLLS", "2"))
    stale_minutes = int(env("STALE_MINUTES", "0"))  # 0 = ignore last_seen
    site_refresh_hours = float(env("SITE_REFRESH_HOURS", "24"))
    error_alert_after = int(env("ERROR_ALERT_AFTER", "6"))
    device_names = [n.strip().lower() for n in env("DEVICE_NAMES", "").split(",") if n.strip()]

    smtp_host = env("SMTP_HOST", required=True)
    smtp_port = int(env("SMTP_PORT", "587"))
    smtp_user = env("SMTP_USER", "")
    smtp_password = env("SMTP_PASSWORD", "")
    smtp_from = env("SMTP_FROM") or env("SMTP_USER", "")
    alert_to = [a.strip() for a in env("ALERT_TO", required=True).split(",") if a.strip()]

    data_dir = Path(env("DATA_DIR", "/data"))


class AuthError(Exception):
    pass


class SummitClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cookie_file = cfg.data_dir / "cookies.pkl"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Origin": "https://sierra.summitcontrol.com",
            "Referer": "https://sierra.summitcontrol.com/",
        })
        self._load_cookies()

    def _load_cookies(self):
        try:
            with open(self.cookie_file, "rb") as f:
                self.session.cookies.update(pickle.load(f))
            log.info("Loaded saved session cookies")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Could not load cookies (%s), will log in fresh", e)

    def _save_cookies(self):
        tmp = self.cookie_file.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(self.session.cookies, f)
        tmp.replace(self.cookie_file)

    def login(self):
        log.info("Logging in as %s", self.cfg.username)
        r = self.session.post(
            f"{AUTH_HOST}/login",
            json={"username": self.cfg.username, "password": self.cfg.password},
            timeout=30,
        )
        if r.status_code in (401, 403) or not r.ok:
            raise AuthError(f"Login failed: HTTP {r.status_code} {r.text[:200]}")
        self._save_cookies()

    def refresh(self) -> bool:
        try:
            r = self.session.get(f"{AUTH_HOST}/refresh-token", timeout=30)
        except requests.RequestException:
            return False
        if r.ok:
            self._save_cookies()
            return True
        return False

    def _request(self, method, url, **kwargs):
        """Request with a single re-auth attempt (refresh, then full login)."""
        r = self.session.request(method, url, timeout=30, **kwargs)
        if r.status_code in (401, 403):
            log.info("Session expired, re-authenticating")
            if not self.refresh():
                self.login()
            r = self.session.request(method, url, timeout=30, **kwargs)
        if r.status_code in (401, 403):
            raise AuthError(f"Still unauthorized after re-login: {url}")
        r.raise_for_status()
        return r.json()

    def current_user(self):
        return self._request("GET", f"{AUTH_HOST}/current")

    def site_ids(self):
        user = self.current_user()
        user_id = user.get("_id") or user.get("user_id") or user.get("id")
        if not user_id:
            raise RuntimeError(f"Could not find user id in /current response: {list(user)}")
        shells = self._request("POST", f"{API_HOST}/v1/shell/get_by_user", json={"user_id": user_id})
        shell = shells[0] if isinstance(shells, list) and shells else shells
        ids = (shell or {}).get("site_ids") or []
        if not ids:
            raise RuntimeError("No site_ids found for this account")
        return ids

    def devices(self, site_ids):
        data = self._request("POST", f"{API_HOST}/v1/device/get/sites", json={"site_ids": site_ids})
        return data if isinstance(data, list) else []


# Mirrors the dashboard's badgeStatus() mapping.
STATUS_LABELS = {
    "badge-online": "Online",
    "badge-secondary": "Connecting",
    "badge-info": "Connected",
    "badge-warning": "Warning",
}


def parse_ts(value):
    if not value or value == "N/A":
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def evaluate(device, stale_minutes):
    """Return (is_down, human-readable reason)."""
    badge = device.get("badge_status") or "badge-offline"
    label = STATUS_LABELS.get(badge, "Offline")
    last_seen = parse_ts(device.get("last_seen"))

    if badge != "badge-online":
        return True, f"status is {label}"
    if stale_minutes and last_seen:
        age = (datetime.now(timezone.utc) - last_seen).total_seconds() / 60
        if age > stale_minutes:
            return True, f"status Online but last seen {age:.0f} min ago"
    return False, label


def fmt_last_seen(device):
    ts = parse_ts(device.get("last_seen"))
    if not ts:
        return "N/A"
    mins = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    ago = f"{mins:.0f} min ago" if mins < 120 else f"{mins / 60:.1f} h ago"
    return f"{ts.astimezone().strftime('%Y-%m-%d %H:%M %Z')} ({ago})"


class Mailer:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, subject, body):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.cfg.smtp_from
        msg["To"] = ", ".join(self.cfg.alert_to)
        msg.set_content(body)

        if self.cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30)
            server.starttls()
        with server:
            if self.cfg.smtp_user:
                server.login(self.cfg.smtp_user, self.cfg.smtp_password)
            server.send_message(msg)
        log.info("Email sent: %s", subject)


class State:
    """Persisted per-device alert state so restarts don't re-send alerts."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"devices": {}, "site_ids": [], "site_ids_at": 0}
        try:
            self.data.update(json.loads(path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


def check_once(client, cfg, state, mailer):
    if not state.data["site_ids"] or time.time() - state.data["site_ids_at"] > cfg.site_refresh_hours * 3600:
        state.data["site_ids"] = client.site_ids()
        state.data["site_ids_at"] = time.time()
        log.info("Monitoring %d site(s)", len(state.data["site_ids"]))

    devices = client.devices(state.data["site_ids"])
    if cfg.device_names:
        devices = [d for d in devices if (d.get("name") or "").strip().lower() in cfg.device_names]
    if not devices:
        log.warning("No devices returned (check DEVICE_NAMES / account access)")

    newly_down, recovered = [], []
    seen_ids = set()
    for d in devices:
        dev_id = str(d.get("_id") or d.get("device_id") or d.get("name"))
        seen_ids.add(dev_id)
        name = d.get("name") or dev_id
        down, reason = evaluate(d, cfg.stale_minutes)
        s = state.data["devices"].setdefault(dev_id, {"name": name, "down_count": 0, "alerted": False})
        s["name"] = name

        if down:
            s["down_count"] += 1
            s["reason"] = reason
            if not s["alerted"] and s["down_count"] >= cfg.confirm_polls:
                s["alerted"] = True
                s["down_since"] = datetime.now(timezone.utc).isoformat()
                newly_down.append((name, reason, fmt_last_seen(d)))
        else:
            if s["alerted"]:
                recovered.append((name, fmt_last_seen(d), s.get("down_since")))
            s.update(down_count=0, alerted=False, reason=reason)
            s.pop("down_since", None)

    # Forget devices that were removed from the account.
    for dev_id in list(state.data["devices"]):
        if dev_id not in seen_ids:
            del state.data["devices"][dev_id]

    summary = ", ".join(
        f"{d.get('name')}={STATUS_LABELS.get(d.get('badge_status'), 'Offline')}" for d in devices
    )
    log.info("Poll OK: %s", summary or "(no devices)")

    if newly_down:
        names = ", ".join(n for n, _, _ in newly_down)
        body = "The following gate(s) appear to be DOWN:\n\n" + "\n".join(
            f"  - {n}: {r}\n    last seen: {ls}" for n, r, ls in newly_down
        ) + "\n\nDashboard: https://sierra.summitcontrol.com/#/site/dashboard\n"
        mailer.send(f"[Gate DOWN] {names}", body)

    if recovered:
        names = ", ".join(n for n, _, _ in recovered)
        body = "The following gate(s) are back ONLINE:\n\n" + "\n".join(
            f"  - {n} (down since {since or 'unknown'}), last seen: {ls}" for n, ls, since in recovered
        ) + "\n"
        mailer.send(f"[Gate OK] {names}", body)


def main():
    logging.basicConfig(
        level=env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    mailer = Mailer(cfg)

    if "--test-email" in sys.argv:
        mailer.send("[Gate monitor] Test email", "SMTP settings work. You'll get alerts here.")
        return

    client = SummitClient(cfg)
    state = State(cfg.data_dir / "state.json")

    if "--list" in sys.argv:
        for d in client.devices(client.site_ids()):
            down, reason = evaluate(d, cfg.stale_minutes)
            print(f"{'DOWN' if down else 'ok  '}  {d.get('name')!s:30}  {reason:40}  last seen {fmt_last_seen(d)}")
        return

    log.info("Starting monitor: every %ss, alert after %d consecutive down polls",
             cfg.poll_seconds, cfg.confirm_polls)
    failures = 0
    error_alerted = False
    while True:
        try:
            check_once(client, cfg, state, mailer)
            state.save()
            if error_alerted:
                mailer.send("[Gate monitor] Recovered", "The monitor can reach Summit Control again.")
            failures, error_alerted = 0, False
            delay = cfg.poll_seconds
        except Exception as e:
            failures += 1
            log.error("Poll failed (%d in a row): %s", failures, e)
            if isinstance(e, AuthError) or failures % 3 == 0:
                state.data["site_ids"] = []  # force re-discovery next time
            if failures >= cfg.error_alert_after and not error_alerted:
                try:
                    mailer.send("[Gate monitor] Cannot check gates",
                                f"The monitor has failed {failures} times in a row.\nLast error: {e}\n")
                    error_alerted = True
                except Exception as mail_err:
                    log.error("Could not send error email: %s", mail_err)
            # Exponential backoff, capped at 1 hour, never faster than the normal interval.
            delay = min(3600, cfg.poll_seconds * (2 ** min(failures - 1, 3)))
        time.sleep(delay)


if __name__ == "__main__":
    main()
