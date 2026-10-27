# Handoff: for the Claude session running on the Pi

You are running **on Christian's Raspberry Pi** (hostname `piS`, user `christian`), connected through Claude Code
Remote Control. An earlier session built Polymath in the cloud and could not reach this Pi; Christian had to relay
every command. You can run commands here directly. **Read this file to the end before doing anything.**

## Who and what

- **Christian** owns this Pi and Polymath. He wants answers that are short and exact, and things done completely
  (tested, documented, the caretaker notes updated). He wants it as autonomous as possible.
- **Polymath** is a from-scratch learning agent (no LLMs, Python standard library and numpy only). The code is in
  `~/polymath-src/polymath`, a clone of github.com/Blazeffect83/Blazeffect83, branch **`claude/polymath-agent`**
  (draft PR #3). It is installed to `/opt/polymath` by `sudo ./install.sh` and runs as systemd services
  `polymath` and `polymath-dashboard`, plus the timer `polymath-heal.timer` (root, every minute: drive roles, SSD
  watch, drive care).
- **The Pi:** Raspberry Pi 4 Model B 8 GB, Debian 13 trixie, 64-bit, Python 3.13, booting from an SD card
  (about 2 GB free). Two blue USB 3 ports, two black USB 2 ports. No battery-backed clock.

## Hard rules (Christian's; also in `polymath/CLAUDE.md`)

- **Never touch `~/Blazeffect83`** on the Pi. Polymath's clone is `~/polymath-src`.
- **Ask Christian before** anything destructive or that changes the system: `sudo` commands that change state,
  formatting or partitioning a drive, editing `/etc/polymath/*`, installing packages, rebooting. Read-only commands
  (`polymath doctor`, `polymath needs`, `polymath storage roles`, `lsblk`, `lsusb -t`, `journalctl`, `df`) need no
  asking.
- **Never format a drive that holds his files.**
- **Never pass `--allow-sd-card`** to `install.sh` for this Pi (see below). Plain `sudo ./install.sh`.
- The agent only learns; self-improvement changes settings, never code; Polymath must keep running whatever happens.
- Every code change follows `polymath/CLAUDE.md`: ruff, ruff format, mypy --strict, tests (≥ 85 % coverage), docs
  (README, OPERATIONS, ARCHITECTURE, VERIFICATION §12), version bump in `polymath/__init__.py` and `pyproject.toml`,
  and **`polymath/agents/faults.toml`** updated (its `[situation]` is what is true on the Pi; add a `[[history]]`
  entry for every real problem seen here). Commit with the attribution lines your harness gives you, push to
  `claude/polymath-agent`. Do not put model names in commits.
- On the Pi, tests can run from the clone: `cd ~/polymath-src/polymath && python3 -m venv /tmp/pmvenv &&
  /tmp/pmvenv/bin/pip install -q numpy pytest pytest-cov ruff mypy && /tmp/pmvenv/bin/python -m pytest -q`
  (ask first if that is too heavy while the agent runs; the cloud session ran them on every change).

## What happened (the short story)

1. The brain lived on a 1 TB SSD (DockCase enclosure). It froze at USB 2 speed; the Pi fell back to **SD-card mode**
   (brain on the SD card in `/var/lib/polymath/brain`).
2. Christian **reformatted the SSD** himself (it now holds no brain). On 2026-10-25 he installed v0.11.2 and ran
   `sudo polymath storage keep-sd-brain` → `promoted`: the SD card's brain became **the** brain, in `/srv/polymath`
   **on the SD card (not a mount)**. SD-card mode is off.
3. The feed then showed the agent **paused** ("only 1.9 GB free on the data disk"; later labelled "cooling at 50 °C",
   which was a wrong label, fixed in v0.11.4). Cause: the SD card was held to a drive's 10 GB reserve.
4. The one drive plugged in, **`EA06-698…`** (almost certainly the reformatted SSD), showed as **USB 2** and
   **exFAT**, 512 GB of room: it can't take the brain on USB 2 (refused on purpose: it froze there before), and it
   wasn't reformatted for Linux.
5. Fixes since then, all pushed, **none yet run on the Pi**:
   - **v0.11.3:** a DRIVES line in the feed says why the brain stays on the SD card. Drive care reformats a
     Windows/macOS drive with none of his files to ext4 (macOS/iPad clutter no longer stops it).
   - **v0.11.4:** the SD card keeps only a 3 GB reserve. Pauses say their real reason. The brain goes on the
     **largest** eligible drive.
   - **v0.12.0:** SMART drive health, test-restored backups, an hourly mirror, safe updates with automatic rollback,
     `polymath needs`, heat-aware scheduling, split brain, texts as pointers.
   - **v0.13.0:** a dream cycle (`polymath dream`), invented procedures, a scientist (`polymath science`),
     breeding, a red team (`polymath redteam`), distilled Wikidata, `polymath why --fact N`, curiosity, a clock
     guard.
   - **v0.13.1:** `install.sh` accepts a brain that lives on the SD card (it had stopped with "/srv/polymath is not
     a mounted filesystem").
6. **Last thing Christian saw:** that install error. He has not installed v0.13.1 yet.

## Why never `--allow-sd-card` here

With an existing brain, older install.sh versions set `disk_budget_gb` to about (free − 10) × 0.8, i.e. **~2 GB**,
and Polymath would then evict most of what it has read. v0.13.1 refuses to cut an existing budget, but the flag is
not needed: plain `sudo ./install.sh` works with a brain on the SD card.

## What to do now

1. Look before changing anything: `cd ~/polymath-src && git status && git log --oneline -3`, then
   `polymath doctor`, `polymath needs`, `polymath storage roles`, `lsblk -o NAME,SIZE,FSTYPE,LABEL,TRAN,MODEL`,
   `lsusb -t`, `df -h / /srv/polymath`, `grep -E 'require_separate_mount|disk_budget_gb' /etc/polymath/polymath.toml`.
2. **Install the latest** (tell Christian first; it restarts the services):
   `cd ~/polymath-src && git pull --ff-only origin claude/polymath-agent && cd polymath && sudo ./install.sh`
   It should say "the brain lives on the SD card for now". If it still stops, show the output and the
   `require_separate_mount` line from step 1.
3. **The SSD:** if `lsusb -t` shows it under the 480M bus, ask Christian to move it to a **blue USB 3 port**
   (USB 3 cable, no USB 2 hub). That is the one thing you cannot do.
4. Then wait and watch: drive care reformats the empty exFAT SSD to ext4 about 10 minutes after it settles (only
   if it holds none of his files; `polymath storage roles` and the feed say what it is doing), and the drive roles
   then move the brain onto it. `polymath storage roles` should end with the SSD as **brain**, `polymath needs` with
   "nothing needs you", `polymath doctor` clean. If drive care says the drive holds files, show Christian the list:
   never erase his files.
5. If anything fails: read `journalctl -u polymath -n 80`, `journalctl -u polymath-heal -n 80`, find the cause in
   the code, fix it properly (with tests and the faults.toml update), push, `sudo ./install.sh` again (ask first).
6. When it works: update `polymath/agents/faults.toml` `[situation]` (what is true now), close the `[[history]]`
   entries confirmed fixed (`open = false`), update the PR #3 description's "On the Pi" section, commit and push.

## Useful facts

- Logs: `journalctl -u polymath`, `-u polymath-dashboard`, `-u polymath-heal`. Live feed: `polymath feed`.
- Drive roles record: `/run/polymath/drive-roles.json`. SSD watch: `/var/lib/polymath/ssd-watch.json`.
- The brain's database: `/srv/polymath/db/polymath.sqlite3` (owned by user `polymath`).
- Config: `/etc/polymath/polymath.toml` (never overwritten by install.sh), drop-ins in `/etc/polymath/polymath.d/`.
- Caretaker's knowledge of every known problem and its steps: `polymath/agents/faults.toml`; `polymath caretaker`.
- Docs: `polymath/docs/OPERATIONS.md` (how to run it, troubleshooting table), `ARCHITECTURE.md`, `VERIFICATION.md`.
