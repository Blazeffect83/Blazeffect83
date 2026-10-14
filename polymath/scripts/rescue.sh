#!/usr/bin/env bash
# Polymath rescue: bring a stuck Pi back and install this checkout, in one command, with a log.
#
#   cd ~/polymath-src/polymath && sudo ./scripts/rescue.sh            (installs with --allow-sd-card)
#   sudo ./scripts/rescue.sh [install.sh options]                     (passed on to install.sh instead)
#
# Every step has a time limit, prints ✓ (done), – (not needed) or ✗ (failed, with what to do), and the whole run
# is logged to /var/log/polymath-rescue.log and to polymath-rescue.txt in your home directory:
#   1. what is installed now, and what this checkout installs
#   2. stop the agent, the dashboard and the drive helper (never waiting on a frozen drive)
#   3. if the brain's drive is frozen: drop its mounts (lazily), reset its USB device, wait for stuck processes
#   4. finish an interrupted package install (dpkg --configure -a) and add uhubctl (optional)
#   5. install this checkout (install.sh)
#   6. check: the installed version, the services, polymath doctor
# It never formats, never deletes, and never touches any other checkout or drive.
set -uo pipefail

SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
SYS="${RESCUE_SYS:-/sys}"
DATA="${RESCUE_DATA:-/srv/polymath}"
MOUNT_ROOT="${RESCUE_MOUNT_ROOT:-/mnt/polymath}"
OPT="${RESCUE_OPT:-/opt/polymath}"
INSTALL="${RESCUE_INSTALL:-$SRC/install.sh}"
LOG="${RESCUE_LOG:-/var/log/polymath-rescue.log}"
PROBE_S="${RESCUE_PROBE_S:-10}"        # a read of the brain's database that takes longer: the drive is frozen
STUCK_WAIT_S="${RESCUE_STUCK_WAIT_S:-60}"  # after the USB reset, wait this long for stuck processes to end
MOUNT_WAIT_S="${RESCUE_MOUNT_WAIT_S:-90}"  # then this long for the drive to be mounted again
PKG_S="${RESCUE_PKG_S:-600}"
INSTALL_S="${RESCUE_INSTALL_S:-1200}"
CHECK_S="${RESCUE_CHECK_S:-120}"
STATES=()

if [ $# -eq 0 ]; then
    set -- --allow-sd-card  # how this Pi was installed; also lets the install run while the drive is away
fi

if [ "$(id -u)" != 0 ] && [ -z "${RESCUE_NOT_ROOT_OK:-}" ]; then
    echo "run it with sudo: sudo $0 $*" >&2
    exit 2
fi
mkdir -p "$(dirname "$LOG")" 2>/dev/null
: >"$LOG" 2>/dev/null || LOG="$(mktemp)"

log() { printf '%s\n' "$*" >>"$LOG"; }
say() { printf '%s\n' "$*"; log "$*"; }
step() { say ""; say "== $*"; }
ok() { say "  ✓ $*"; STATES+=("✓ $*"); }
skip() { say "  – $*"; STATES+=("– $*"); }
bad() { say "  ✗ $*"; STATES+=("✗ $*"); }

# limited SECS CMD…: run CMD for at most SECS seconds, output into the log. A command stuck on a frozen drive
# cannot be killed; it is left behind (it ends once the drive is reset) and this carries on. 124 = timed out.
limited() {
    local secs="$1" pid ticks=0 rc
    shift
    log "\$ $*"
    "$@" </dev/null >>"$LOG" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$ticks" -ge $((secs * 4)) ]; then
            log "(still running after ${secs} s: left behind)"
            return 124
        fi
        sleep 0.25
        ticks=$((ticks + 1))
    done
    wait "$pid"
    rc=$?
    log "(exit $rc)"
    return "$rc"
}

stuck_processes() {  # processes in uninterruptible sleep that belong to Polymath or the USB storage driver
    ps -eo pid=,stat=,args= 2>/dev/null | awk '$2 ~ /^D/ && /polymath|usb-storage/ {print}'
}
stuck_for_a_while() {  # stuck in 3 looks a second apart (a moment in state D is normal disk work, not a freeze)
    local pids first
    first="$(stuck_processes | awk '{print $1}' | sort)"
    [ -n "$first" ] || return 1
    for _ in 1 2; do
        sleep "${RESCUE_LOOK_S:-1}"
        pids="$(stuck_processes | awk '{print $1}' | sort)"
        first="$(comm -12 <(printf '%s\n' "$first") <(printf '%s\n' "$pids"))"
        [ -n "$first" ] || return 1
    done
    return 0
}

# Mounts are read from mountinfo, never from the filesystem: on a frozen drive even stat() of the mount point hangs
MOUNTINFO="${RESCUE_MOUNTINFO:-/proc/self/mountinfo}"
is_mounted() { awk -v m="$1" '$5 == m {f = 1} END {exit !f}' "$MOUNTINFO" 2>/dev/null; }
mount_source() {  # the device mounted at $1 (e.g. /dev/sda2), from the field after " - <fstype>"
    awk -v m="$1" '$5 == m {for (i = 6; i <= NF; i++) if ($i == "-") {print $(i + 2); exit}}' "$MOUNTINFO" 2>/dev/null
}
mounts_under() {  # mount points below $1
    awk -v r="$1/" 'index($5, r) == 1 {print $5}' "$MOUNTINFO" 2>/dev/null
}

usb_device_of() {  # the sysfs directory of the USB device a disk hangs off (with "authorized"), or nothing
    local cur
    cur="$(readlink -f "$SYS/block/$1" 2>/dev/null)" || return 1
    while [ -n "$cur" ] && [ "$cur" != "/" ]; do
        if [ -f "$cur/authorized" ] && [ -f "$cur/idVendor" ]; then
            printf '%s\n' "$cur"
            return 0
        fi
        cur="$(dirname "$cur")"
    done
    return 1
}

brain_disk() {  # sda for /dev/sda2 (the brain's mount), else the disk behind a drive under the mount root
    local src mnt
    src="$(mount_source "$DATA")"
    if [ -z "$src" ]; then
        for mnt in $(mounts_under "$MOUNT_ROOT"); do
            src="$(mount_source "$mnt")"
            [ -n "$src" ] && break
        done
    fi
    src="${src#/dev/}"
    case "$src" in
        sd*) printf '%s\n' "$src" | sed 's/[0-9]*$//' ;;
        nvme*) printf '%s\n' "$src" | sed 's/p[0-9]*$//' ;;
    esac
}

# shown SECS CMD…: like limited, but the output also goes to the screen (for the install, which takes minutes)
shown() {
    local secs="$1" pid ticks=0 rcfile
    shift
    rcfile="$(mktemp)"
    log "\$ $*"
    ( "$@" </dev/null 2>&1; echo "$?" >"$rcfile" ) | tee -a "$LOG" &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$ticks" -ge $((secs * 4)) ]; then
            log "(still running after ${secs} s: left behind)"
            return 124
        fi
        sleep 0.25
        ticks=$((ticks + 1))
    done
    wait "$pid"
    local rc
    rc="$(cat "$rcfile" 2>/dev/null)"
    rm -f "$rcfile"
    log "(exit ${rc:-?})"
    return "${rc:-1}"
}

version_of() { sed -n 's/^version = "\(.*\)"$/\1/p' "$1/pyproject.toml" 2>/dev/null | head -n 1; }

say "Polymath rescue, $(date '+%Y-%m-%d %H:%M:%S %Z') · log: $LOG"

# ------------------------------------------------------------------ 1. what is installed, what will be
step "1/6 versions"
want="$(version_of "$SRC")"
commit="$(git -c safe.directory='*' -C "$SRC" rev-parse --short=7 HEAD 2>/dev/null || true)"
have="$(cat "$OPT/build.json" 2>/dev/null || echo 'nothing installed')"
say "  installed now: $have"
say "  this checkout: v${want:-?}${commit:+ · $commit} ($SRC)"
if [ -z "$want" ] || [ ! -x "$INSTALL" ]; then
    bad "this is not a Polymath checkout (no pyproject.toml or install.sh in $SRC)"
    say "  → cd ~/polymath-src/polymath && sudo ./scripts/rescue.sh"
    exit 1
fi
ok "will install v$want"

# ------------------------------------------------------------------ 2. stop everything (never waiting)
step "2/6 stopping the agent, the dashboard and the drive helper"
limited 20 systemctl stop --no-block polymath.service polymath-dashboard.service polymath-heal.timer \
    polymath-heal.service
limited 20 systemctl kill --signal=KILL polymath.service polymath-dashboard.service
sleep 2
ok "asked to stop (anything stuck on the drive ends in step 3)"

# ------------------------------------------------------------------ 3. a frozen drive
step "3/6 the brain's drive"
disk="$(brain_disk)"
db="$DATA/db/polymath.sqlite3"
frozen=0
if stuck_for_a_while; then
    frozen=1
    say "  processes stuck on the drive:"
    stuck_processes | sed 's/^/    /' | tee -a "$LOG"
fi
if [ "$frozen" = 0 ] && is_mounted "$DATA"; then
    # a read past the page cache (cached data can make a frozen drive look fine), with a time limit
    limited "$PROBE_S" dd if="$db" of=/dev/null bs=4096 count=1 iflag=direct status=none
    case $? in
        124) frozen=1; say "  the brain's database did not answer within ${PROBE_S} s" ;;
        0) ;;
        *) limited "$PROBE_S" dd if="$db" of=/dev/null bs=4096 count=1 status=none  # no O_DIRECT here: plain read
           [ $? = 124 ] && { frozen=1; say "  the brain's database did not answer within ${PROBE_S} s"; } ;;
    esac
fi
if [ "$frozen" = 0 ]; then
    skip "the drive answers${disk:+ (/dev/$disk)}: nothing to free"
else
    for _ in 1 2 3 4 5; do
        is_mounted "$DATA" || break
        limited 10 umount -l "$DATA"
    done
    for mnt in $(mounts_under "$MOUNT_ROOT"); do
        limited 10 umount -l "$mnt"
    done
    dev=""
    [ -z "$disk" ] || dev="$(usb_device_of "$disk" || true)"
    if [ -n "$dev" ]; then
        printf '0' >"$dev/authorized" 2>>"$LOG" && sleep 3 && printf '1' >"$dev/authorized" 2>>"$LOG"
        say "  reset the USB device of /dev/$disk ($(cat "$dev/idVendor" 2>/dev/null):$(cat "$dev/idProduct" \
            2>/dev/null))"
    else
        say "  could not find the USB device of the brain's drive${disk:+ (/dev/$disk)} to reset"
    fi
    waited=0
    while [ -n "$(stuck_processes)" ] && [ "$waited" -lt "$STUCK_WAIT_S" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if [ -n "$(stuck_processes)" ]; then
        bad "processes are still stuck on the drive after ${STUCK_WAIT_S} s"
        say "  → the install still runs; if Polymath stays stuck afterwards: sudo reboot"
    else
        ok "freed the drive: lazy unmounts${dev:+, USB reset}, no process stuck any more"
    fi
    limited 20 systemctl reset-failed
    limited 30 systemctl restart --no-block 'polymath-volume@*.service'
    limited 30 udevadm trigger --action=add --subsystem-match=block
    waited=0
    until is_mounted "$DATA" || [ "$waited" -ge "$MOUNT_WAIT_S" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if is_mounted "$DATA"; then
        ok "the drive is mounted again at $DATA"
    else
        bad "the drive did not come back within ${MOUNT_WAIT_S} s"
        say "  → Polymath learns on its standby brain meanwhile; at the Pi: replug the SSD into a blue USB 3 port"
    fi
fi

# ------------------------------------------------------------------ 4. packages
step "4/6 packages"
if limited "$PKG_S" dpkg --configure -a; then
    ok "dpkg is consistent"
else
    bad "dpkg --configure -a failed (details in the log); the install goes on without it"
fi
if command -v "${RESCUE_UHUBCTL:-uhubctl}" >/dev/null 2>&1; then
    skip "uhubctl is installed already"
elif limited "$PKG_S" apt-get install -y uhubctl; then
    ok "installed uhubctl: the drive helper can now cut and restore the USB power"
else
    bad "could not install uhubctl (optional; details in the log)"
fi

# ------------------------------------------------------------------ 5. install
step "5/6 installing v$want ($INSTALL $*)"
shown "$INSTALL_S" "$INSTALL" "$@"
rc=$?
if [ "$rc" = 0 ]; then
    ok "install.sh finished"
elif [ "$rc" = 124 ]; then
    bad "install.sh still runs after ${INSTALL_S} s (left behind)"
else
    bad "install.sh stopped with exit status $rc"
    say "  its last lines:"
    tail -n 15 "$LOG" | sed 's/^/    /'
fi

# ------------------------------------------------------------------ 6. check
step "6/6 checking"
now="$(cat "$OPT/build.json" 2>/dev/null || true)"
case "$now" in
    *"\"$want\""*) ok "installed: $now" ;;
    *) bad "still installed: ${now:-nothing} (expected v$want)" ;;
esac
waited=0
agent="$(systemctl is-active polymath.service 2>/dev/null || true)"
while [ "$agent" != active ] && [ "$waited" -lt "$CHECK_S" ]; do
    sleep 2
    waited=$((waited + 2))
    agent="$(systemctl is-active polymath.service 2>/dev/null || true)"
done
dash="$(systemctl is-active polymath-dashboard.service 2>/dev/null || true)"
if [ "$agent" = active ]; then
    ok "the agent runs (it may be waiting for its drive or learning on standby: see doctor below)"
else
    bad "the agent is '$agent' after ${CHECK_S} s (details: journalctl -u polymath -n 60)"
fi
[ "$dash" = active ] && ok "the dashboard runs" || bad "the dashboard is '$dash'"
say ""
say "polymath doctor:"
shown 60 "${RESCUE_POLYMATH:-polymath}" doctor

# ------------------------------------------------------------------ summary
say ""
say "== summary"
failed=0
for s in "${STATES[@]}"; do
    say "  $s"
    case "$s" in "✗"*) failed=1 ;; esac
done
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    if [ -n "$home" ] && cp "$LOG" "$home/polymath-rescue.txt" 2>/dev/null; then
        chown "$SUDO_USER": "$home/polymath-rescue.txt" 2>/dev/null
        say "the full log: $home/polymath-rescue.txt"
    fi
fi
[ "$failed" = 0 ] && say "everything worked" || say "something above is marked ✗: send the log"
exit "$failed"
