#!/usr/bin/env bash
# Remove Polymath. Learned data (/srv/polymath) and configuration (/etc/polymath) are KEPT unless --purge.
#
#   sudo ./uninstall.sh                 stop and remove services, autostart entries and code
#   sudo ./uninstall.sh --purge --yes   also delete all learned data, backups, configuration and the user
#   ./uninstall.sh --root /tmp/stage    operate on a staged tree (no system changes; for testing)
set -euo pipefail

ROOT=""
SYSTEM=1
PURGE=0
YES=0
DESKTOP_USER="${SUDO_USER:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT="${2:?}"; SYSTEM=0; shift ;;
        --purge) PURGE=1 ;;
        --yes) YES=1 ;;
        --user) DESKTOP_USER="${2:?}"; shift ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
[ "$SYSTEM" = 0 ] || [ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
if [ "$PURGE" = 1 ] && [ "$YES" != 1 ]; then
    echo "--purge deletes everything Polymath has learned; add --yes to confirm" >&2
    exit 2
fi
[ -n "$DESKTOP_USER" ] || DESKTOP_USER="$(getent passwd 1000 | cut -d: -f1 || true)"

if [ "$SYSTEM" = 1 ]; then
    say "stopping services and releasing plugged-in drives (their polymath-brain folders are kept)"
    systemctl disable --now polymath.service polymath-dashboard.service 2>/dev/null || true
    systemctl stop 'polymath-volume@*.service' 2>/dev/null || true
fi
rm -f "$ROOT/etc/systemd/system/polymath.service" "$ROOT/etc/systemd/system/polymath-dashboard.service" \
    "$ROOT/etc/systemd/system/polymath-volume@.service" "$ROOT/etc/udev/rules.d/90-polymath-storage.rules"
[ "$SYSTEM" = 0 ] || udevadm control --reload-rules 2>/dev/null || true
rm -f "$ROOT/etc/systemd/journald.conf.d/polymath.conf"
if [ "$SYSTEM" = 1 ]; then
    systemctl daemon-reload
    systemctl restart systemd-journald || true
fi

if [ -n "$DESKTOP_USER" ]; then
    home="$(getent passwd "$DESKTOP_USER" | cut -d: -f6 || true)"
    [ -n "$home" ] || home="/home/$DESKTOP_USER"
    home="$ROOT$home"
    say "removing the live feed autostart and menu entries for $DESKTOP_USER"
    rm -f "$home/.config/autostart/polymath-feed.desktop" "$home/.config/autostart/polymath-dashboard.desktop" \
        "$home/.local/share/applications/polymath-feed.desktop" \
        "$home/.local/share/applications/polymath-dashboard.desktop"
    [ ! -f "$home/.config/labwc/autostart" ] || sed -i \
        -e '\|/opt/polymath/src/scripts/open-feed.sh|d' -e '\|/opt/polymath/src/scripts/open-dashboard.sh|d' \
        "$home/.config/labwc/autostart"
    [ ! -f "$home/.config/wayfire.ini" ] || sed -i '/^polymath *= /d' "$home/.config/wayfire.ini"
fi

say "removing /opt/polymath"
rm -rf "$ROOT/opt/polymath"

if [ "$PURGE" = 1 ]; then
    say "purging learned data, backups and configuration"
    # the data directory is usually a mount point: empty it, keep the mount
    [ ! -d "$ROOT/srv/polymath" ] || find "$ROOT/srv/polymath" -mindepth 1 -delete
    rm -rf "$ROOT/etc/polymath"
    if [ "$SYSTEM" = 1 ] && id polymath >/dev/null 2>&1; then
        userdel polymath || true
    fi
else
    say "kept /srv/polymath (learned data, backups) and /etc/polymath; use --purge --yes to delete them"
fi
say "done"
