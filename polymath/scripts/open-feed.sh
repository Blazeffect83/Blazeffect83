#!/usr/bin/env bash
# Open the Polymath live feed in a terminal window: at desktop login (XDG autostart / labwc / wayfire) and
# from the menu. The feed waits for the agent by itself, so this never waits.
#
#   open-feed.sh            open the feed window, unless one is already open (autostart)
#   open-feed.sh --new      always open another window (menu launcher)
#   open-feed.sh --inside   what runs inside the window: the feed, and a prompt if it ever exits
set -u
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
FEED_CMD="${POLYMATH_FEED_CMD:-/opt/polymath/venv/bin/polymath feed}"
TITLE="Polymath — live feed"
COLS="${POLYMATH_FEED_COLS:-120}"
ROWS="${POLYMATH_FEED_ROWS:-36}"
FONT_SIZE="${POLYMATH_FEED_FONT_SIZE:-14}"  # larger than a normal terminal: readable from across the desk

if [ "${1:-}" = "--inside" ]; then
    printf '\033]0;%s\007' "$TITLE"  # window title, for terminals without a --title option
    # shellcheck disable=SC2086  # FEED_CMD is a command line on purpose
    $FEED_CMD
    status=$?
    printf '\nThe live feed stopped (exit status %s). Press Enter to close this window.\n' "$status"
    read -r _
    exit "$status"
fi

if [ "${1:-}" != "--new" ]; then
    LOCK="${XDG_RUNTIME_DIR:-/tmp}/polymath-feed.lock"
    exec 9>"$LOCK"
    if ! flock -n 9; then
        exit 0  # another autostart mechanism already opened it (the terminal keeps the lock while open)
    fi
fi

INSIDE="$SELF --inside"
for term in ${POLYMATH_TERMINAL:-} lxterminal x-terminal-emulator foot xfce4-terminal gnome-terminal konsole kitty \
    alacritty xterm; do
    command -v "$term" >/dev/null 2>&1 || continue
    case "$term" in
        lxterminal)
            # lxterminal has no font option: this window gets its own settings directory (written once, so
            # your changes to it stay), and your other terminals keep theirs
            conf_home="${HOME}/.config/polymath-feed"
            if [ ! -f "$conf_home/lxterminal/lxterminal.conf" ]; then
                mkdir -p "$conf_home/lxterminal"
                printf '[general]\nfontname=Monospace %s\nscrollback=5000\nhidemenubar=true\nhidescrollbar=false\n' \
                    "$FONT_SIZE" >"$conf_home/lxterminal/lxterminal.conf"
            fi
            XDG_CONFIG_HOME="$conf_home" exec lxterminal --no-remote --title="$TITLE" --geometry="${COLS}x${ROWS}" \
                -e "$INSIDE" ;;
        foot)            exec foot --title="$TITLE" --window-size-chars="${COLS}x${ROWS}" --font="monospace:size=$FONT_SIZE" "$SELF" --inside ;;
        xfce4-terminal)  exec xfce4-terminal --disable-server --title="$TITLE" --geometry="${COLS}x${ROWS}" --font="Monospace $FONT_SIZE" -x "$SELF" --inside ;;
        gnome-terminal)  exec gnome-terminal --title="$TITLE" --geometry="${COLS}x${ROWS}" -- "$SELF" --inside ;;
        konsole)         exec konsole -p tabtitle="$TITLE" -e "$SELF" --inside ;;
        kitty)           exec kitty --title "$TITLE" "$SELF" --inside ;;
        alacritty)       exec alacritty --title "$TITLE" -e "$SELF" --inside ;;
        xterm)           exec xterm -T "$TITLE" -geometry "${COLS}x${ROWS}" -fa Monospace -fs "$FONT_SIZE" -e "$SELF" --inside ;;
        *)               exec "$term" -T "$TITLE" -e "$SELF" --inside ;;  # Debian x-terminal-emulator interface
    esac
done
echo "open-feed: no terminal emulator found; run '$FEED_CMD' in any terminal" >&2
exit 1
