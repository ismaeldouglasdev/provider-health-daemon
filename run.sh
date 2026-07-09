#!/bin/bash
# Provider Health Daemon — quick start/stop

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HEALTH_DIR="$HOME/.9router"

ensure_dirs() {
    mkdir -p "$HEALTH_DIR"
}

start() {
    ensure_dirs
    echo "🛡️  Starting Provider Health Daemon on port ${HEALTH_PROXY_PORT:-20131}..."
    python3 "$SCRIPT_DIR/daemon.py" &
    PID=$!
    echo "$PID" > /tmp/health-daemon.pid
    echo "   PID: $PID"
    echo "   To activate in OpenCode, update baseURL → http://127.0.0.1:${HEALTH_PROXY_PORT:-20131}/v1"
}

stop() {
    if [ -f /tmp/health-daemon.pid ]; then
        PID=$(cat /tmp/health-daemon.pid)
        kill "$PID" 2>/dev/null && echo "✅ Stopped (PID $PID)"
        rm -f /tmp/health-daemon.pid
    else
        echo "Daemon not running"
    fi
}

status() {
    if [ -f /tmp/health-daemon.pid ]; then
        PID=$(cat /tmp/health-daemon.pid)
        if kill -0 "$PID" 2>/dev/null; then
            echo "✅ Running (PID $PID)"
            echo ""
            echo "Health snapshot:"
            python3 -c "
import json
from pathlib import Path
h = Path.home() / '.9router' / 'health.json'
if h.exists():
    data = json.loads(h.read_text())
    for ns in ('providers', 'models'):
        for name, entry in data.get(ns, {}).items():
            if entry.get('status') != 'healthy':
                print(f'  {ns[:-1]}: {name} → {entry[\"status\"]} ({entry.get(\"reason\",\"\")})')
"
        else
            echo "❌ PID file exists but process dead"
        fi
    else
        echo "❌ Not running"
    fi
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status) status ;;
    *) echo "Usage: $0 {start|stop|restart|status}" ;;
esac