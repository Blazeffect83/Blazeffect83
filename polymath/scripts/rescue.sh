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
ETC="${RESCUE_ETC:-/etc/polymath}"
LIB="$SRC/scripts/ops-lib.sh"
# shellcheck source=scripts/ops-lib.sh
. "$LIB"

if [ $# -eq 0 ]; then
    set -- --allow-sd-card  # how this Pi was installed; also lets the install run while the drive is away
fi

if [ "$(id -u)" != 0 ] && [ -z "${RESCUE_NOT_ROOT_OK:-}" ]; then
    echo "run it with sudo: sudo $0 $*" >&2
    exit 2
fi
MOUNTINFO="${RESCUE_MOUNTINFO:-/proc/self/mountinfo}"
open_log

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
if sd_mode_on; then
    # SD-card mode: the brain is on the SD card. A drive still mounted or stuck is let go of, and its USB device
    # stays switched off (scripts/sd-card-mode.sh off switches it back on)
    if [ "$frozen" = 0 ] && ! is_mounted "$DATA" && [ -z "$(mounts_under "$MOUNT_ROOT")" ]; then
        skip "SD-card mode: the brain is on the SD card and no drive is mounted"
    else
        drop_drive_mounts
        dev=""
        [ -z "$disk" ] || dev="$(usb_device_of "$disk" || true)"
        if [ -n "$dev" ]; then
            printf '0' >"$dev/authorized" 2>>"$LOG"
            say "  switched off the USB device of /dev/$disk ($(usb_id "$dev")): SD-card mode leaves it alone"
        fi
        if wait_unstuck; then
            ok "SD-card mode: let go of the drive (nothing is stuck on it)"
        else
            bad "processes are still stuck on the drive after ${STUCK_WAIT_S} s"
            say "  → the install still runs; if Polymath stays stuck afterwards: sudo reboot"
        fi
    fi
elif [ "$frozen" = 0 ]; then
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
        say "  reset the USB device of /dev/$disk ($(usb_id "$dev"))"
    else
        say "  could not find the USB device of the brain's drive${disk:+ (/dev/$disk)} to reset"
    fi
    if wait_unstuck; then
        ok "freed the drive: lazy unmounts${dev:+, USB reset}, no process stuck any more"
    else
        bad "processes are still stuck on the drive after ${STUCK_WAIT_S} s"
        say "  → the install still runs; if Polymath stays stuck afterwards: sudo reboot"
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
        say "  → Polymath learns on its standby brain meanwhile; at the Pi: replug the SSD into a blue USB 3 port," \
            "or run everything on the SD card: sudo ./scripts/sd-card-mode.sh on"
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
finish_log polymath-rescue.txt
exit $?
