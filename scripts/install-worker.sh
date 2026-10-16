#!/bin/bash
# Installs (or reinstalls) the worker launchd agent. Idempotent.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/install-lib.sh"   # DATA_DIR, LABEL_PREFIX, render_template
LABEL="$LABEL_PREFIX.worker"
SRC="$ROOT/launchd/worker.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/transcriber-worker.log"
(cd "$ROOT/worker" && uv sync --reinstall-package transcriber-worker >/dev/null)
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
render_template "$SRC" "$DST"
for i in 1 2 3 4 5; do          # bootout асинхронный: сразу после него bootstrap может увидеть старый job
  if launchctl bootstrap "gui/$(id -u)" "$DST" 2>/dev/null; then
    break
  fi
  if [ "$i" = 5 ]; then
    echo "bootstrap failed after 5 attempts" >&2
    exit 1
  fi
  sleep 1
done
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "installed $LABEL; log: $LOG"
if [ ! -f "$DATA_DIR/config.yaml" ]; then
  echo "каталог данных не настроен: cd worker && uv run transcriber init"
fi
