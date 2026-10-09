#!/usr/bin/env bash
# Removes AEGIS completely. Nothing is left running or scheduled.
#   sudo ./scripts/uninstall.sh [--purge-data] [--yes]
# Without --purge-data the database, knowledge and backups are kept (a final backup is offered).
set -euo pipefail
PURGE=0; ASSUME_YES=0
for a in "$@"; do case "$a" in --purge-data) PURGE=1 ;; --yes|-y) ASSUME_YES=1 ;; *) echo "unknown: $a"; exit 2 ;; esac; done
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
ask() { [[ $ASSUME_YES == 1 ]] && return 0; read -r -p "$1 [y/N] " r; [[ "$r" =~ ^[Yy]$ ]]; }

ENV_FILE=/etc/aegis/aegis.env
DATA_DIR=$(grep -E '^DATA_DIR=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d "'\"" || true)
DATA_DIR=${DATA_DIR:-/var/lib/aegis}

echo "This removes: aegis.service (+drop-ins), /opt/aegis, AppArmor profile aegis-bwrap,"
echo "/etc/sudoers.d/aegis (if present), the 'aegis' user."
[[ $PURGE == 1 ]] && echo "AND deletes /etc/aegis and ALL DATA in $DATA_DIR (knowledge, tasks, backups)."
ask "Continue?" || exit 1

if [[ $PURGE == 0 && -x /opt/aegis/.venv/bin/aegis ]] && ask "Create a final backup first?"; then
  sudo -u aegis AEGIS_ENV_FILE="$ENV_FILE" /opt/aegis/.venv/bin/aegis backup || echo "backup failed (continuing)"
fi

systemctl disable --now aegis 2>/dev/null || true
rm -f /etc/systemd/system/aegis.service
rm -rf /etc/systemd/system/aegis.service.d
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed aegis 2>/dev/null || true

if [[ -f /etc/apparmor.d/aegis-bwrap ]]; then
  apparmor_parser -R /etc/apparmor.d/aegis-bwrap 2>/dev/null || true
  rm -f /etc/apparmor.d/aegis-bwrap
fi
rm -f /etc/sudoers.d/aegis
# Any sandbox processes still alive belong to the aegis user.
pkill -u aegis 2>/dev/null || true
rm -rf /opt/aegis

if [[ $PURGE == 1 ]]; then
  rm -rf /etc/aegis "$DATA_DIR"
else
  echo "kept: /etc/aegis (config) and $DATA_DIR (data, backups)"
fi
if id aegis >/dev/null 2>&1; then userdel aegis 2>/dev/null || true; fi

echo
echo "Verification:"
{ systemctl list-unit-files 2>/dev/null | grep -i aegis; } || echo "  no aegis units"
crontab -l -u aegis 2>/dev/null || echo "  no aegis crontab"
pgrep -u aegis >/dev/null 2>&1 && echo "  WARNING: aegis processes remain" || echo "  no aegis processes"
ls -d /opt/aegis 2>/dev/null || echo "  /opt/aegis removed"
