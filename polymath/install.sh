#!/usr/bin/env bash
# Polymath installer for Raspberry Pi 4/5 (Raspberry Pi OS Bookworm or Trixie, 64-bit). Idempotent: run it again.
#
#   sudo ./install.sh                 install / upgrade, enable and start everything (NVMe at /srv/polymath)
#   sudo ./install.sh --allow-sd-card keep the brain on the SD card for now (smaller budget); drives plugged in later
#                                     are added to the brain automatically
#   sudo ./install.sh --dry-run       print what would be done
#   ./install.sh --root /tmp/stage    stage all files under a directory (no system changes; for testing)
#
# Result: polymath.service (the agent) and polymath-dashboard.service run at boot, the desktop logs in
# automatically and opens a terminal with the live feed of what it is learning. Any drive plugged in
# (USB SSD/HDD/stick, NVMe) is added to the brain's storage pool.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT=""
DRY=0
SYSTEM=1
VENV=1
DESKTOP_USER="${SUDO_USER:-}"
DATA=/srv/polymath
ALLOW_SD=0
MOUNT_ROOT=/mnt/polymath

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --root) ROOT="${2:?--root needs a directory}"; SYSTEM=0; shift ;;
        --user) DESKTOP_USER="${2:?--user needs a name}"; shift ;;
        --skip-venv) VENV=0 ;;
        --allow-sd-card) ALLOW_SD=1 ;;
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
    grep -qsE 'bookworm|trixie' /etc/os-release || warn "not Raspberry Pi OS Bookworm or Trixie; continuing"
    if [ "$ALLOW_SD" = 1 ]; then
        if ! mountpoint -q "$DATA" 2>/dev/null; then
            warn "keeping the brain on the SD card ($DATA) with a small budget, as asked (--allow-sd-card);" \
                "drives you plug in later are added to the brain automatically"
        fi
    elif ! mountpoint -q "$DATA"; then
        die "$DATA is not a mounted filesystem. Mount the NVMe drive there first (see docs/OPERATIONS.md), \
or pass --allow-sd-card to start on the SD card and add drives later."
    elif [ "$(stat -c %d "$DATA")" = "$(stat -c %d /)" ]; then
        die "$DATA is on the root (SD card) filesystem; mount the NVMe drive there, or pass --allow-sd-card"
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
    for pkg in python3-venv curl fdisk e2fsprogs; do
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
    if grep -qs '^minecraft_' "$CONF"; then
        say "commenting out retired minecraft_* settings in $CONF (backup: $CONF.bak)"
        run cp -p "$CONF" "$CONF.bak"
        run sed -i 's/^\(minecraft_[a-z_]* *=.*\)$/# retired (the Minecraft player check was removed): \1/' "$CONF"
    fi
fi

if [ "$ALLOW_SD" = 1 ] && [ "$DRY" = 0 ]; then
    # The brain may live on the root filesystem; size its budget to the card (≤ 15 GB, keeping 10 GB free).
    probe="$ROOT$DATA"
    while [ ! -e "$probe" ]; do probe="$(dirname "$probe")"; done
    free_bytes="$(df --output=avail -B1 "$probe" | tail -n 1 | tr -d ' ')"
    budget="$(python3 -c "f = $free_bytes / 1e9; print(round(min(15.0, max(2.0, (f - 10.0) * 0.8)), 1))")"
    changed=0
    grep -q '^require_separate_mount *= *true' "$CONF" && changed=1
    grep -q '^disk_budget_gb *= *200.0' "$CONF" && changed=1
    if [ "$changed" = 1 ]; then
        say "SD-card mode: data on the root filesystem, brain budget $budget GB on the card (backup: $CONF.bak)"
        cp -p "$CONF" "$CONF.bak"
        sed -i -e 's/^require_separate_mount *= *true/require_separate_mount = false/' \
            -e "s/^disk_budget_gb *= *200.0/disk_budget_gb = $budget/" "$CONF"
    fi
elif [ "$ALLOW_SD" = 1 ]; then
    printf '    + set require_separate_mount = false and an SD-sized disk_budget_gb in %s\n' "$CONF"
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

# ------------------------------------------------------------------ storage pool: drives plugged in become brain space
if [ -n "$(install_file "$SRC/deploy/polymath-volume@.service" "$ROOT/etc/systemd/system/polymath-volume@.service" 0644)" ]; then
    units_changed=1
fi
udev_changed="$(install_file "$SRC/deploy/90-polymath-storage.rules" "$ROOT/etc/udev/rules.d/90-polymath-storage.rules" 0644)"
run mkdir -p "$ROOT$MOUNT_ROOT"
run chmod 0755 "$ROOT$MOUNT_ROOT"
run chmod 0755 "$OPT/src/scripts/open-dashboard.sh" "$OPT/src/scripts/open-feed.sh"
install_file "$SRC/deploy/polymath-cli" "$ROOT/usr/local/bin/polymath" 0755 >/dev/null  # `polymath …` for every user

# ------------------------------------------------------------------ desktop: autologin + live feed on screen
FEED_LAUNCHER=/opt/polymath/src/scripts/open-feed.sh
if [ -n "$DESKTOP_USER" ]; then
    home="$(getent passwd "$DESKTOP_USER" | cut -d: -f6 || true)"
    [ -n "$home" ] || home="/home/$DESKTOP_USER"
    home="$ROOT$home"
    say "live feed terminal at login for desktop user $DESKTOP_USER"
    install_file "$SRC/deploy/polymath-feed.desktop" "$home/.config/autostart/polymath-feed.desktop" 0644 >/dev/null
    # menu entries: the live feed (another window) and the dashboard (browser)
    install_file "$SRC/deploy/polymath-feed-menu.desktop" "$home/.local/share/applications/polymath-feed.desktop" 0644 \
        >/dev/null
    install_file "$SRC/deploy/polymath-dashboard.desktop" \
        "$home/.local/share/applications/polymath-dashboard.desktop" 0644 >/dev/null
    labwc="$home/.config/labwc/autostart"
    wayfire="$home/.config/wayfire.ini"
    if [ "$DRY" = 0 ]; then
        # upgrade from the dashboard-kiosk autostart of earlier versions
        rm -f "$home/.config/autostart/polymath-dashboard.desktop"
        [ ! -f "$labwc" ] || sed -i '\|/opt/polymath/src/scripts/open-dashboard.sh|d' "$labwc"
        if ! grep -qsF "$FEED_LAUNCHER" "$labwc"; then
            mkdir -p "$(dirname "$labwc")"
            [ -f "$labwc" ] || printf '# user autostart (labwc)\n' >"$labwc"
            printf '%s &\n' "$FEED_LAUNCHER" >>"$labwc"
        fi
        mkdir -p "$(dirname "$wayfire")"
        touch "$wayfire"
        if grep -qs '^polymath *=' "$wayfire"; then
            sed -i "s|^polymath *=.*|polymath = $FEED_LAUNCHER|" "$wayfire"
        elif grep -q '^\[autostart\]' "$wayfire"; then
            sed -i "/^\[autostart\]/a polymath = $FEED_LAUNCHER" "$wayfire"
        else
            printf '\n[autostart]\npolymath = %s\n' "$FEED_LAUNCHER" >>"$wayfire"
        fi
    else
        printf '    + add %s to the XDG, labwc and wayfire autostart of %s\n' "$FEED_LAUNCHER" "$DESKTOP_USER"
    fi
    if [ "$SYSTEM" = 1 ] && [ "$DRY" = 0 ]; then
        chown -R "$DESKTOP_USER": "$home/.config/autostart" "$home/.config/labwc" "$wayfire" \
            "$home/.local/share/applications"
        if ! command -v lxterminal >/dev/null 2>&1 && ! command -v x-terminal-emulator >/dev/null 2>&1; then
            warn "no terminal emulator found: install one (sudo apt install lxterminal) to see the live feed"
        fi
    fi
else
    warn "no desktop user found (pass --user NAME): the live feed will not open automatically"
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
    if [ -n "$udev_changed" ] || [ "$units_changed" = 1 ]; then
        run udevadm control --reload-rules
    fi
    if [ "$code_changed" = 1 ] || [ "$units_changed" = 1 ]; then
        run systemctl restart polymath.service polymath-dashboard.service
    else
        run systemctl start polymath.service polymath-dashboard.service
    fi
    say "looking for drives already plugged in"
    run udevadm trigger --action=add --subsystem-match=block
    if [ "$DRY" = 0 ]; then
        for _ in $(seq 60); do
            curl -fsS -o /dev/null --max-time 2 http://localhost:8765/health && break
            sleep 2
        done
        if curl -fsS --max-time 2 http://localhost:8765/health; then
            echo
            say "Polymath is running. Live feed: polymath feed (opens by itself at login)." \
                "Dashboard: http://$(hostname -I | awk '{print $1}'):8765/"
        else
            warn "the agent has not reported healthy yet; see: journalctl -u polymath -n 50"
        fi
    fi
fi
say "done"
