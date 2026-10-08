#!/usr/bin/env bash
# Upgrade in place with automatic rollback point.
#   cd /path/to/new/aegis/checkout && sudo ./scripts/upgrade.sh
# Rollback:  sudo ./scripts/upgrade.sh --rollback
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
PREFIX=/opt/aegis; PREV=/opt/aegis.prev; A=$PREFIX/.venv/bin/aegis
ENVF=/etc/aegis/aegis.env
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=$(grep -E '^BIND_PORT=' $ENVF | cut -d= -f2 | tr -d "'\""); PORT=${PORT:-8600}
health() { sleep 5; curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null; }

if [[ "${1:-}" == "--rollback" ]]; then
  [[ -d $PREV ]] || { echo "no previous version at $PREV" >&2; exit 1; }
  echo "Rolling back code. If the upgrade migrated the database, restore the pre-upgrade backup"
  echo "listed in $PREV/.pre-upgrade-backup with scripts/restore.sh."
  systemctl stop aegis
  rm -rf "$PREFIX"; mv "$PREV" "$PREFIX"
  systemctl start aegis; health && echo "rolled back and healthy"
  exit 0
fi

echo "1/5 backup"; BK=$(sudo -u aegis AEGIS_ENV_FILE=$ENVF $A backup | "$PREFIX/.venv/bin/python" -c 'import json,sys;print(json.load(sys.stdin)["path"])')
echo "    $BK"
echo "2/5 keep current version at $PREV"; rm -rf "$PREV"; cp -a "$PREFIX" "$PREV"; echo "$BK" > "$PREV/.pre-upgrade-backup"
echo "3/5 install new code"
systemctl stop aegis
tar -C "$SRC" --exclude=.git --exclude=.venv --exclude='__pycache__' --exclude=.env -cf - . | tar -C "$PREFIX" -xf -
"$PREFIX/.venv/bin/pip" install -q "$PREFIX"
install -m 644 "$PREFIX/deploy/aegis.service" /etc/systemd/system/aegis.service; systemctl daemon-reload
echo "4/5 migrate"; sudo -u aegis AEGIS_ENV_FILE=$ENVF $A init
echo "5/5 start + health"; systemctl start aegis
if health; then echo "upgrade OK"; else echo "health check failed → rolling back"; "$0" --rollback; exit 1; fi
