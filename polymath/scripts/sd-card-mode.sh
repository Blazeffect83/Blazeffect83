#!/usr/bin/env bash
# Polymath SD-card mode: run the whole brain on the SD card while the SSD cannot be trusted, and switch back later.
#
#   cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh on     switch to the SD card (and install this checkout)
#   sudo ./scripts/sd-card-mode.sh on --fresh                           …starting with an empty brain
#   sudo ./scripts/sd-card-mode.sh on --keep-ssd-on                     …leaving the SSD's USB device switched on
#   sudo ./scripts/sd-card-mode.sh off                                  back to the SSD, once it works again
#   ./scripts/sd-card-mode.sh status                                    which mode, how much room (no sudo needed)
#
# on:  stop everything; switch the mode on with one drop-in (/etc/polymath/polymath.d/sd-card.toml; polymath.toml is
#      not changed); let go of the SSD (unmount it, then switch its USB device off, like unplugging it); start the
#      brain on the SD card (/var/lib/polymath/brain) from the copy the SD card kept when the brain moved onto the SSD
#      (renamed, not copied: it costs no room), or empty; install this checkout; check it runs on the SD card.
# off: switch the SSD's USB device back on and the mode off. Once the SSD is mounted, the agent starts on it and moves
#      what it learned on the SD card onto it by itself. If the SSD does not come back, SD-card mode stays on.
#
# Every step has a time limit and prints ✓ (done), – (not needed) or ✗ (failed, with what to do); the run is logged
# to /var/log/polymath-sd-card.log and to polymath-sd-card.txt in your home directory. It never formats and never
# deletes: the SD card's copy of the brain is renamed, the SSD's data is not touched.
set -uo pipefail

SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
SYS="${RESCUE_SYS:-/sys}"
DATA="${RESCUE_DATA:-/srv/polymath}"
MOUNT_ROOT="${RESCUE_MOUNT_ROOT:-/mnt/polymath}"
OPT="${RESCUE_OPT:-/opt/polymath}"
INSTALL="${RESCUE_INSTALL:-$SRC/install.sh}"
ETC="${RESCUE_ETC:-/etc/polymath}"
SD_DIR="${RESCUE_SD_DIR:-/var/lib/polymath/brain}"
STATE="${RESCUE_STATE:-/var/lib/polymath/sd-card-mode.json}"
RUN="${RESCUE_RUN:-/run/polymath}"
LOG="${RESCUE_LOG:-/var/log/polymath-sd-card.log}"
STUCK_WAIT_S="${RESCUE_STUCK_WAIT_S:-60}"   # after the SSD is let go of, wait this long for stuck processes to end
MOUNT_WAIT_S="${RESCUE_MOUNT_WAIT_S:-120}"  # off: wait this long for the SSD to be mounted again
DBCHECK_S="${RESCUE_DBCHECK_S:-900}"        # checking the SD card's copy of the brain before starting from it
INSTALL_S="${RESCUE_INSTALL_S:-1200}"
CHECK_S="${RESCUE_CHECK_S:-300}"          # a big brain on an SD card can take minutes to start
STOP_WAIT_S="${RESCUE_STOP_WAIT_S:-30}"     # a clean stop of the agent (one job slice is at most 15 s)
MOUNTINFO="${RESCUE_MOUNTINFO:-/proc/self/mountinfo}"
DROPIN="$ETC/polymath.d/sd-card.toml"
# shellcheck source=scripts/ops-lib.sh
. "$SRC/scripts/ops-lib.sh"

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; }
MODE=on
case "${1:-}" in
    on|off|status) MODE="$1"; shift ;;
    -h|--help) usage; exit 0 ;;
esac
FRESH=0
KEEP_SSD=0
EXTRA=()
while [ $# -gt 0 ]; do
    case "$1" in
        --fresh) FRESH=1 ;;
        --keep-ssd-on) KEEP_SSD=1 ;;
        --) shift; EXTRA+=("$@"); break ;;
        *) EXTRA+=("$1") ;;  # passed on to install.sh
    esac
    shift
done

json_field() {  # json_field FILE KEY: one string field of a small JSON file ("" when missing or unreadable)
    timeout 10 python3 -I -c 'import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    pass' "$1" "$2" 2>/dev/null
}
gb() { awk -v b="${1:-0}" 'BEGIN {printf "%.1f", b / 1e9}'; }
free_bytes() {  # free space on the filesystem holding $1 (or its nearest existing parent)
    local p="$1"
    while [ ! -e "$p" ] && [ "$p" != / ]; do p="$(dirname "$p")"; done
    df --output=avail -B1 "$p" 2>/dev/null | tail -n 1 | tr -d ' '
}
dir_bytes() { timeout 60 du -sb "$1" 2>/dev/null | cut -f1; }
same_fs() { [ "$(stat -c %d "$1" 2>/dev/null)" = "$(stat -c %d "$2" 2>/dev/null)" ]; }
polymath_owns() {  # give a directory tree to the polymath user (when there is one)
    id polymath >/dev/null 2>&1 || return 0
    limited 300 chown -R polymath:polymath "$1"
    chmod 0750 "$1" 2>/dev/null
}
usb_devices_with_id() {  # sysfs USB devices with the vendor:product $1
    local d
    for d in "$SYS"/bus/usb/devices/*; do
        [ -f "$d/authorized" ] && [ "$(usb_id "$d")" = "$1" ] && printf '%s\n' "$d"
    done
}
write_state() {  # what was switched off, so `off` can switch exactly that back on
    mkdir -p "$(dirname "$STATE")"
    printf '{"usb": "%s", "id": "%s", "disk": "%s", "at": %s}\n' "$1" "$2" "$3" "$(date +%s)" >"$STATE.tmp" &&
        mv -f "$STATE.tmp" "$STATE"
}
agent_on_sd() {  # the agent's own snapshot says it runs on the SD-card brain
    timeout 10 python3 -I -c 'import json, sys
st = json.load(open(sys.argv[1])).get("status") or {}
sys.exit(0 if (st.get("sd_card") or {}).get("dir") == sys.argv[2] else 1)' "$RUN/feed.json" "$SD_DIR" 2>/dev/null
}
stop_everything() {  # a clean stop first (the agent commits within one job slice), then whatever is left is killed
    local waited=0 state
    limited 20 systemctl stop --no-block polymath.service polymath-dashboard.service polymath-heal.timer \
        polymath-heal.service
    state="$(systemctl is-active polymath.service 2>/dev/null || true)"
    while { [ "$state" = active ] || [ "$state" = deactivating ]; } && [ "$waited" -lt "$STOP_WAIT_S" ]; do
        sleep 1
        waited=$((waited + 1))
        state="$(systemctl is-active polymath.service 2>/dev/null || true)"
    done
    limited 20 systemctl kill --signal=KILL polymath.service polymath-dashboard.service
    sleep "${RESCUE_SETTLE_S:-2}"
    ok "stopped (anything stuck on the SSD is let go of below)"
}
wait_active() {  # wait up to CHECK_S for the agent; prints its state
    local waited=0 agent
    agent="$(systemctl is-active polymath.service 2>/dev/null || true)"
    while [ "$agent" != active ] && [ "$waited" -lt "$CHECK_S" ]; do
        sleep 2
        waited=$((waited + 2))
        agent="$(systemctl is-active polymath.service 2>/dev/null || true)"
    done
    printf '%s\n' "$agent"
}

# ------------------------------------------------------------------ status (no root needed)
if [ "$MODE" = status ]; then
    LOG=/dev/null
    if sd_mode_on; then
        echo "SD-card mode: on (since $(date -r "$DROPIN" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?'))"
        size="$(dir_bytes "$SD_DIR")"
        echo "  brain: $SD_DIR (${size:+$(gb "$size") GB}${size:-its size needs sudo}); $(gb "$(free_bytes "$SD_DIR")") GB" \
            "free on the SD card"
        echo "  back to the SSD once it works: sudo ./scripts/sd-card-mode.sh off"
    else
        echo "SD-card mode: off (the brain lives at $DATA)"
    fi
    if is_mounted "$DATA"; then
        echo "  SSD: mounted at $DATA ($(mount_source "$DATA"))"
    elif [ -f "$STATE" ] && [ -n "$(json_field "$STATE" usb)" ]; then
        echo "  SSD: switched off ($(json_field "$STATE" id), $(json_field "$STATE" usb))"
    else
        echo "  SSD: not mounted"
    fi
    echo "  agent: $(systemctl is-active polymath.service 2>/dev/null || echo unknown)"
    exit 0
fi

if [ "$(id -u)" != 0 ] && [ -z "${RESCUE_NOT_ROOT_OK:-}" ]; then
    echo "run it with sudo: sudo $0 $MODE" >&2
    exit 2
fi
open_log
say "Polymath SD-card mode $MODE, $(date '+%Y-%m-%d %H:%M:%S %Z') · log: $LOG"

# ================================================================== off: back to the SSD
if [ "$MODE" = off ]; then
    step "1/4 stopping the agent, the dashboard and the drive helper"
    stop_everything

    step "2/4 switching back to the SSD"
    if ! sd_mode_on; then
        skip "SD-card mode is not on"
    fi
    [ -f "$DROPIN" ] && mv -f "$DROPIN" "$DROPIN.off"  # kept: if the SSD does not come back, it goes straight back
    switched=""
    usb="$(json_field "$STATE" usb)"
    id="$(json_field "$STATE" id)"
    for dev in ${usb:+"$usb"} $( [ -n "$id" ] && usb_devices_with_id "$id" ); do
        if [ -f "$dev/authorized" ] && [ "$(cat "$dev/authorized" 2>/dev/null)" = 0 ]; then
            printf '1' >"$dev/authorized" 2>>"$LOG" && switched="$dev"
            say "  switched the SSD's USB device back on ($id, $dev)"
            break
        fi
    done
    limited 20 systemctl reset-failed
    limited 30 systemctl restart --no-block 'polymath-volume@*.service'
    limited 30 udevadm trigger --action=add --subsystem-match=block
    waited=0
    until is_mounted "$DATA" || [ "$waited" -ge "$MOUNT_WAIT_S" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    back=0
    if is_mounted "$DATA"; then
        back=1
        rm -f "$DROPIN.off" "$STATE"
        ok "the SSD is back: the brain is on it again at $DATA ($(mount_source "$DATA"))"
    else
        [ -f "$DROPIN.off" ] && mv -f "$DROPIN.off" "$DROPIN"
        [ -z "$switched" ] || printf '0' >"$switched/authorized" 2>>"$LOG"
        bad "the SSD did not come back within ${MOUNT_WAIT_S} s: SD-card mode stays on, nothing else changed"
        say "  → plug the SSD into a blue USB 3 port (no hub), wait 30 s, then run this again;" \
            "lsblk must list it (sda)"
    fi

    step "3/4 starting"
    limited 20 systemctl reset-failed
    limited 30 systemctl restart --no-block polymath.service polymath-dashboard.service
    limited 20 systemctl start polymath-heal.timer
    ok "asked the agent, the dashboard and the drive helper to start"

    step "4/4 checking"
    agent="$(wait_active)"
    if [ "$agent" = active ]; then
        if [ "$back" = 1 ]; then
            ok "the agent runs on the SSD; what it learned on the SD card moves onto it by itself (the feed shows it)"
        else
            ok "the agent runs, still on the SD card"
        fi
    else
        bad "the agent is '$agent' after ${CHECK_S} s (details: journalctl -u polymath -n 60)"
    fi
    say ""
    say "polymath doctor:"
    shown 60 "${RESCUE_POLYMATH:-polymath}" doctor
    finish_log polymath-sd-card.txt
    exit $?
fi

# ================================================================== on: everything on the SD card
step "1/7 versions and room"
want="$(version_of "$SRC")"
commit="$(git -c safe.directory='*' -C "$SRC" rev-parse --short=7 HEAD 2>/dev/null || true)"
say "  installed now: $(cat "$OPT/build.json" 2>/dev/null || echo 'nothing installed')"
say "  this checkout: v${want:-?}${commit:+ · $commit} ($SRC)"
if [ -z "$want" ] || [ ! -x "$INSTALL" ]; then
    bad "this is not a Polymath checkout (no pyproject.toml or install.sh in $SRC)"
    say "  → cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh on"
    exit 1
fi
free_now="$(free_bytes "$SD_DIR")"
say "  free on the SD card: $(gb "$free_now") GB (the brain may use all of it but 2 GB, which the system keeps)"
ok "will install v$want and run it on the SD card"

step "2/7 stopping the agent, the dashboard and the drive helper"
stop_everything

step "3/7 switching SD-card mode on"
mkdir -p "$ETC/polymath.d"
{
    echo "# SD-card mode, switched on by scripts/sd-card-mode.sh on $(date '+%Y-%m-%d %H:%M'). Off (back to the SSD):"
    echo "#   cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh off"
    echo "# The whole brain runs on the SD card ($SD_DIR) and no drive is touched. polymath.toml is not changed."
    echo "[storage]"
    echo "sd_only = true"
} >"$DROPIN.tmp" && chmod 0644 "$DROPIN.tmp" && mv -f "$DROPIN.tmp" "$DROPIN"
if sd_mode_on; then
    ok "SD-card mode is on: $DROPIN"
else
    bad "could not write $DROPIN"
    finish_log polymath-sd-card.txt
    exit 1
fi

step "4/7 letting go of the SSD"
disk="$(brain_disk)"
if ! is_mounted "$DATA" && [ -z "$(mounts_under "$MOUNT_ROOT")" ]; then
    known="$(json_field "$STATE" id)"
    off=""
    if [ "$KEEP_SSD" = 0 ] && [ -n "$known" ]; then  # switched off before, and on again since (a reboot)
        for dev in $(usb_devices_with_id "$known"); do
            [ "$(cat "$dev/authorized" 2>/dev/null)" = 1 ] || continue
            printf '0' >"$dev/authorized" 2>>"$LOG" && off="$dev" && write_state "$dev" "$known" ""
        done
    fi
    if [ -n "$off" ]; then
        ok "the SSD is not mounted; switched its USB device off again ($known)"
    else
        skip "the SSD is not mounted: nothing to let go of"
    fi
else
    drop_drive_mounts
    if is_mounted "$DATA"; then
        bad "$DATA is still mounted"
    fi
    dev=""
    [ -z "$disk" ] || dev="$(usb_device_of "$disk" || true)"
    if [ "$KEEP_SSD" = 1 ]; then
        ok "unmounted the SSD${disk:+ (/dev/$disk)}; its USB device stays on (--keep-ssd-on)"
    elif [ -n "$dev" ]; then
        printf '0' >"$dev/authorized" 2>>"$LOG"
        write_state "$dev" "$(usb_id "$dev")" "$disk"
        ok "unmounted the SSD (/dev/$disk) and switched its USB device off ($(usb_id "$dev")), like unplugging it:" \
            "you may unplug it now"
    else
        ok "unmounted the SSD${disk:+ (/dev/$disk)} (its USB device was not found: unplug it to be sure)"
    fi
    if ! wait_unstuck; then
        bad "processes are still stuck on the SSD after ${STUCK_WAIT_S} s"
        say "  → Polymath starts on the SD card anyway; if anything stays stuck: sudo reboot (SD-card mode stays on)"
    fi
fi

step "5/7 the brain on the SD card"
mkdir -p "$(dirname "$SD_DIR")"
if [ -f "$SD_DIR/db/polymath.sqlite3" ]; then
    size="$(dir_bytes "$SD_DIR")"
    skip "the SD-card brain is there already ($SD_DIR, $(gb "$size") GB): it carries on"
elif [ "$FRESH" = 1 ]; then
    skip "--fresh: it starts with an empty brain and learns from scratch"
else
    moved=""
    if ! is_mounted "$DATA" && [ -f "$DATA/MOVED-TO-DRIVE.json" ]; then  # the SD card's own directory now
        moved="$(json_field "$DATA/MOVED-TO-DRIVE.json" sd_copy)"
    fi
    cand=""
    for c in ${moved:+"$moved"} $(ls -dt "$DATA"-sd-copy* 2>/dev/null); do
        [ -f "$c/db/polymath.sqlite3" ] && { cand="$c"; break; }
    done
    if [ -z "$cand" ]; then
        skip "the SD card holds no copy of the brain: it starts with an empty brain and learns from scratch"
    elif ! same_fs "$cand" "$(dirname "$SD_DIR")"; then
        bad "the SD card's copy of the brain ($cand) is on another filesystem than $SD_DIR: not moved (copying it" \
            "would need its size again); it starts with an empty brain"
    else
        say "  checking the SD card's copy of the brain ($cand, $(gb "$(dir_bytes "$cand")") GB)…"
        # immutable=1: read as it lies, nothing written next to it (no -shm owned by root)
        timeout "$DBCHECK_S" python3 -I -c 'import sqlite3, sys
con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro&immutable=1", uri=True)
r = con.execute("PRAGMA quick_check").fetchone()[0]
print(r)
sys.exit(0 if r == "ok" else 1)' "$cand/db/polymath.sqlite3" >>"$LOG" 2>&1
        rc=$?
        if [ "$rc" != 0 ]; then
            why="it failed the database check"
            [ "$rc" = 124 ] && why="checking it took longer than ${DBCHECK_S} s"
            bad "the SD card's copy of the brain was not used: $why (it is kept at $cand); it starts with an empty brain"
        else
            if [ -d "$SD_DIR" ]; then rmdir "$SD_DIR" 2>/dev/null; fi
            if [ -e "$SD_DIR" ]; then
                bad "$SD_DIR already holds files: the SD card's copy of the brain was not moved there (kept at $cand)"
            elif mv "$cand" "$SD_DIR" 2>>"$LOG"; then
                printf '{"from": "%s", "at": %s}\n' "$cand" "$(date +%s)" >"$SD_DIR/seeded-from.json"
                ok "the brain starts from the SD card's copy ($cand → $SD_DIR; renamed, not copied)"
            else
                bad "could not move $cand to $SD_DIR (details in the log); it starts with an empty brain"
            fi
        fi
    fi
fi
mkdir -p "$SD_DIR/db"  # the dashboard may write next to the database (SQLite's WAL index)
polymath_owns "$SD_DIR"

step "6/7 installing v$want ($INSTALL --allow-sd-card${EXTRA[*]:+ ${EXTRA[*]}})"
shown "$INSTALL_S" "$INSTALL" --allow-sd-card "${EXTRA[@]}"
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

step "7/7 checking"
now="$(cat "$OPT/build.json" 2>/dev/null || true)"
case "$now" in
    *"\"$want\""*) ok "installed: $now" ;;
    *) bad "still installed: ${now:-nothing} (expected v$want)" ;;
esac
agent="$(wait_active)"
if [ "$agent" = activating ]; then
    bad "the agent is still starting after ${CHECK_S} s (a big brain is slow on an SD card)"
    say "  → give it a few minutes, then: polymath doctor   (details: journalctl -u polymath -n 60)"
elif [ "$agent" != active ]; then
    bad "the agent is '$agent' after ${CHECK_S} s (details: journalctl -u polymath -n 60)"
else
    waited=0
    until agent_on_sd || [ "$waited" -ge "$CHECK_S" ]; do
        sleep 2
        waited=$((waited + 2))
    done
    if agent_on_sd; then
        ok "the agent runs on the SD card ($SD_DIR)"
    else
        bad "the agent runs, but has not said it is on the SD card within ${CHECK_S} s (see doctor below)"
    fi
fi
dash="$(systemctl is-active polymath-dashboard.service 2>/dev/null || true)"
[ "$dash" = active ] && ok "the dashboard runs" || bad "the dashboard is '$dash'"
say ""
say "polymath doctor:"
shown 60 "${RESCUE_POLYMATH:-polymath}" doctor
finish_log polymath-sd-card.txt
exit $?
