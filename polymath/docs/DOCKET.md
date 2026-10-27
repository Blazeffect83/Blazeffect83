# The docket: everything about Polymath, from the start to now

Written 2026-10-27 by the cloud session that built Polymath, for the Claude session running on Christian's Pi
(Remote Control). `docs/HANDOFF.md` says what to do; this says **everything that happened**, so nothing has to
be retold. The caretaker's own records hold the same story in machine-readable form: `polymath/agents/faults.toml`
(`[[update]]` per version, `[[history]]` per real Pi problem, `[situation]` for now).

---

## 1. The people, the machine, the rules

- **Christian** (user `christian`) owns the Pi and the project. He wants:
  - things done completely, tested and documented, "so well I'm genuinely impressed";
  - answers that are short, quick and exact;
  - maximum autonomy ("the more autonomous, the better").

  He relays between sessions and wants to stop being the middleman.
- **The Pi:**
  - Raspberry Pi 4 Model B 8 GB, hostname `piS`, Debian 13 trixie 64-bit, Python 3.13.
  - Boots from an SD card (59 GB card, about 2 GB free at the worst point).
  - Two blue USB 3 ports and two black USB 2 ports. No battery-backed clock. Small touchscreen with the live feed at login.
- **Repository:** github.com/Blazeffect83/Blazeffect83, branch `claude/polymath-agent`, draft **PR #3**.
  - Polymath is in `polymath/`.
  - The rest of the repo is an older video-editing app (April 2026), untouched.
- **On the Pi:**
  - Polymath's clone is `~/polymath-src`.
  - **`~/Blazeffect83` must never be touched** (it once sat on another branch, `claude/aegis-agent`, which is why updates "didn't install" on Oct 14).
- **Hard rules** (Christian's, in `polymath/CLAUDE.md`):
  - No LLMs, external AI APIs or pretrained models: standard library and numpy only.
  - Never overwrite existing work.
  - Ask before anything destructive, privileged, external, or that widens permissions.
  - Never format a drive that holds his files.
  - The agent only learns: it never runs commands, never touches other devices or sites, and vets every site before reading it.
  - Self-improvement changes settings, never code.
  - Polymath must keep working whatever happens: every look at the brain's drive goes through `core/fsprobe` with a time limit, and every failure path reports through `core/issues`.
  - Every update bumps `polymath/__init__.py` and `pyproject.toml` together.
  - Every update updates `polymath/agents/faults.toml`: meta, `[[update]]`, `[situation]`, a `[[fault]]` per new issue code, and `[[history]]` per real Pi problem (`open = true` until confirmed fixed on the Pi).
  - The quality bar: ruff, ruff format, `mypy --strict polymath`, pytest at ≥ 85 % coverage (it has been 94 %), and docs (README, OPERATIONS, ARCHITECTURE, VERIFICATION §12 entry).
  - Never claim something was tested or deployed when it wasn't.
  - No model names in commits.

## 2. What Polymath is

An autonomous learning agent that runs 24/7 under systemd and learns only from data it collects itself.

- **Sources:** Wikipedia and Wikidata dumps, OpenAlex, PubMed, Gutenberg, StackExchange, RSS feeds, and vetted open-web sites.
- **What it builds:** a knowledge graph with a source on every fact, text indexing, embeddings it trains itself, rule learning, inference, contradiction handling and self-quizzing.
- **Interfaces:**
  - an agent society;
  - Orin the caretaker;
  - a live terminal feed;
  - a dashboard: loopback without sign-in, plus HTTPS with pairing for the home network;
  - the `polymath` CLI.
- **Runs as:**
  - services `polymath` (the agent) and `polymath-dashboard`, as user `polymath`;
  - `polymath-heal.timer`, as root every minute: drive helper, SSD watch, drive roles, drive care;
  - `polymath-volume@<dev>` units started by udev for each plugged-in drive.
- **Where things live:**
  - code in `/opt/polymath/src`, venv in `/opt/polymath/venv`, `build.json` is the installed build;
  - configuration in `/etc/polymath/polymath.toml` (never overwritten by install.sh), drop-ins in `/etc/polymath/polymath.d/`;
  - the brain in `/srv/polymath` (database `db/polymath.sqlite3`);
  - drives mounted under `/mnt/polymath/<id>`.

## 3. The whole timeline

### Building it (Oct 9–11)
- **v0.1 (phases 1–11):**
  - agent loop, durable scheduler, sd_notify, configuration, JSON logs;
  - senses (dumps, feeds, crawler), memory, perception, embeddings, reasoning, drive (curiosity, bandit);
  - self-evaluation (held-out quiz), answering, CLI, dashboard;
  - body guard (heat, disk), backups, eviction;
  - systemd units and the installer.
- **Agent society:** agents spawned from plain-English directives, with verified rewards, scopes, skills and evolution. A trial ended 39/44 verified correct with no false penalties.
- **v0.2–v0.3:**
  - live feed at login (the Minecraft player check was dropped);
  - SD-card install mode;
  - storage pool (plugged-in drives become brain space);
  - vetted open-web learning, a daily digest, learning from mistakes;
  - SD card wear guard;
  - "tell me about X" paragraphs, predictions scoreboard, "did you know", weekly recap;
  - dashboard face, knowledge map with time-lapse, and a `/live` page for phones.
- **v0.4:** it improves itself (self-tuning with rollback, rule track record, auto-spawned specialists, reading strategy, learned phrasing). Formats a fresh shop SSD once by itself. Feed faces and a 12-hour clock; fits the touchscreen.
- **v0.5.0:** it reasons, not just looks up:
  - time awareness and impossible-fact detection;
  - multi-step questions, number sense and geography, cause and effect, categories, tables, a second language, how-to;
  - duplicate merging and big-brain mode.

### The SSD saga (Oct 11–15)
The brain lived on a **1 TB SSD in a DockCase enclosure**, bus-powered from the Pi. The fixes came one Pi problem at a time:
- **v0.5.1:** the dashboard can't freeze the feed; offline mode.
- **v0.5.2:** a missing brain drive never makes everything go dark; the `core/issues` inbox.
- **v0.5.3:** names a USB drive that answers but reports 0 bytes (`sda 0B usb`).
- **v0.5.4:** replaces a dead brain mount (Input/output error).
- **v0.5.5:** brings the drive back by itself: `polymath-heal.timer`, USB reset, and a power cut with uhubctl. Also: "dpkg was interrupted" blocked apt.
- **v0.5.6:** heal restarts `polymath-volume@*`.
- **v0.5.7:** a frozen drive never freezes Polymath (`core/fsprobe` time limits).
- **v0.6.0:** a standby brain on the SD card while the drive is gone, folded back later.
- **v0.6.1:** the caretaker knowledge base (`faults.toml`).
- **v0.6.2:** from the Pi itself:
  - root cause: **the SSD linked at USB 2 (480M) and froze** (processes stuck in state D, "database is locked");
  - O_DIRECT probes.
- **v0.6.3:** `scripts/rescue.sh`, one-command recovery plus install with a log.
- **v0.7.0, SD-card mode** (Christian: "still not working"; run everything off the SD card until the SSD is solved):
  - `scripts/sd-card-mode.sh on|off|status`;
  - the brain started from the SD card's old 3.6 GB copy in `/var/lib/polymath/brain`.

  The SSD work was put **on hold** at his request.

### Running on the SD card (Oct 15–25)
- **v0.8.0:** Orin the caretaker: incidents, Bayesian causes, bounded remedies, escalation with advice.
- **v0.8.1:** the feed shows what it is learning and how far along.
- **v0.8.2:** "goes offline for a few minutes after learning". Single steps ran for minutes; the vigil now keeps proof of life during long steps, and search is faster.
- **v0.9.0:** private access from the PC's browser (home network only, HTTPS and pairing), a security audit, and storage that lasts (forecast, leaner storage).

### Getting the SSD back (Oct 25–27): the current saga
1. **Oct 25:** Christian plugged the SSD in again; it was not picked up (SD-card mode ignores drives by design).
   - **v0.10.0, the SSD watch:** moves back onto the SSD by itself once it is healthy at USB 3 for 10 minutes, and falls back to the SD card by itself if it fails again.
2. He wants 4 USB drives managed automatically: which holds the brain, which holds backups, which are extra, and what each one holds, so a lost drive is relearned by priority.
   - **v0.11.0:** drive roles, a per-drive inventory, and relearning.
3. **v0.11.1:** `polymath doctor` crashed (PermissionError on `/etc/polymath/tls/server.crt`). Fixed by asking the dashboard's loopback listener instead; no permissions widened.
4. **The feed said** "a drive is plugged in, but it does not hold Polymath's brain". Christian had **reformatted the SSD** (on an iPad/Mac, as exFAT: "ext4 is not an option").
   - **v0.11.2:** a wiped brain drive is handled: promote the SD card's brain, or rebuild from a backup. `sudo polymath storage keep-sd-brain`. Drive care reformats empty Windows/macOS drives.
   - **Christian ran it, Oct 25 ~23:51:** `promoted` (2 min 37 s). The SD card's brain is now **the** brain, in `/srv/polymath` **on the SD card (not a mount)**. SD-card mode is off. The HTTPS cert was renewed for a new IPv6 address.
5. **The feed then showed** "paused — only 1.9 GB free on the data disk". The only drive, **`EA06-698…`**, showed as a **USB 2 hard disk, exFAT, 512 GB of room**; it is almost certainly the reformatted SSD on a black USB 2 port. It was made the backup drive, could not take the brain (USB 2 is refused on purpose: it froze there), and was not reformatted.
   - **v0.11.3:**
     - a DRIVES line in the feed says why the brain stays on the SD card;
     - drive care ignores macOS/iPad clutter, keeps data off a drive it will reformat, and drops Polymath's own data on it only while the brain is stuck on the SD card;
     - a reformatted drive takes the brain at once.
6. **"It keeps pausing for cooling at 50 °C":** not heat (the thermal pause is at 82 °C).
   - With SD-card mode ended, the SD card was held to a drive's 10 GB reserve: pause below 5 GB, then flapping. The feed fell back to the label "cooling down".
   - **v0.11.4:**
     - a brain on the SD card keeps a 3 GB reserve;
     - a disk pause holds until 2.2 GB are free;
     - pauses show their real reason;
     - Christian asked: **the brain goes on the largest eligible drive** (`storage.brain_prefer = "largest"`).
7. **v0.12.0**, eight ideas Christian approved:
   - split brain (`brain_prefer = "split"`);
   - SMART drive health (smartmontools; a failing drive loses the brain at once);
   - every backup test-restored;
   - safe updates (the previous version is kept in `/opt/polymath/previous`; `scripts/update-check.sh` rolls back after 10 minutes if the agent crash-loops);
   - `polymath needs` (what only Christian can do);
   - an hourly incremental mirror of the database onto the backup drive;
   - texts kept as pointers and read again when there is room;
   - heat-aware scheduling.
8. **v0.13.0**, Christian's own "game changer" list:
   - a dream cycle (`polymath dream`);
   - agents invent procedures (a sandboxed interpreter, data not code);
   - a scientist (`polymath science`, held-out binomial tests);
   - a breeding society (crossover, preferences as genes);
   - a red team (`polymath redteam`; it found the inbox accepting any job at any priority, now clamped);
   - distilled Wikidata;
   - `polymath why --fact N` (provenance now records job and agent);
   - a curiosity engine;
   - a clock guard (after a power cut without internet).
9. **The install of v0.13.0 failed:** "/srv/polymath is not a mounted filesystem… or pass --allow-sd-card".
   - **v0.13.1:** install.sh accepts a brain that lives on the SD card. **Never use `--allow-sd-card` on this Pi:** it would have cut the brain's budget to about 2 GB and evicted most of what it read.
10. **Oct 27:** Christian connected the Pi through **Claude Code Remote Control**, and the Pi session (you) is working.

## 4. What the Pi session has found so far (relayed by Christian)

- `/srv/polymath/backups` held **6 half-finished backup copies** from Oct 15 (`.polymath-20261015-*.sqlite3`, 3.56 GB each, about 21 GB). They were left when the backup job was killed mid-check and never passed their check.
- The only finished backup was a **12 KB** one from Oct 10: effectively no backup.
- This filled the SD card and caused an **eviction crash loop**.
- **Advice given to Christian:** "Keep newest, delete 5". That frees about 18 GB, which is plenty, and keeps the newest copy (likely complete, cut off during its check) as the only insurance. **Delete that last copy once a new backup has passed its test restore** (`body.verify_backup`, v0.12.0).
- The Pi session mentioned a code fix that sweeps such leftovers. Do it properly, following §1:
  - remove stale `.polymath-*.sqlite3` scratch copies left by a killed backup;
  - handle the eviction crash;
  - add tests, update faults.toml (a `[[history]]` entry for the leftover backups and the crash loop), bump the version, commit and push.
- Whatever else you find: keep `faults.toml [situation]` true to what is on the Pi.

## 5. Where things stand right now (what is true, what is not yet)

- **Installed on the Pi:** at least v0.11.2. v0.11.3 → v0.13.1 are pushed but were **not yet confirmed installed** when this was written. Check `cat /opt/polymath/build.json`.
- **The brain:** `/srv/polymath` on the SD card. Config should have `require_separate_mount = false`.
- **The SSD:** reformatted exFAT, probably on a black USB 2 port. It needs Christian to move it to a **blue USB 3 port**; drive care then makes it ext4 and the brain moves onto it.
- **Optional package:** `smartmontools` is installed by install.sh when apt works. If apt says "dpkg was interrupted": `sudo dpkg --configure -a` (ask first).
- **Never confirmed on the Pi:** anything from v0.11.3 on. VERIFICATION.md says so honestly; update it with what you actually see.

## 6. The goals

1. **Now:**
   - the latest version installed;
   - the SD card has room again;
   - the SSD on USB 3 and formatted ext4;
   - the brain moved onto it (`polymath storage roles` shows brain on it);
   - `polymath doctor` clean and `polymath needs` saying "nothing needs you";
   - a fresh backup that passed its test restore.
2. **Then:**
   - more drives across the 4 ports, chosen by Polymath itself (the largest eligible drive is the brain, backups on another physical disk, the rest is extra room);
   - lost drives relearned by priority.
3. **Always:**
   - fully autonomous and fully offline-capable;
   - never down;
   - learning and improving itself (dreams, scientist, curiosity, the society), with every decision explainable;
   - the red team hardening its defenses.
4. **Process:** Christian should not be the middleman. Keep him informed in short, exact messages; ask only what only he can decide or do.

## 7. Commands worth knowing

- Look:
  - `polymath doctor`, `polymath needs`, `polymath storage roles`, `polymath storage list`, `polymath storage usage --detail`;
  - `polymath caretaker`, `polymath feed`, `polymath backup --list`;
  - `polymath dream`, `polymath science`, `polymath redteam`, `polymath why --fact N`;
  - `cat /opt/polymath/build.json`, `lsblk -o NAME,SIZE,FSTYPE,LABEL,TRAN,MODEL`, `lsusb -t`, `df -h / /srv/polymath`.
- Logs: `journalctl -u polymath -n 80`, `-u polymath-dashboard`, `-u polymath-heal`, `-u polymath-update-check`.
- Install (ask first): `cd ~/polymath-src && git pull --ff-only origin claude/polymath-agent && cd polymath && sudo ./install.sh`.
- Brain on the SD card by hand (already done): `sudo polymath storage keep-sd-brain`.
- Format a drive by hand (ask first; never one with his files): `sudo polymath storage format <id> --yes`.
