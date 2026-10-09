#!/usr/bin/env bash
# Polymath installer for Raspberry Pi 5 (Raspberry Pi OS Bookworm, 64-bit). Idempotent: run it again any time.
#
#   sudo ./install.sh                 install / upgrade, enable and start everything
#   sudo ./install.sh --dry-run       print what would be done
#   ./install.sh --root /tmp/stage    stage all files under a directory (no system changes; for testing)
#
# Result: polymath.service (the agent) and polymath-dashboard.service run at boot, the desktop logs in
# automatically and opens the dashboard. Data lives on the NVMe drive mounted at /srv/polymath.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT=""
DRY=0
SYSTEM=1
VENV=1
DESKTOP_USER="${SUDO_USER:-}"
DATA=/srv/polymath

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --root) ROOT="${2:?--root needs a directory}"; SYSTEM=0; shift ;;
        --user) DESKTOP_USER="${2:?--user needs a name}"; shift ;;
        --skip-venv) VENV=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '    + %s\n' "$*"; else "$@"; fi; }

# install_file SRC DEST MODE → prints "changed" when DEST was created or differs
install_file() {
    local src="$1" dest="$2" mode="$3"
    if [ -f "$dest" ] && cmp -s "$src" "$dest"; then
        return 0
    fi
    run install -D -m "$mode" "$src" "$dest"
    echo changed
}

# ------------------------------------------------------------------ checks
if [ "$SYSTEM" = 1 ] && [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
    die "run as root: sudo $0"
fi
command -v python3 >/dev/null || die "python3 is required"
python3 - <<'PY' || die "Python 3.11 or newer is required"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
if [ "$SYSTEM" = 1 ]; then
    [ "$(uname -m)" = aarch64 ] || warn "this is $(uname -m), not a 64-bit Raspberry Pi; continuing"
    grep -qs bookworm /etc/os-release || warn "not Raspberry Pi OS Bookworm; continuing"
    if ! mountpoint -q "$DATA"; then
        die "$DATA is not a mounted filesystem. Mount the NVMe drive there first (see docs/OPERATIONS.md); \
Polymath never writes bulk data to the SD card."
    fi
    if [ "$(stat -c %d "$DATA")" = "$(stat -c %d /)" ]; then
        die "$DATA is on the root (SD card) filesystem; mount the NVMe drive there"
    fi
fi
if [ -z "$DESKTOP_USER" ]; then
    DESKTOP_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
fi

# ------------------------------------------------------------------ system user and packages
if [ "$SYSTEM" = 1 ]; then
    if ! id polymath >/dev/null 2>&1; then
        say "creating system user polymath"
        run useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin polymath
    fi
    missing=()
    for pkg in python3-venv curl; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        say "installing ${missing[*]}"
        run apt-get install -y "${missing[@]}"
    fi
fi

# ------------------------------------------------------------------ code and virtual environment
OPT="$ROOT/opt/polymath"
say "installing code to $OPT/src"
run mkdir -p "$OPT/src"
code_changed=0
if [ "$DRY" = 0 ]; then
    stamp_before="$(find "$OPT/src" -type f \( -name '*.py' -o -name '*.sql' -o -name '*.js' -o -name '*.css' \
        -o -name '*.html' -o -name '*.sh' -o -name '*.toml' \) -exec md5sum {} + 2>/dev/null | sort -k2 | md5sum)"
    tar -C "$SRC" --exclude='./.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='./build' --exclude='*.egg-info' -cf - . \
        | tar -C "$OPT/src" -xf -
    stamp_after="$(find "$OPT/src" -type f \( -name '*.py' -o -name '*.sql' -o -name '*.js' -o -name '*.css' \
        -o -name '*.html' -o -name '*.sh' -o -name '*.toml' \) -exec md5sum {} + 2>/dev/null | sort -k2 | md5sum)"
    [ "$stamp_before" = "$stamp_after" ] || code_changed=1
fi
if [ "$VENV" = 1 ]; then
    if [ ! -x "$OPT/venv/bin/python" ]; then
        say "creating the runtime virtual environment (numpy only)"
        run python3 -m venv "$OPT/venv"
        code_changed=1
    fi
    if [ "$code_changed" = 1 ] || ! "$OPT/venv/bin/python" -c 'import polymath, numpy' 2>/dev/null; then
        say "installing polymath and numpy into the venv"
        run "$OPT/venv/bin/pip" install --quiet --upgrade pip
        run "$OPT/venv/bin/pip" install --quiet "$OPT/src"
    fi
fi

# ------------------------------------------------------------------ configuration (never overwritten)
CONF="$ROOT/etc/polymath/polymath.toml"
if [ ! -f "$CONF" ]; then
    say "writing default configuration $CONF"
    run install -D -m 0644 "$SRC/config/polymath.toml" "$CONF"
else
    say "keeping existing configuration $CONF"
fi

# ------------------------------------------------------------------ data directory
run mkdir -p "$ROOT$DATA"
if [ "$SYSTEM" = 1 ]; then
    run chown polymath:polymath "$ROOT$DATA"
    run chmod 0750 "$ROOT$DATA"
fi

# ------------------------------------------------------------------ systemd units and journald limits
units_changed=0
for unit in polymath.service polymath-dashboard.service; do
    if [ -n "$(install_file "$SRC/deploy/$unit" "$ROOT/etc/systemd/system/$unit" 0644)" ]; then
        units_changed=1
    fi
done
journald_changed="$(install_file "$SRC/deploy/journald-polymath.conf" "$ROOT/etc/systemd/journald.conf.d/polymath.conf" 0644)"
run chmod 0755 "$OPT/src/scripts/open-dashboard.sh"

# ------------------------------------------------------------------ desktop: autologin + dashboard on screen
if [ -n "$DESKTOP_USER" ]; then
    home="$(getent passwd "$DESKTOP_USER" | cut -d: -f6 || true)"
    [ -n "$home" ] || home="/home/$DESKTOP_USER"
    home="$ROOT$home"
    say "dashboard autostart for desktop user $DESKTOP_USER"
    install_file "$SRC/deploy/polymath-dashboard.desktop" "$home/.config/autostart/polymath-dashboard.desktop" 0644 \
        >/dev/null
    line="/opt/polymath/src/scripts/open-dashboard.sh &"
    labwc="$home/.config/labwc/autostart"
    if [ "$DRY" = 0 ] && ! grep -qsF "$line" "$labwc"; then
        mkdir -p "$(dirname "$labwc")"
        [ -f "$labwc" ] || printf '# user autostart (labwc)\n' >"$labwc"
        printf '%s\n' "$line" >>"$labwc"
    fi
    wayfire="$home/.config/wayfire.ini"
    if [ "$DRY" = 0 ] && ! grep -qs '^polymath *=' "$wayfire"; then
        mkdir -p "$(dirname "$wayfire")"
        touch "$wayfire"
        if grep -q '^\[autostart\]' "$wayfire"; then
            sed -i '/^\[autostart\]/a polymath = /opt/polymath/src/scripts/open-dashboard.sh' "$wayfire"
        else
            printf '\n[autostart]\npolymath = /opt/polymath/src/scripts/open-dashboard.sh\n' >>"$wayfire"
        fi
    fi
    if [ "$SYSTEM" = 1 ] && [ "$DRY" = 0 ]; then
        chown -R "$DESKTOP_USER": "$home/.config/autostart" "$home/.config/labwc" "$wayfire"
    fi
else
    warn "no desktop user found (pass --user NAME): the dashboard will not open automatically"
fi

# ------------------------------------------------------------------ enable and start
if [ "$SYSTEM" = 1 ]; then
    if command -v raspi-config >/dev/null 2>&1; then
        say "desktop autologin (raspi-config B4)"
        run raspi-config nonint do_boot_behaviour B4
    else
        warn "raspi-config not found: enable desktop autologin yourself"
    fi
    [ -z "$journald_changed" ] || run systemctl restart systemd-journald
    [ "$units_changed" = 0 ] || run systemctl daemon-reload
    run systemctl enable polymath.service polymath-dashboard.service
    if [ "$code_changed" = 1 ] || [ "$units_changed" = 1 ]; then
        run systemctl restart polymath.service polymath-dashboard.service
    else
        run systemctl start polymath.service polymath-dashboard.service
    fi
    if [ "$DRY" = 0 ]; then
        for _ in $(seq 60); do
            curl -fsS -o /dev/null --max-time 2 http://localhost:8765/health && break
            sleep 2
        done
        if curl -fsS --max-time 2 http://localhost:8765/health; then
            echo
            say "Polymath is running. Dashboard: http://$(hostname -I | awk '{print $1}'):8765/"
        else
            warn "the agent has not reported healthy yet; see: journalctl -u polymath -n 50"
        fi
    fi
fi
say "done"
