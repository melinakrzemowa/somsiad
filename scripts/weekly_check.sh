#!/usr/bin/env bash
# Thin wrapper invoked by launchd. Real logic lives in weekly_check.py.
# Keeping a stable .sh entry point means the launchd plist (installed in
# ~/Library/LaunchAgents/) does not need to change when the implementation
# is updated by CI.
exec /usr/bin/python3 "$(dirname "$0")/weekly_check.py" "$@"
