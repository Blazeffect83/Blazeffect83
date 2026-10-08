#!/usr/bin/env bash
# Restore a backup:  sudo ./scripts/restore.sh /var/lib/aegis/backups/aegis-YYYYmmddTHHMMSSZ.db.gz
# Verifies the backup, stops the service, keeps the current DB as *.pre-restore-*, restarts, health-checks.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
BK=${1:?usage: restore.sh <backup.db.gz>}
A=/opt/aegis/.venv/bin/aegis
sudo -u aegis AEGIS_ENV_FILE=/etc/aegis/aegis.env $A verify-backup "$BK"
systemctl stop aegis
sudo -u aegis AEGIS_ENV_FILE=/etc/aegis/aegis.env $A restore "$BK" --yes
systemctl start aegis
sleep 4
PORT=$(grep -E '^BIND_PORT=' /etc/aegis/aegis.env | cut -d= -f2 | tr -d "'\"")
curl -fsS "http://127.0.0.1:${PORT:-8600}/health" && echo " ← healthy"
