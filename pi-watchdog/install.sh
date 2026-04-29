#!/usr/bin/env bash
# Install the somsiad watchdog on the Raspberry Pi. Idempotent.
#
# Run on the Pi (after copying the pi-watchdog/ dir over):
#   sudo bash install.sh
#
# Or end-to-end from your Mac:
#   rsync -az pi-watchdog/ raspi:/tmp/pi-watchdog/
#   ssh raspi sudo bash /tmp/pi-watchdog/install.sh
#
# Reads SMTP password from stdin if /etc/somsiad-watchdog/smtp_password
# does not exist yet:
#   echo -n 'mypassword' | ssh raspi sudo bash /tmp/pi-watchdog/install.sh
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR=/opt/somsiad-watchdog
CONF_DIR=/etc/somsiad-watchdog
STATE_DIR=/var/lib/somsiad-watchdog

# 1. Directories.
install -d -m 0755 "$INSTALL_DIR"
install -d -m 0755 "$CONF_DIR"
install -d -m 0755 "$STATE_DIR"

# 2. Files.
install -m 0755 "$REPO_DIR/watchdog.py" "$INSTALL_DIR/watchdog.py"
install -m 0644 "$REPO_DIR/somsiad-watchdog.service" /etc/systemd/system/somsiad-watchdog.service
install -m 0644 "$REPO_DIR/somsiad-watchdog.timer"   /etc/systemd/system/somsiad-watchdog.timer

# 3. SMTP password. Existing file is left alone; if missing, read stdin.
if [[ ! -s "$CONF_DIR/smtp_password" ]]; then
  if [[ -t 0 ]]; then
    echo "ERROR: $CONF_DIR/smtp_password is missing. Pipe it in:" >&2
    echo "  echo -n 'YOURPASS' | sudo bash $0" >&2
    exit 2
  fi
  echo "reading SMTP password from stdin" >&2
  install -m 0600 /dev/null "$CONF_DIR/smtp_password"
  cat > "$CONF_DIR/smtp_password"
  # Trim trailing newline.
  perl -i -pe 'chomp if eof' "$CONF_DIR/smtp_password" 2>/dev/null || \
    sed -i -e :a -e '/^$/{$d;N;ba}' "$CONF_DIR/smtp_password"
fi
chmod 0600 "$CONF_DIR/smtp_password"

# 4. Reload systemd, enable + start the timer.
systemctl daemon-reload
systemctl enable --now somsiad-watchdog.timer

echo
echo "installed."
echo "  status :  systemctl status somsiad-watchdog.timer"
echo "  log    :  journalctl -u somsiad-watchdog -n 20 --no-pager"
echo "  manual :  systemctl start somsiad-watchdog.service"
