#!/usr/bin/env python3
"""External watchdog for the Air, running on the Raspberry Pi.

Air monitors itself via somsiad — but if the Air is down, that
self-monitoring is down too. This script runs on the Pi (always-on
LAN neighbour) and pages me if the Air becomes unreachable.

Strategy:
  1. Every minute (driven by systemd timer), check whether the Air is
     reachable via two layers in this order:
       a. HTTPS to the public Grafana endpoint (the whole chain:
          cloudflared, networking, Grafana itself).
       b. ICMP ping to air.local on the LAN — if (a) fails this tells
          us if the Air is up but cloudflared is unhappy.
  2. Track consecutive failures across runs in a small state file.
  3. After FAILURE_THRESHOLD consecutive failures, send a single
     "Air is down" email.
  4. Optionally fire wake-on-LAN once. Won't help if the Air is up but
     misbehaving; might help if it actually crashed and is now waiting
     to be poked back to life.
  5. On recovery, send a single "Air is back up" email.

Config (read from environment, with safe defaults):
  AIR_HOST       — hostname/IP for ICMP probe (default: air.local)
  AIR_PUBLIC_URL — full URL for HTTPS probe (default: monitoring.melinakrzemowa.pl)
  AIR_MAC        — MAC address for wake-on-LAN (default: 50:ed:3c:16:81:11)
  STATE_FILE     — where consecutive failure count + alert state live
  SMTP_*         — SMTP server config (same as somsiad)
  ALERT_EMAIL_TO — recipient

The smtp password is read from /etc/somsiad-watchdog/smtp_password
(mode 600, gitignored — never commit secrets).
"""

from __future__ import annotations

import json
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

# ---- config ---------------------------------------------------------------

AIR_HOST = os.environ.get("AIR_HOST", "air.local")
AIR_PUBLIC_URL = os.environ.get(
    "AIR_PUBLIC_URL", "https://monitoring.melinakrzemowa.pl/login"
)
AIR_MAC = os.environ.get("AIR_MAC", "50:ed:3c:16:81:11")

STATE_FILE = Path(os.environ.get(
    "STATE_FILE", "/var/lib/somsiad-watchdog/state.json"
))

# Threshold: number of consecutive failures (~ minutes, since the timer
# fires once per minute) before we page. Two minutes catches almost all
# transient hiccups; we trade off a bit of noise resistance for speed.
FAILURE_THRESHOLD = int(os.environ.get("FAILURE_THRESHOLD", "3"))
PROBE_TIMEOUT_S = int(os.environ.get("PROBE_TIMEOUT_S", "8"))

SMTP_HOST = os.environ.get("SMTP_HOST", "h18.seohost.pl")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "alert@melinakrzemowa.pl")
SMTP_PASSWORD_FILE = Path(os.environ.get(
    "SMTP_PASSWORD_FILE", "/etc/somsiad-watchdog/smtp_password"
))
MAIL_FROM = os.environ.get("MAIL_FROM", "alert@melinakrzemowa.pl")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "raspi-watchdog")
MAIL_TO = os.environ.get("ALERT_EMAIL_TO", "kelostrada@gmail.com")

# ---- helpers --------------------------------------------------------------

def log(msg: str) -> None:
    """systemd captures stdout into the journal — that's all we need."""
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


def hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "raspi"


def probe_https(url: str, timeout: int = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """HEAD the URL via curl — lighter than GET, mirrors what Cloudflare
    actually returns to a normal client. We treat ANY HTTP status as
    'reachable' (5xx included): if Cloudflare answers at all, the edge
    is up. Only TCP/TLS-level failure means Air or its tunnel is down.
    Cloudflare Access returning 403 is still 'CF is up' and not an Air
    problem, so we don't want to alert on it."""
    try:
        r = subprocess.run(
            ["curl", "-sI", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 4,
        )
        code = (r.stdout or "").strip()
        if code and code.isdigit() and int(code) > 0:
            return True, f"http {code}"
        return False, f"http error (curl exit {r.returncode})"
    except Exception as e:
        return False, f"http error: {e}"


def probe_icmp(host: str, timeout: int = PROBE_TIMEOUT_S, attempts: int = 2) -> tuple[bool, str]:
    """Ping the host. Retry once with a short pause to absorb a cold
    mDNS resolve — without retry the first run after boot tends to lose
    both packets while avahi is still warming up."""
    last = ""
    for i in range(attempts):
        try:
            r = subprocess.run(
                ["ping", "-c", "2", "-W", str(timeout), host],
                capture_output=True, text=True, timeout=timeout + 4,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if "min/avg/max" in line:
                        return True, line.strip()
                return True, "icmp ok"
            last = (r.stderr.strip() or r.stdout.strip().split("\n")[-1] or "fail")[:120]
        except Exception as e:
            last = f"icmp error: {e}"
        if i + 1 < attempts:
            time.sleep(2)
    return False, f"icmp fail ({last})"


def fire_wol(mac: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["wakeonlan", mac],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return True, r.stdout.strip()
        return False, r.stderr.strip() or f"exit {r.returncode}"
    except Exception as e:
        return False, str(e)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"consecutive_failures": 0, "alert_sent": False, "wol_sent": False}
    except Exception as e:
        log(f"state load failed: {e!r}; starting fresh")
        return {"consecutive_failures": 0, "alert_sent": False, "wol_sent": False}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def send_email(subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
    msg["To"] = MAIL_TO
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    password = SMTP_PASSWORD_FILE.read_text().strip()
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
        s.login(SMTP_USER, password)
        s.send_message(msg)


# ---- main -----------------------------------------------------------------

def check_air() -> tuple[bool, list[str]]:
    """Two-layer probe. Healthy = at least one of HTTPS/ICMP succeeded.
    Returns (ok, [labelled probe results])."""
    notes: list[str] = []
    ok_https, msg_https = probe_https(AIR_PUBLIC_URL)
    notes.append(f"https {AIR_PUBLIC_URL}: {msg_https}")

    # Always ICMP — useful diagnostic even when HTTPS works.
    ok_icmp, msg_icmp = probe_icmp(AIR_HOST)
    notes.append(f"icmp {AIR_HOST}: {msg_icmp}")

    return (ok_https or ok_icmp), notes


def main() -> int:
    state = load_state()
    ok, notes = check_air()

    if ok:
        if state["alert_sent"]:
            # Recovery — clear flags and notify.
            log("RECOVERED")
            try:
                send_email(
                    subject=f"[somsiad] Air is back up",
                    body=(
                        f"From: {hostname()} (raspi-watchdog)\n"
                        f"At:   {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
                        f"The Air is responding again.\n\n"
                        + "\n".join(f"  {n}" for n in notes)
                    ),
                )
            except Exception as e:
                log(f"recovery email failed: {e!r}")
        # Healthy — reset.
        save_state({"consecutive_failures": 0, "alert_sent": False, "wol_sent": False})
        log(f"OK ({notes[0]} | {notes[1]})")
        return 0

    # Failed.
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    log(f"FAIL #{state['consecutive_failures']} ({notes[0]} | {notes[1]})")

    if state["consecutive_failures"] >= FAILURE_THRESHOLD and not state["alert_sent"]:
        # First time crossing the threshold — page and try WoL.
        try:
            send_email(
                subject=f"[somsiad] Air is unreachable",
                body=(
                    f"From: {hostname()} (raspi-watchdog)\n"
                    f"At:   {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                    f"After {state['consecutive_failures']} consecutive failed checks "
                    f"(~ {state['consecutive_failures']} minutes).\n\n"
                    + "\n".join(f"  {n}" for n in notes)
                    + "\n\nNext steps:\n"
                    "  1. ssh air (this works only via the Cloudflare Tunnel —\n"
                    "     if that's down, use ssh kelu@air.local from the LAN).\n"
                    "  2. If unreachable on LAN too, the Air may need a power\n"
                    "     cycle. WoL has been attempted from the Pi.\n"
                ),
            )
            state["alert_sent"] = True
        except Exception as e:
            log(f"alert email failed: {e!r}")

        if not state["wol_sent"]:
            ok_wol, msg_wol = fire_wol(AIR_MAC)
            log(f"WoL: {'sent' if ok_wol else 'failed'} ({msg_wol})")
            state["wol_sent"] = True

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
