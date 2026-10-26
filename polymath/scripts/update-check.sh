#!/bin/bash
# Ten minutes after `sudo ./install.sh` put a new version in place (it schedules this with systemd-run): is the new
# version healthy? If the agent keeps crashing (it failed, or restarted 3 or more times since the install) and does
# not report in, the previous version (kept by install.sh in /opt/polymath/previous) is put back and both services
# restart on it. An agent that is merely waiting (for its drive, say) is left alone: it is not crashing.
#
#   update-check.sh RESTARTS_AT_INSTALL
#
# POLYMATH_OPT (/opt/polymath) and POLYMATH_STATE (/var/lib/polymath) can point elsewhere for testing.
set -u
OPT="${POLYMATH_OPT:-/opt/polymath}"
STATE="${POLYMATH_STATE:-/var/lib/polymath}"
BASE="${1:-0}"
PREV="$OPT/previous"

log() { printf 'polymath update check: %s\n' "$*"; }
healthy() { curl -fsS -o /dev/null --max-time 5 http://localhost:8765/health 2>/dev/null; }
prop() { systemctl show polymath.service -p "$1" --value 2>/dev/null || true; }
json_of() { if [ -s "$1" ]; then tr -d '\n' <"$1"; else printf '"?"'; fi; }

if healthy; then
    log "the new version is healthy"
    exit 0
fi
restarts="$(prop NRestarts)"
state="$(prop ActiveState)"
case "$restarts" in '' | *[!0-9]*) restarts=0 ;; esac
case "$BASE" in '' | *[!0-9]*) BASE=0 ;; esac
grown=$((restarts - BASE))
if [ "$state" != failed ] && [ "$grown" -lt 3 ]; then
    log "not reporting in yet, but not crashing (${state:-unknown}, $grown restarts since the install): left as it is"
    exit 0
fi
if [ ! -d "$PREV/src" ]; then
    log "the new version keeps failing (${state:-unknown}, $grown restarts), and there is no previous version to go back to"
    exit 1
fi
from="$(json_of "$OPT/build.json")"
to="$(json_of "$PREV/build.json")"
log "the new version keeps failing (${state:-unknown}, $grown restarts): going back to $to"
rm -rf "$OPT/src.failed"
mv "$OPT/src" "$OPT/src.failed"
cp -a "$PREV/src" "$OPT/src"
for site in "$OPT"/venv/lib/python3*/site-packages; do
    [ -d "$site" ] || continue
    if [ -d "$PREV/site" ]; then
        rm -rf "$site/polymath" "$site"/polymath-*.dist-info
        cp -a "$PREV/site/." "$site/"
    fi
done
[ ! -f "$PREV/build.json" ] || cp -a "$PREV/build.json" "$OPT/build.json"
mkdir -p "$STATE"
printf '{"at": %s, "from": %s, "to": %s, "why": "%s, %s restarts since the install"}\n' \
    "$(date +%s)" "$from" "$to" "${state:-unknown}" "$grown" >"$STATE/update-rolled-back.json"
chmod 0644 "$STATE/update-rolled-back.json"
systemctl reset-failed polymath.service polymath-dashboard.service 2>/dev/null || true
systemctl restart --no-block polymath.service polymath-dashboard.service
log "went back; the failed version's code is kept in $OPT/src.failed"
