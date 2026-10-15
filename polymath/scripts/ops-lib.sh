# Polymath operations library: what scripts/rescue.sh and scripts/sd-card-mode.sh share. Sourced, never run.
#
# Callers set, before sourcing: LOG (the log file), SYS (/sys), DATA (/srv/polymath), MOUNT_ROOT (/mnt/polymath),
# MOUNTINFO (/proc/self/mountinfo), STUCK_WAIT_S. Every step prints ✓ (done), – (not needed) or ✗ (failed, with what
# to do) and is kept in STATES for the summary. Nothing here ever waits on a frozen drive: commands get a time limit,
# and mounts are read from mountinfo, never from the filesystem.
# shellcheck shell=bash

STATES=()

log() { printf '%s\n' "$*" >>"$LOG"; }
say() { printf '%s\n' "$*"; log "$*"; }
step() { say ""; say "== $*"; }
ok() { say "  ✓ $*"; STATES+=("✓ $*"); }
skip() { say "  – $*"; STATES+=("– $*"); }
bad() { say "  ✗ $*"; STATES+=("✗ $*"); }

open_log() {  # the log, emptied; a log that cannot be written goes to a temporary file instead
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    : >"$LOG" 2>/dev/null || LOG="$(mktemp)"
}

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
wait_unstuck() {  # wait up to STUCK_WAIT_S for stuck processes to end; 0 when none is left
    local waited=0
    while [ -n "$(stuck_processes)" ] && [ "$waited" -lt "$STUCK_WAIT_S" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    [ -z "$(stuck_processes)" ]
}

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
usb_id() { printf '%s:%s' "$(cat "$1/idVendor" 2>/dev/null)" "$(cat "$1/idProduct" 2>/dev/null)"; }

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

# drop_drive_mounts: unmount the brain's drive everywhere (its bind mount on DATA and the mounts under MOUNT_ROOT).
# A drive that answers is unmounted cleanly (its data written out); one that does not is unmounted lazily.
drop_drive_mounts() {
    local mnt
    for mnt in "$DATA" $(mounts_under "$MOUNT_ROOT"); do
        is_mounted "$mnt" || continue
        limited 30 umount "$mnt" || limited 10 umount -l "$mnt"
        for _ in 1 2 3 4 5; do  # stacked mounts (a bind made twice) come off one at a time
            is_mounted "$mnt" || break
            limited 10 umount -l "$mnt"
        done
    done
}

finish_log() {  # the summary; the log is copied to the home of whoever ran sudo; returns 1 when anything failed
    local failed=0 s home
    say ""
    say "== summary"
    for s in "${STATES[@]}"; do
        say "  $s"
        case "$s" in "✗"*) failed=1 ;; esac
    done
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
        home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
        if [ -n "$home" ] && cp "$LOG" "$home/$1" 2>/dev/null; then
            chown "$SUDO_USER": "$home/$1" 2>/dev/null
            say "the full log: $home/$1"
        fi
    fi
    [ "$failed" = 0 ] && say "everything worked" || say "something above is marked ✗: send the log"
    return "$failed"
}

sd_mode_on() {  # SD-card mode is switched on (scripts/sd-card-mode.sh wrote its drop-in)
    grep -qsE '^[[:space:]]*sd_only[[:space:]]*=[[:space:]]*true' "${ETC:-/etc/polymath}/polymath.d/"*.toml
}
