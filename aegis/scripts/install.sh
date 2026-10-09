#!/usr/bin/env bash
# AEGIS installer for Raspberry Pi 5 / Ubuntu Server ARM64 (also works on x86_64 Ubuntu/Debian).
# Interactive and idempotent. Every privileged change is announced and confirmed first.
#
#   sudo ./scripts/install.sh [--data-dir /mnt/ssd/aegis] [--port 8600] [--yes] [--no-systemd]
set -euo pipefail

PREFIX=/opt/aegis
ETC=/etc/aegis
ENV_FILE=$ETC/aegis.env
DATA_DIR=/var/lib/aegis
PORT=8600
ASSUME_YES=0
NO_SYSTEMD=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
ask()  { [[ $ASSUME_YES == 1 ]] && return 0; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy]$ ]]; }

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

say "Read-only pre-flight checks"
. /etc/os-release 2>/dev/null || true
echo "OS: ${PRETTY_NAME:-unknown}  arch: $(uname -m)  kernel: $(uname -r)"
if [[ -r /proc/device-tree/model ]]; then echo "device: $(tr -d '\0' < /proc/device-tree/model)"; fi
free -h | head -2
df -h "$(dirname "$DATA_DIR")" | tail -1
PY=$(command -v python3 || true)
[[ -n "$PY" ]] || { echo "python3 is required" >&2; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || { echo "Python >= 3.11 required" >&2; exit 1; }
"$PY" -c 'import sqlite3; c=sqlite3.connect(":memory:"); c.execute("create virtual table t using fts5(x)")' \
  || { echo "SQLite with FTS5 is required" >&2; exit 1; }
if [[ -e $PREFIX && ! -f $PREFIX/.aegis-installed ]]; then
  echo "$PREFIX exists and was not created by this installer; refusing to overwrite." >&2; exit 1
fi

MISSING=()
"$PY" -c 'import venv, ensurepip' 2>/dev/null || MISSING+=(python3-venv)
command -v bwrap >/dev/null || MISSING+=(bubblewrap)
if (( ${#MISSING[@]} )); then
  say "Packages needed: ${MISSING[*]}"
  if ask "Install them with apt-get?"; then apt-get update -q && apt-get install -y -q "${MISSING[@]}"
  else warn "continuing without: ${MISSING[*]} (code execution will be unavailable without bubblewrap)"; fi
fi

say "System user 'aegis' (no login shell)"
if ! id aegis >/dev/null 2>&1; then
  ask "Create system user 'aegis'?" || exit 1
  useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin aegis
fi

say "Application files → $PREFIX"
mkdir -p "$PREFIX"
tar -C "$SRC" --exclude=.git --exclude=.venv --exclude='__pycache__' --exclude='*.pyc' --exclude=.env -cf - . \
  | tar -C "$PREFIX" -xf -
touch "$PREFIX/.aegis-installed"
"$PY" -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q --upgrade pip
"$PREFIX/.venv/bin/pip" install -q "$PREFIX"
chown -R root:root "$PREFIX"; chmod -R go-w "$PREFIX"

say "Configuration → $ENV_FILE"
mkdir -p "$ETC"
if [[ ! -f $ENV_FILE ]]; then
  sed -e "s|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|" -e "s|^BIND_PORT=.*|BIND_PORT=$PORT|" "$PREFIX/.env.example" > "$ENV_FILE"
  echo "created $ENV_FILE"
else
  echo "keeping existing $ENV_FILE (never overwritten)"
fi
chown root:aegis "$ETC" "$ENV_FILE"; chmod 750 "$ETC"; chmod 640 "$ENV_FILE"

say "Data directory → $DATA_DIR"
mkdir -p "$DATA_DIR"; chown aegis:aegis "$DATA_DIR"; chmod 750 "$DATA_DIR"
sudo -u aegis AEGIS_ENV_FILE="$ENV_FILE" "$PREFIX/.venv/bin/aegis" init

say "Sandbox check (bubblewrap as user aegis)"
if command -v bwrap >/dev/null; then
  if sudo -u aegis bwrap --unshare-all --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib \
       --proc /proc --dev /dev /usr/bin/true 2>/dev/null; then
    echo "bubblewrap works: strong sandbox enabled"
  elif [[ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null)" == 1 ]]; then
    warn "AppArmor blocks unprivileged user namespaces, so bubblewrap cannot start."
    echo "Fix: install an AppArmor profile that lets /usr/bin/bwrap (only) create user namespaces."
    echo "Trade-off: any local user could then use bwrap to create namespaces. See docs/SECURITY.md."
    if ask "Install /etc/apparmor.d/aegis-bwrap?"; then
      install -m 644 "$PREFIX/deploy/bwrap-userns.apparmor" /etc/apparmor.d/aegis-bwrap
      apparmor_parser -r /etc/apparmor.d/aegis-bwrap
      sudo -u aegis bwrap --unshare-all --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib \
        --proc /proc --dev /dev /usr/bin/true && echo "bubblewrap now works"
    else
      warn "code execution tools stay disabled until the sandbox works"
    fi
  else
    warn "bubblewrap probe failed for another reason; run: sudo -u aegis bwrap --unshare-all --ro-bind / / true"
  fi
fi

say "Dashboard password"
if ! grep -q "^ADMIN_PASSWORD_HASH='scrypt" "$ENV_FILE"; then
  if [[ $ASSUME_YES == 1 ]]; then
    warn "no password set; run later: sudo $PREFIX/.venv/bin/aegis --env-file $ENV_FILE set-password"
  else
    "$PREFIX/.venv/bin/aegis" --env-file "$ENV_FILE" set-password
    chown root:aegis "$ENV_FILE"; chmod 640 "$ENV_FILE"
  fi
fi

say "systemd service"
if [[ $NO_SYSTEMD == 1 ]]; then
  echo "skipped (--no-systemd). Start manually: sudo -u aegis AEGIS_ENV_FILE=$ENV_FILE $PREFIX/.venv/bin/aegis serve"
else
install -m 644 "$PREFIX/deploy/aegis.service" /etc/systemd/system/aegis.service
if [[ "$DATA_DIR" != /var/lib/aegis ]]; then
  mkdir -p /etc/systemd/system/aegis.service.d
  cat > /etc/systemd/system/aegis.service.d/10-data-dir.conf <<CONF
[Service]
WorkingDirectory=$DATA_DIR
ReadWritePaths=$DATA_DIR
CONF
fi
systemctl daemon-reload
if ask "Enable AEGIS at boot and start it now?"; then
  systemctl enable --now aegis
  sleep 4
  curl -fsS "http://127.0.0.1:$PORT/health" && echo " ← health" || warn "health check failed: journalctl -u aegis -n 50"
fi
fi

say "Done"
cat <<MSG
Dashboard: http://127.0.0.1:$PORT (bound to loopback only)
From the iPad: install Tailscale on the Pi and the iPad, then on the Pi run
    sudo tailscale serve --bg --https=443 http://127.0.0.1:$PORT
and open https://<pi-name>.<tailnet>.ts.net — set COOKIE_SECURE=true and
ALLOWED_HOSTS in $ENV_FILE, then: sudo systemctl restart aegis
Configure a model provider in $ENV_FILE (MODEL_PROVIDER, MODEL_NAME, API key).
Full guide: $PREFIX/docs/INSTALL.md
MSG
