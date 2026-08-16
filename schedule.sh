#!/bin/bash
# Install / remove the daily auto-update LaunchAgent.
#
#   ./schedule.sh install    # run update.py every day at 23:17 local
#   ./schedule.sh uninstall  # remove it
#   ./schedule.sh status     # is it loaded, and when did it last run
#
# 23:17 local is chosen so the run lands after the CFTC publishes the COT
# report (Fridays 15:30 ET). New 13F filings are picked up the next day.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.saharcohen.holdings13f"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$(command -v python3)"

case "${1:-}" in
install)
  mkdir -p "$HOME/Library/LaunchAgents" "$HERE/logs"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$HERE/update.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>23</integer>
    <key>Minute</key><integer>17</integer>
  </dict>
  <key>StandardOutPath</key><string>$HERE/logs/update.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/update.err</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLISTEOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "installed: $LABEL (daily 23:17)"
  echo "logs: $HERE/logs/update.log"
  ;;
uninstall)
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed: $LABEL"
  ;;
status)
  if launchctl list | grep -q "$LABEL"; then
    echo "loaded:"; launchctl list | grep "$LABEL"
  else
    echo "not loaded"
  fi
  [ -f "$HERE/logs/update.log" ] && echo "--- last log lines ---" && tail -15 "$HERE/logs/update.log"
  ;;
*)
  echo "usage: $0 {install|uninstall|status}" >&2
  exit 1
  ;;
esac
