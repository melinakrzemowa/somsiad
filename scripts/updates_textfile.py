#!/usr/bin/env python3
"""Export pending-update counts as node_exporter textfile metrics.

Counts outdated Homebrew packages (`brew outdated --json=v2`) and pending
macOS updates (`softwareupdate -l`) for the host dashboard's tiles. The
weekly email (weekly_check.py) does its own richer collection with names
and versions; these metrics are just the counts.

launchd runs it every 6 hours
(~/Library/LaunchAgents/com.melinakrzemowa.somsiad-updates.plist) —
softwareupdate hits Apple's servers and takes ~a minute, so this is
deliberately much less frequent than the battery job. Staleness is
observable via node_textfile_mtime_seconds{file="updates.prom"}.

Test:
  TEXTFILE_DIR=/tmp /usr/bin/python3 updates_textfile.py && cat /tmp/updates.prom
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

TEXTFILE_DIR = os.environ.get(
    "TEXTFILE_DIR", "/Users/kelu/services/node_exporter/textfile"
)
OUT_FILE = os.path.join(TEXTFILE_DIR, "updates.prom")


def brew_outdated() -> int:
    """Count of outdated formulae + casks. Raises on failure — with
    nothing outdated brew still prints {"formulae":[],"casks":[]}."""
    r = subprocess.run(
        ["brew", "outdated", "--json=v2"],
        capture_output=True, text=True, timeout=180, check=True,
    )
    data = json.loads(r.stdout)
    return len(data.get("formulae", [])) + len(data.get("casks", []))


def macos_updates() -> tuple[int, int]:
    """(pending update count, 1 if any needs a restart). The banner and the
    "no updates" notice split between stdout and stderr depending on the OS
    release, so read both."""
    r = subprocess.run(
        ["softwareupdate", "-l"],
        capture_output=True, text=True, timeout=300, check=False,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if "No new software available" in out:
        return 0, 0
    labels = re.findall(r"^\*\s*Label:", out, re.M)
    if not labels:
        raise RuntimeError("softwareupdate -l returned no labels and no 'up to date' notice")
    restart = 1 if re.search(r"Action:\s*restart", out) else 0
    return len(labels), restart


def main() -> int:
    os.makedirs(TEXTFILE_DIR, exist_ok=True)
    lines = []
    ok = 1

    try:
        n_brew = brew_outdated()
        lines += [
            "# HELP somsiad_pending_brew_upgrades Number of outdated Homebrew formulae and casks.",
            "# TYPE somsiad_pending_brew_upgrades gauge",
            f"somsiad_pending_brew_upgrades {n_brew}",
        ]
    except Exception as e:
        sys.stderr.write(f"updates_textfile: brew: {e}\n")
        ok = 0

    try:
        n_macos, restart = macos_updates()
        lines += [
            "# HELP somsiad_pending_macos_updates Number of pending macOS software updates.",
            "# TYPE somsiad_pending_macos_updates gauge",
            f"somsiad_pending_macos_updates {n_macos}",
            "# HELP somsiad_pending_macos_restart_required 1 if any pending macOS update requires a restart.",
            "# TYPE somsiad_pending_macos_restart_required gauge",
            f"somsiad_pending_macos_restart_required {restart}",
        ]
    except Exception as e:
        sys.stderr.write(f"updates_textfile: softwareupdate: {e}\n")
        ok = 0

    lines += [
        "# HELP somsiad_updates_scrape_success 1 if the last update check fully succeeded.",
        "# TYPE somsiad_updates_scrape_success gauge",
        f"somsiad_updates_scrape_success {ok}",
    ]
    # Atomic replace so node_exporter never reads a half-written file.
    fd, tmp = tempfile.mkstemp(dir=TEXTFILE_DIR, prefix=".updates.")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, OUT_FILE)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
