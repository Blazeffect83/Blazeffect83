#!/usr/bin/env bash
# Polymath installer for Raspberry Pi 4/5 (Raspberry Pi OS Bookworm or Trixie, 64-bit). Idempotent: run it again.
#
#   sudo ./install.sh                 install / upgrade, enable and start everything (NVMe at /srv/polymath)
#   sudo ./install.sh --allow-sd-card keep the brain on the SD card for now (smaller budget); drives plugged in later
#                                     are added to the brain automatically
#   sudo ./install.sh --dry-run       print what would be done
#   ./install.sh --root /tmp/stage    stage all files under a directory (no system changes; for testing)
#
# In SD-card mode (scripts/sd-card-mode.sh on) the brain runs on the SD card (/var/lib/polymath/brain) and the SSD is
# left alone: the installer then never touches /srv/polymath, and needs no --allow-sd-card.
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

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }
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
# a step that fails names itself, so a stopped install never looks like "nothing happened"
trap 'rc=$?; printf "\033[31merror:\033[0m the install stopped at line %s (exit status %s): %s\n" "$LINENO" "$rc" "$BASH_COMMAND" >&2' ERR
# mounts from mountinfo: on a frozen drive even stat() of the mount point hangs (mountpoint, stat)
is_mounted() { awk -v m="$1" '$5 == m {f = 1} END {exit !f}' /proc/self/mountinfo 2>/dev/null; }
# SD-card mode (scripts/sd-card-mode.sh wrote its drop-in): the brain is on the SD card, the SSD is not used
sd_mode() { grep -qsE '^[[:space:]]*sd_only[[:space:]]*=[[:space:]]*true' "$ROOT/etc/polymath/polymath.d/"*.toml; }
SD_DIR=/var/lib/polymath/brain
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '    + %s\n' "$*"; else "$@"; fi; }
# bounded CMD…: like run, but gives up after 15 s. A brain drive that froze makes writes to it hang in the kernel,
# where nothing can kill them: the stuck command is left behind (it ends when the drive is reset) and the install
# carries on, so the code and the services still update
bounded() {
    if [ "$DRY" = 1 ]; then printf '    + %s\n' "$*"; return 0; fi
    local out rc
    out="$(mktemp)"
    "$@" </dev/null >"$out" 2>&1 &  # its output via a file: a stuck command must not hold the installer's pipes
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge "${BOUNDED_TICKS:-30}" ]; then rm -f "$out"; return 124; fi
        sleep 0.5
        waited=$((waited + 1))
    done
    wait "$pid"
    rc=$?
    cat "$out" >&2
    rm -f "$out"
    return "$rc"
}

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
    if sd_mode; then
        say "SD-card mode is on: the brain runs on the SD card ($SD_DIR); $DATA and the SSD are left alone"
    elif [ "$ALLOW_SD" = 1 ]; then
        if ! is_mounted "$DATA"; then
            warn "keeping the brain on the SD card ($DATA) with a small budget, as asked (--allow-sd-card);" \
                "drives you plug in later are added to the brain automatically"
        fi
    elif ! is_mounted "$DATA"; then
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
        run apt-get install -y "${missing[@]}" || die "could not install ${missing[*]}: if apt says dpkg was interrupted, run 'sudo dpkg --configure -a' and this installer again"
    fi
    # optional: lets `polymath storage heal` replug the brain's drive by cutting the USB power; never blocks the install
    if ! dpkg -s uhubctl >/dev/null 2>&1; then
        say "installing uhubctl (lets Polymath replug its drive by itself)"
        run apt-get install -y uhubctl || warn "could not install uhubctl: Polymath still resets its drive, but cannot cut the USB power. Later: sudo dpkg --configure -a && sudo apt-get install uhubctl"
    fi
    # optional: lets the drive roles read each drive's own health report (SMART) and move off one that is failing
    if ! dpkg -s smartmontools >/dev/null 2>&1; then
        say "installing smartmontools (lets Polymath see a drive failing before it dies)"
        run apt-get install -y --no-install-recommends smartmontools || warn "could not install smartmontools: drive health shows as unknown. Later: sudo dpkg --configure -a && sudo apt-get install smartmontools"
    fi
fi

# ------------------------------------------------------------------ code and virtual environment
OPT="$ROOT/opt/polymath"
say "installing polymath v$(sed -n 's/^version = "\(.*\)"$/\1/p' "$SRC/pyproject.toml" | head -n 1) from $SRC" \
    "(installed now: $(cat "$OPT/build.json" 2>/dev/null || echo nothing))"
say "installing code to $OPT/src"
run mkdir -p "$OPT/src"
code_changed=0
# (v0.12.0) the version installed now is kept until the new one proves healthy (scripts/update-check.sh)
if [ "$SYSTEM" = 1 ] && [ "$DRY" = 0 ] && [ -d "$OPT/src" ]; then
    rm -rf "$OPT/previous.new"
    mkdir -p "$OPT/previous.new/site"
    cp -a "$OPT/src" "$OPT/previous.new/src"
    [ ! -f "$OPT/build.json" ] || cp -a "$OPT/build.json" "$OPT/previous.new/build.json"
    for site in "$OPT"/venv/lib/python3*/site-packages; do
        [ -d "$site/polymath" ] || continue
        cp -a "$site/polymath" "$site"/polymath-*.dist-info "$OPT/previous.new/site/" 2>/dev/null || true
    done
fi
if [ "$DRY" = 0 ]; then
    stamp_before="$(find "$OPT/src" -type f \( -name '*.py' -o -name '*.sql' -o -name '*.js' -o -name '*.css' \
        -o -name '*.html' -o -name '*.sh' -o -name '*.toml' \) -exec md5sum {} + 2>/dev/null | sort -k2 | md5sum)"
    tar -C "$SRC" --exclude='./.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='./build' --exclude='*.egg-info' -cf - . \
        | tar -C "$OPT/src" --no-same-owner --no-same-permissions -xf -
    # root owns the installed code (security audit): the services and pip never run files another user can change
    chown -R root:root "$OPT/src" 2>/dev/null || true
    chmod -R go-w "$OPT/src" 2>/dev/null || true
    stamp_after="$(find "$OPT/src" -type f \( -name '*.py' -o -name '*.sql' -o -name '*.js' -o -name '*.css' \
        -o -name '*.html' -o -name '*.sh' -o -name '*.toml' \) -exec md5sum {} + 2>/dev/null | sort -k2 | md5sum)"
    [ "$stamp_before" = "$stamp_after" ] || code_changed=1
fi
# build stamp (version, commit, commit date): the live feed shows it top right and checks the agent runs it
BUILD_JSON="$OPT/build.json"
build_version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$SRC/pyproject.toml" | head -n 1)"
build_commit="$(git -c safe.directory='*' -C "$SRC" rev-parse --short=7 HEAD 2>/dev/null || true)"
build_date="$(git -c safe.directory='*' -C "$SRC" log -1 --format=%cs 2>/dev/null || true)"
build_new="{\"version\": \"${build_version:-unknown}\", \"commit\": \"$build_commit\", \"date\": \"$build_date\"}"
if [ "$(cat "$BUILD_JSON" 2>/dev/null || true)" != "$build_new" ]; then
    code_changed=1  # a new commit restarts the agent so it runs (and reports) the installed build
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
if [ -d "$OPT/previous.new" ]; then
    if [ "$code_changed" = 1 ]; then
        rm -rf "$OPT/previous" && mv "$OPT/previous.new" "$OPT/previous"
    else
        rm -rf "$OPT/previous.new"
    fi
fi
# written after the code is installed: a feed that sees the new stamp reloads straight into the new code
if [ "$(cat "$BUILD_JSON" 2>/dev/null || true)" != "$build_new" ]; then
    say "build: polymath v${build_version:-unknown}${build_commit:+ · $build_commit}${build_date:+ ($build_date)}"
    if [ "$DRY" = 0 ]; then
        printf '%s\n' "$build_new" >"$BUILD_JSON.tmp" && chmod 0644 "$BUILD_JSON.tmp" && mv -f "$BUILD_JSON.tmp" "$BUILD_JSON"
    else
        run write "$BUILD_JSON"
    fi
fi

# ------------------------------------------------------------------ configuration (never overwritten)
# (v0.9.0) the dashboard's sign-in-free listener stays on the Pi; other devices use https with pairing ([access])
CONF="$ROOT/etc/polymath/polymath.toml"
if [ ! -f "$CONF" ]; then
    say "writing default configuration $CONF"
    run install -D -m 0644 "$SRC/config/polymath.toml" "$CONF"
else
    say "keeping existing configuration $CONF"
    if grep -qE '^host *= *"0\.0\.0\.0"' "$CONF"; then
        say "dashboard: the sign-in-free listener now stays on the Pi (host = 127.0.0.1); your PC uses https://<pi>:8443" \
            "after pairing (backup: $CONF.bak)"
        run cp -p "$CONF" "$CONF.bak"
        run sed -i 's/^host *= *"0\.0\.0\.0".*$/host = "127.0.0.1"                  # the Pi itself; other devices: [access] (https, pairing)/' "$CONF"
    fi
    if grep -qs '^minecraft_' "$CONF"; then
        say "commenting out retired minecraft_* settings in $CONF (backup: $CONF.bak)"
        run cp -p "$CONF" "$CONF.bak"
        run sed -i 's/^\(minecraft_[a-z_]* *=.*\)$/# retired (the Minecraft player check was removed): \1/' "$CONF"
    fi
fi

if sd_mode; then
    say "SD-card mode: its settings come from $ROOT/etc/polymath/polymath.d (polymath.toml is not changed)"
elif [ "$ALLOW_SD" = 1 ] && [ "$DRY" = 0 ]; then
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
if sd_mode; then
    # the SD-card brain (systemd also makes it when the agent starts: StateDirectory); /srv/polymath is not touched
    run mkdir -p "$ROOT$SD_DIR/db"
    if [ "$SYSTEM" = 1 ]; then
        run chown -R polymath:polymath "$ROOT$SD_DIR"
        run chmod 0750 "$ROOT$SD_DIR"
    fi
else
    # a brain drive that dropped out leaves a dead mount here (Input/output error): warn and carry on, so the code and
    # services still update; the agent then waits for its drive and `polymath doctor` says what to do
    run mkdir -p "$ROOT$DATA" || warn "cannot use $DATA right now (the brain's drive may have dropped out): reboot, then run this installer again"
    if [ "$SYSTEM" = 1 ]; then
        bounded chown polymath:polymath "$ROOT$DATA" || warn "cannot change the owner of $DATA (the brain's drive dropped out or froze): Polymath waits for it and brings it back by itself (polymath doctor shows how it goes)"
        bounded chmod 0750 "$ROOT$DATA" || true
    fi
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
for unit in polymath-heal.service polymath-heal.timer; do  # brings the brain's drive back if it goes missing
    if [ -n "$(install_file "$SRC/deploy/$unit" "$ROOT/etc/systemd/system/$unit" 0644)" ]; then
        units_changed=1
    fi
done
udev_changed="$(install_file "$SRC/deploy/90-polymath-storage.rules" "$ROOT/etc/udev/rules.d/90-polymath-storage.rules" 0644)"
run mkdir -p "$ROOT$MOUNT_ROOT"
run chmod 0755 "$ROOT$MOUNT_ROOT"
run chmod 0755 "$OPT/src/scripts/open-dashboard.sh" "$OPT/src/scripts/open-feed.sh" "$OPT/src/scripts/update-check.sh"
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

# ------------------------------------------------------------------ HTTPS for your other devices (interface/access.py)
if [ "$SYSTEM" = 1 ]; then
    if [ "$DRY" = 0 ]; then
        say "dashboard certificate for your home network (scripts/access-cert.sh)"
        POLYMATH_TLS_DIR="$ROOT/etc/polymath/tls" "$OPT/src/scripts/access-cert.sh" \
            || warn "no HTTPS certificate (see above): the dashboard is reachable only on the Pi itself until it is made"
    else
        run "$OPT/src/scripts/access-cert.sh"
    fi
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
    run systemctl enable --now polymath-heal.timer
    if [ -n "$udev_changed" ] || [ "$units_changed" = 1 ]; then
        run udevadm control --reload-rules
    fi
    # --no-block: an agent stuck on a frozen drive can take minutes to stop; the health wait below follows it
    if [ "$code_changed" = 1 ] || [ "$units_changed" = 1 ]; then
        restarts_before="$(systemctl show polymath.service -p NRestarts --value 2>/dev/null || echo 0)"
        run systemctl restart --no-block polymath.service polymath-dashboard.service
        if [ "$code_changed" = 1 ] && [ -d "$OPT/previous/src" ]; then
            # in 10 minutes: healthy, or back to the previous version by itself (scripts/update-check.sh)
            say "the previous version is kept: if the new one keeps failing, it is put back by itself in 10 minutes"
            systemctl stop polymath-update-check.timer polymath-update-check.service 2>/dev/null || true
            systemctl reset-failed polymath-update-check.service 2>/dev/null || true
            run systemd-run --quiet --unit=polymath-update-check --on-active=10min \
                "$OPT/src/scripts/update-check.sh" "${restarts_before:-0}" \
                || warn "could not schedule the update check: if Polymath keeps failing, polymath doctor says what to do"
        fi
    else
        run systemctl start --no-block polymath.service polymath-dashboard.service
    fi
    say "looking for drives already plugged in"
    run udevadm trigger --action=add --subsystem-match=block
    if [ "$DRY" = 0 ]; then
        # "not answering yet" and 503 ("the agent has not reported in yet") are normal while it starts: not shown
        say "waiting for the agent to report in (up to 2 minutes; a big brain on an SD card can take longer)"
        for _ in $(seq 60); do
            curl -fsS -o /dev/null --max-time 2 http://localhost:8765/health 2>/dev/null && break
            sleep 2
        done
        if curl -fsS --max-time 2 http://localhost:8765/health 2>/dev/null; then
            echo
            say "Polymath is running. Live feed: polymath feed (opens by itself at login). On the Pi: http://localhost:8765/"
            # your PC's browser: a one-time pairing code (interface/access.py); the code works once, for 15 minutes
            "$OPT/venv/bin/polymath" access pair --from-install \
                || warn "could not make a pairing code now: run 'polymath access pair' when you want to pair your PC"
        else
            warn "the agent has not reported in yet (it may still be starting): polymath doctor in a few minutes;" \
                "details: journalctl -u polymath -n 50"
        fi
    fi
fi
say "done: installed $(cat "$OPT/build.json" 2>/dev/null || echo '?')"
