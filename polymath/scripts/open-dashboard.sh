#!/usr/bin/env bash
# Open the Polymath dashboard full-window when the desktop starts (XDG autostart / labwc / wayfire).
# Waits for the dashboard to answer (it serves the page even while the agent is still starting).
set -u
URL="${POLYMATH_DASHBOARD_URL:-http://localhost:8765/}"
WAIT="${POLYMATH_DASHBOARD_WAIT:-180}"
LOCK="${XDG_RUNTIME_DIR:-/tmp}/polymath-dashboard.lock"

exec 9>"$LOCK"
if ! flock -n 9; then
    exit 0  # another autostart mechanism already opened it
fi

for _ in $(seq "$WAIT"); do
    if curl -fsS -o /dev/null --max-time 2 "$URL"; then
        break
    fi
    sleep 1
done

for browser in chromium-browser chromium; do
    if command -v "$browser" >/dev/null 2>&1; then
        exec "$browser" --app="$URL" --start-maximized --noerrdialogs --disable-infobars \
            --no-first-run --disable-session-crashed-bubble --password-store=basic \
            --user-data-dir="${XDG_CONFIG_HOME:-$HOME/.config}/polymath-dashboard"
    fi
done
xdg-open "$URL"
