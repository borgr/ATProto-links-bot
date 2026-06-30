#!/usr/bin/env bash
# Manage the Discord -> Bluesky/Semble relay LaunchAgent.
#   ./service.sh start|stop|restart|status|logs
set -uo pipefail
LABEL=com.colab.discord-relay
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG="$HOME/PycharmProjects/discord_atproto_bridge/relay.log"

case "${1:-status}" in
  start)
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || true
    launchctl kickstart "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "started ($LABEL)";;
  stop)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "stopped ($LABEL)";;
  restart)
    # use after editing .env to pick up new secrets
    launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null \
      || { launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || true; }
    echo "restarted ($LABEL)";;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit" \
      || echo "not loaded";;
  logs)
    tail -n 40 -f "$LOG";;
  *)
    echo "usage: $0 {start|stop|restart|status|logs}";;
esac
