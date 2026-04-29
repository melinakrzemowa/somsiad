#!/usr/bin/env python3
"""Reciprocal watchdog: macOS Air pings the Raspberry Pi.

The Pi runs a watchdog that pages me when the Air is unreachable.
This script is the symmetric half: launchd runs it every minute on
the Air to make sure the Pi is alive — if the Pi dies, the Air's
own alerts wouldn't catch it (it's not in the somsiad blackbox
because Colima's Docker VM can't reach LAN peers).

Same SMTP creds as Alertmanager. State file lives next to the
weekly-check artefacts.

Test by overriding env on the command line:
  TARGET_HOST=10.255.255.1 FAILURE_THRESHOLD=1 \\
    STATE_FILE=/tmp/aw.json /usr/bin/python3 air_watchdog.py
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

DEPLOY_DIR = "/Users/kelu/services/monitoring.melinakrzemowa.pl"

TARGET_NAME = os.environ.get("TARGET_NAME", "Raspberry Pi")
# Try mDNS first; fall back to the LAN IP. macOS launchd-spawned processes
# don't always have mDNS resolution available (no user session context),
# so a static IP is required for reliability. Update if the router ever
# reassigns it — DHCP reservation is preferred.
TARGET_HOST = os.environ.get("TARGET_HOST", "raspberrypi.local")
TARGET_FALLBACK_IP = os.environ.get("TARGET_FALLBACK_IP", "192.168.0.140")

STATE_FILE = Path(os.environ.get(
    "STATE_FILE", f"{DEPLOY_DIR}/scripts/air_watchdog_state.json"
))
FAILURE_THRESHOLD = int(os.environ.get("FAILURE_THRESHOLD", "3"))
PROBE_TIMEOUT_S = int(os.environ.get("PROBE_TIMEOUT_S", "8"))

SMTP_HOST = os.environ.get("SMTP_HOST", "h18.seohost.pl")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "alert@melinakrzemowa.pl")
SMTP_PASSWORD_FILE = Path(os.environ.get(
    "SMTP_PASSWORD_FILE", f"{DEPLOY_DIR}/alertmanager/smtp_password"
))
MAIL_FROM = os.environ.get("MAIL_FROM", "alert@melinakrzemowa.pl")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "air-watchdog")
MAIL_TO = os.environ.get("ALERT_EMAIL_TO", "kelostrada@gmail.com")


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


def probe_icmp(host: str, timeout: int = PROBE_TIMEOUT_S, attempts: int = 2) -> tuple[bool, str]:
    last = ""
    for i in range(attempts):
        try:
            # macOS ping uses -W in milliseconds; we want a generous wall clock,
            # so set -t (deadline, in seconds) and -c.
            r = subprocess.run(
                ["ping", "-c", "2", "-t", str(timeout), host],
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


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"consecutive_failures": 0, "alert_sent": False}
    except Exception as e:
        log(f"state load failed: {e!r}; starting fresh")
        return {"consecutive_failures": 0, "alert_sent": False}


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


def main() -> int:
    state = load_state()
    ok, note = probe_icmp(TARGET_HOST)
    # Hostname probe failed (e.g. mDNS unavailable in launchd context); try IP.
    if not ok and TARGET_FALLBACK_IP and TARGET_FALLBACK_IP != TARGET_HOST:
        ok2, note2 = probe_icmp(TARGET_FALLBACK_IP)
        note = f"{note}; ip {TARGET_FALLBACK_IP}: {note2}"
        ok = ok2

    if ok:
        if state.get("alert_sent"):
            log("RECOVERED")
            try:
                send_email(
                    subject=f"[somsiad] {TARGET_NAME} is back up",
                    body=(
                        f"From: {socket.gethostname()} (air-watchdog)\n"
                        f"At:   {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
                        f"{TARGET_NAME} is responding again.\n\n  {note}\n"
                    ),
                )
            except Exception as e:
                log(f"recovery email failed: {e!r}")
        save_state({"consecutive_failures": 0, "alert_sent": False})
        log(f"OK ({note})")
        return 0

    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    log(f"FAIL #{state['consecutive_failures']} ({note})")

    if state["consecutive_failures"] >= FAILURE_THRESHOLD and not state.get("alert_sent"):
        try:
            send_email(
                subject=f"[somsiad] {TARGET_NAME} is unreachable",
                body=(
                    f"From: {socket.gethostname()} (air-watchdog)\n"
                    f"At:   {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                    f"After {state['consecutive_failures']} consecutive failed checks.\n\n"
                    f"  {note}\n\n"
                    "The Pi runs the Air's external watchdog, so until it is\n"
                    "back up the Air's own outages won't generate a page.\n"
                    "Check it via `ssh raspi` (Cloudflare Tunnel) or LAN.\n"
                ),
            )
            state["alert_sent"] = True
        except Exception as e:
            log(f"alert email failed: {e!r}")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
