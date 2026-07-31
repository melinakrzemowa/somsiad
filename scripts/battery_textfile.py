#!/usr/bin/env python3
"""Export real macOS battery health as node_exporter textfile metrics.

node_exporter's built-in `node_power_supply_battery_health` reads IOKit's
power-source health key, which on Apple Silicon reports "Check Battery"
even when macOS's own verdict (System Settings / system_profiler
"Condition") is Normal. This script exports the authoritative signal
instead: condition, maximum capacity % and cycle count from
`system_profiler SPPowerDataType -json`.

launchd runs it every 5 minutes
(~/Library/LaunchAgents/com.melinakrzemowa.somsiad-battery.plist).
node_exporter picks the file up via --collector.textfile.directory
(set in /opt/homebrew/etc/node_exporter.args).

The output directory lives OUTSIDE the CI deploy dir on purpose —
`rsync --delete` must never touch it.

Test:
  TEXTFILE_DIR=/tmp /usr/bin/python3 battery_textfile.py && cat /tmp/battery.prom
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
OUT_FILE = os.path.join(TEXTFILE_DIR, "battery.prom")


def collect() -> list[str]:
    r = subprocess.run(
        ["system_profiler", "SPPowerDataType", "-json"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    battery = None
    for item in json.loads(r.stdout).get("SPPowerDataType", []):
        if item.get("_name") == "spbattery_information":
            battery = item
            break
    if battery is None:
        raise RuntimeError("no spbattery_information in system_profiler output")

    health = battery.get("sppower_battery_health_info", {})
    charge = battery.get("sppower_battery_charge_info", {})

    condition = health.get("sppower_battery_health", "Unknown")
    cycles = health.get("sppower_battery_cycle_count")
    # "81%" -> 81
    cap_raw = str(health.get("sppower_battery_health_maximum_capacity", ""))
    cap_match = re.match(r"(\d+)", cap_raw)

    def flag(key: str) -> int:
        return 1 if str(charge.get(key, "")).upper() == "TRUE" else 0

    lines = [
        "# HELP somsiad_battery_condition_info Battery condition from system_profiler (the authoritative macOS signal). The current condition has value 1.",
        "# TYPE somsiad_battery_condition_info gauge",
        f'somsiad_battery_condition_info{{condition="{condition}"}} 1',
        "# HELP somsiad_battery_cycle_count Battery cycle count.",
        "# TYPE somsiad_battery_cycle_count gauge",
    ]
    if cycles is not None:
        lines.append(f"somsiad_battery_cycle_count {int(cycles)}")
    lines += [
        "# HELP somsiad_battery_max_capacity_percent Maximum capacity relative to design capacity, percent.",
        "# TYPE somsiad_battery_max_capacity_percent gauge",
    ]
    if cap_match:
        lines.append(f"somsiad_battery_max_capacity_percent {int(cap_match.group(1))}")
    soc = charge.get("sppower_battery_state_of_charge")
    if soc is not None:
        lines += [
            "# HELP somsiad_battery_state_of_charge_percent Current charge level, percent.",
            "# TYPE somsiad_battery_state_of_charge_percent gauge",
            f"somsiad_battery_state_of_charge_percent {int(soc)}",
        ]
    lines += [
        "# HELP somsiad_battery_charging 1 if the battery is charging.",
        "# TYPE somsiad_battery_charging gauge",
        f"somsiad_battery_charging {flag('sppower_battery_is_charging')}",
        "# HELP somsiad_battery_fully_charged 1 if the battery reports fully charged.",
        "# TYPE somsiad_battery_fully_charged gauge",
        f"somsiad_battery_fully_charged {flag('sppower_battery_fully_charged')}",
    ]
    return lines


def main() -> int:
    os.makedirs(TEXTFILE_DIR, exist_ok=True)
    try:
        lines = collect()
        ok = 1
    except Exception as e:
        sys.stderr.write(f"battery_textfile: {e}\n")
        lines, ok = [], 0
    lines += [
        "# HELP somsiad_battery_scrape_success 1 if the last battery collection succeeded.",
        "# TYPE somsiad_battery_scrape_success gauge",
        f"somsiad_battery_scrape_success {ok}",
    ]
    # Atomic replace so node_exporter never reads a half-written file.
    fd, tmp = tempfile.mkstemp(dir=TEXTFILE_DIR, prefix=".battery.")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, OUT_FILE)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
