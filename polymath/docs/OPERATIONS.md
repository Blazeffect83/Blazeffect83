# Operations

## <a id="nvme"></a>1. Prepare the NVMe drive (once), or start on the SD card

Polymath prefers to keep its data off the SD card: a filesystem mounted at `/srv/polymath`.

**No drive yet?** `sudo ./install.sh --allow-sd-card` keeps the brain on the SD card for now:
- the budget is sized to the card (at most 15 GB, leaving 10 GB free);
- any drive you plug in later is added to the brain automatically (§8).

Constant database writes wear SD cards, so add a USB SSD when you can.

> **Warning:** `mkfs` erases the drive. Check the device name with `lsblk` first.

```bash
lsblk                                         # find the NVMe disk, e.g. /dev/nvme0n1
sudo mkfs.ext4 -L polymath /dev/nvme0n1p1     # ERASES the partition — only on an empty drive
sudo mkdir -p /srv/polymath
echo 'LABEL=polymath /srv/polymath ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a && findmnt /srv/polymath
```

If the drive already has a filesystem you want to keep, mount that one instead. A bind mount of a directory
on the NVMe drive also works.

## 2. Install, upgrade, uninstall

```bash
sudo ./install.sh              # first install and every upgrade (idempotent: safe to repeat)
sudo ./install.sh --allow-sd-card   # no NVMe yet: keep the brain on the SD card, add drives later
sudo ./install.sh --dry-run    # show what it would do
sudo ./uninstall.sh            # remove services and code; KEEPS /srv/polymath and /etc/polymath
sudo ./uninstall.sh --purge --yes   # also delete everything it learned, and its user
```

`install.sh` does the following:
- checks the mount;
- creates the `polymath` system user;
- installs `python3-venv`, `curl`, `fdisk` and `e2fsprogs` if missing;
- copies the code to `/opt/polymath/src` and builds `/opt/polymath/venv` (numpy only);
- records the build (version, git commit, commit date) in `/opt/polymath/build.json`. A new commit restarts the
  agent so that it runs, and reports, the installed build;
- writes `/etc/polymath/polymath.toml` (never overwritten once it exists);
- installs `polymath.service`, `polymath-dashboard.service` and a journald size limit;
- enables desktop autologin (`raspi-config nonint do_boot_behaviour B4`);
- adds the live feed terminal to the desktop user's autostart (XDG, labwc and wayfire), plus menu entries for the
  live feed and the dashboard. An upgrade replaces the dashboard autostart of earlier versions;
- installs the storage pool: a udev rule, `polymath-volume@.service` and `/mnt/polymath`, then adopts drives that
  are already plugged in (§8);
- comments out the `minecraft_*` settings of earlier versions in an existing configuration (backup:
  `polymath.toml.bak`). Left in place, they would be ignored with a warning;
- (v0.9.0) makes root the owner of the installed code, so no service runs a file another user could change;
- (v0.9.0) moves an old `host = "0.0.0.0"` under `[dashboard]` to `127.0.0.1` (backup: `polymath.toml.bak`): the
  sign-in-free listener stays on the Pi, other devices use HTTPS with pairing;
- (v0.9.0) makes the dashboard's HTTPS certificate (`scripts/access-cert.sh`, below);
- starts everything and waits for `/health`, then prints a **pairing code** for your PC's browser.

## 3. Everyday

| | |
|---|---|
| live feed | opens in a terminal at login · `polymath feed` in any terminal · menu: *Polymath live feed* |
| dashboard | on the Pi: `http://localhost:8765/` · menu: *Polymath dashboard* · your PC: `https://<pi>:8443/` after pairing ([below](#browser)) |
| who can see it | `polymath access` (status) · `pair` (a code for a new browser) · `devices` · `revoke <id>` (or `all`) · `log` |
| where the room goes | `polymath storage usage` (database, backups, indexes, and when the disk would reach its reserve) · `--detail` splits the database (slow: reads all of it) |
| is everything working? | `polymath doctor`: brain, agent, internet, dashboard and disk, each with the fix if broken |
| status | `polymath status` · `systemctl status polymath polymath-dashboard` |
| logs | `journalctl -u polymath -f` (JSON lines; `-o cat` for compact) |
| nightly report | `/srv/polymath/reports/report-YYYY-MM-DD.md` or `polymath report` |
| research a topic | `polymath learn "topic"` or `polymath learn https://example.org/` |
| what it learned today | `polymath digest` (written daily at 07:00; also in the feed and on the dashboard) |
| new sites it vetted | `polymath sites` · `polymath sites --check example.org` |
| the week in review | `polymath recap` (Sundays at 08:00; `--now` for this week so far) |
| tell me about … | `polymath tell "topic"`, or ask "Tell me about …" on the dashboard |
| its predictions | `polymath predictions` (guessed before reading; confirmed, wrong, open) |
| SD card wear | `polymath wear` |
| on your phone | `https://<pi>:8443/live` (pair it like the PC): the face and the live feed |
| knowledge map | on the dashboard: topics clustered by what they share, sized by documents. ▶ plays the time-lapse; click a topic to ask about it |
| ask harder questions | `polymath ask "Is Brazil bigger than Angola?"` · "How far is Paris from Berlin?" · "Who was alive at the same time as Mozart?" · "What causes malaria?" · "Can penguins fly?" · "How do I …?" · "Who is the head of government of the capital of France?" |
| what it changed about itself | `polymath changes` (`--all` adds trials that changed nothing; `--json`), the dashboard panel *How it improved itself*, "improved" lines in the feed |
| overrule a tuned setting | `polymath changes --reset NAME` (or `all`): back to the default and pinned · `polymath changes --allow NAME` (or `all`) lets it tune again |

### <a id="browser"></a>Your PC's browser (v0.9.0)

Polymath on your PC, in Chrome, while you work: only on your home network, and only for browsers you paired.

**Pair once (2 minutes, no need to touch the Pi):**
1. The install ends with a box like this (or run `polymath access pair` on the Pi, or over `ssh`):

   ```text
     Open Polymath on your PC (Chrome), on your home network:
         https://192.168.1.50:8443/
         https://pis.local:8443/
     Pairing code:   K7QF-3MXA      (works once, for 15 minutes)
   ```
2. Open the address on your PC. Chrome first says *Your connection is not private*: the Pi made its own
   certificate, which Chrome does not know yet. Choose **Advanced → Proceed to 192.168.1.50 (unsafe)**. The
   connection is still encrypted.
3. Type the code and a name for the device. That browser is now paired for 400 days; everything else still
   needs no password.
4. Optional, to remove the warning for good: on the pairing page, **Download the Pi's certificate** (`ca.crt`),
   open it → *Install Certificate* → *Current User* → *Place all certificates in the following store* →
   **Trusted Root Certification Authorities** → Finish. Check that the thumbprint Windows shows matches the one
   on the Pi's screen (`polymath access`). Restart Chrome. The certificate is made on the Pi, its key never leaves
   it, and it can only vouch for this Pi's own names.
5. Optional, its own window: Chrome menu ⋮ → *Cast, save and share* → **Install page as app** (or the install
   icon in the address bar). It opens like a program, from the Start menu or the taskbar.

A paired phone or tablet works the same way (`/live` is the face and the feed). A paired browser can make a code
for the next device from the dashboard's *Who can see this* card, so the Pi itself is never needed.

**What keeps everyone else out:**
- **Home network only.** The dashboard refuses any address outside the private ranges (`192.168.x.x`, `10.x.x.x`,
  `172.16–31.x.x`, IPv6 local), and systemd drops such packets before the program sees them (`IPAddressAllow`).
  Your router does not forward port 8443, so nothing on the internet reaches it anyway.
- **Paired browsers only.** Pairing gives that browser a random 256-bit key, kept in a cookie scripts cannot read
  (`HttpOnly`, `Secure`, `SameSite=Strict`). The Pi stores only a hash of it, so even a copy of its files opens
  nothing. A code works once, for 15 minutes; five wrong guesses cancel it and ten attempts from one computer in
  ten minutes are refused.
- **Encrypted.** HTTPS (TLS 1.2 or newer) with the Pi's own certificate; plain HTTP on the port gets nothing.
- **No tricks from other sites.** Requests must name the Pi itself (no DNS rebinding), changes must come from the
  dashboard's own pages (`Origin` checked, JSON only), and the pages forbid framing and outside scripts.
- **You see and control it.** The *Who can see this* card and `polymath access devices` list every paired device
  with when and from where it was last seen; *revoke* or `polymath access revoke <id>` (or `all`) locks it out at
  once. `polymath access log` lists pairings, removals and every refused attempt.
- **The sign-in-free page stays on the Pi.** `http://localhost:8765/` (the menu entry, the live feed, the CLI)
  listens only on the Pi itself. `http://<pi>:8765/` from another device no longer works, on purpose.

**If the Pi's address changes** (a new one from the router), the certificate no longer names it: `polymath doctor`
says so, and `sudo /opt/polymath/src/scripts/access-cert.sh && sudo systemctl restart polymath-dashboard` renews
it (same authority, so a PC that trusts it keeps trusting it). Better: give the Pi a fixed address in the router
(a *DHCP reservation*), or use `https://<name>.local:8443/`.

Settings (`[access]`): `remote` (true; false: only the Pi itself), `port` (8443), `networks` (the private ranges),
`names` (extra host names to answer to). The certificate script: `sudo ./scripts/access-cert.sh` (make or renew),
`--force` (a new server certificate), `--status` (no sudo).

### How it improves itself

It never edits its code. What it changes, each change logged with the numbers behind it:

- **Settings**: every 8 hours it tests one setting against half/double (or a grid of) alternatives on the same
  questions and adopts a clearly better value. A new value is *watching* until two self-tests have run; if
  accuracy falls by more than 5 points it is undone by itself. Settings: `link.subjects`, `link.features`,
  `predict.min_confidence`, `infer.min_confidence`.
- **Rules**: every 6 hours each learned rule is checked against what sources later said. Rules that keep
  being contradicted are dropped with the facts only they produced; contradicted conclusions are withdrawn and
  never re-derived.
- **Specialists**: a relation it keeps getting wrong on the self-test (for example "fix birth place") gets its
  own *predict* agent (`polymath agents list` shows them, marked auto). They retire when the relation recovers,
  or after 21 days.
- **Reading**: daily it reads more of the sources and topics that teach it the most per CPU-minute (weights
  0.5–2, shown on the dashboard).
- **Writing**: it learns sentence phrasings from what it reads ("X was born in Y") and `tell` uses them.

The CLI reads the same configuration as the service (`/etc/polymath/polymath.toml`). `install.sh` puts
`polymath` on everyone's PATH (`/usr/local/bin/polymath`):
- most commands run as the `polymath` user, which owns the data, through `sudo`;
- `polymath feed` runs as you;
- `storage attach|detach|eject` run as root.

### The live feed

`polymath feed` shows what the agent is learning, as it happens:

| tag | what |
|---|---|
| `read` | a document arrived: Wikipedia article, paper, book, feed item, crawled page |
| `fact` / `disputed` | a fact it learned (from Wikidata, an infobox, or read in a sentence, quoted); disputed when sources disagree |
| `inferred` | a fact it derived with a learned rule (inverse, transitive, symmetric) |
| `reason` | rules learned (with examples), facts inferred, contradictions checked, how much it trusts each source |
| `quiz` | a self-test on a hidden fact: ✓ / ✗ with the truth, then the score against chance |
| `agent` | an agent's verified reward or penalty and what it was for; new and evolved agents |
| `curious` | the topics it most wants to learn about now |
| `you` | what you asked it to learn |
| `body` | throttling, pausing, back to normal, agent offline/online |
| `site` / `safety` | a new site approved, put on probation, refused (and why), or dropped; a safety list loaded |
| `relearn` / `fixed ✓` | reading up on a wrong self-test answer; getting it right on the re-test |
| `digest` | the daily "what I learned today" summary, line by line |
| `report` / `backup` / `error` | nightly report, backups, failed job slices (with retry or give-up) |

Busy streams are capped per refresh (for example 5 facts and 4 documents every 1.5 s), and the rest are counted
(`+4,312 more facts`), so a Wikidata ingest stays readable. The pinned header shows:
- the state, and the version top right;
- **a NOW line** with what it is doing right now, highlighted (cyan; yellow when paused; red when not running);
- **two LEARNING lines** (v0.8.1): what it is learning and how far along it is (below);
- its knowledge counts, the latest quiz score and the CPU temperature.

**What it is learning, and how far along.** Jobs run in short slices that take turns, so NOW changes every few
seconds. The LEARNING lines follow the reading round underneath and stay on it while other work takes a turn:

```
 LEARNING   Photosynthesis  · curious  · just read “Stoma”
  knows █████████░░░░░ 62% (+25%) — read about 4 of 6 things it mentions   this round ███████░░░░░░░ 2 of 4 articles
```

- **The subject:** the topic curiosity pursues, what you asked for (`polymath learn …`), the wrong answer it reads
  up on, an agent's subject, or the dump part or file it works through.
- **Who chose it:** `curious`, `you asked`, `fixing a wrong answer`, `agent <name>`, or nothing (its reading plan).
- **What it read last:** the newest article of the round (for Wikidata, the newest thing it learned a fact about).
- **knows:** how well it knows the topic, the same measure curiosity ranks topics by. Once it has been quizzed on
  the topic, it is the share of right answers. Before that, it is the share of the things the topic's articles
  mention that it has read about too. `(+25%)` is what this round has added so far. It updates every 30 seconds
  for topics of up to 400 articles, and hourly for bigger ones.
- **this round:** the articles of the batch it has read, the share of a dump file, or the books of a list. `✓` and
  `LEARNED` when the round is finished.
- **Between topics** (indexing, reasoning, training): the last subject, dimmed, and the next topics curiosity wants.

The feed also gets a line when a round starts (`learning  started on Photosynthesis (curious) — knows 38% so far`)
and when it finishes (`learned  finished a round on Photosynthesis (curious) · 4 of 4 articles · knows 38% → 62%`),
and the face is proud after a round that taught it something. The dashboard and the `/live` page show the same
in a LEARNING card under the NOW banner, with the two bars.

The feed window uses a larger font (14 pt) than other terminals. Change it with `POLYMATH_FEED_FONT_SIZE`, or
edit `~/.config/polymath-feed/lxterminal/lxterminal.conf` (your other terminals are not affected). The dashboard
and the `/live` page show the same NOW line as a large banner.

**On a small screen** (under 100 columns, like a Pi touchscreen) the feed packs itself:
- the header drops the cycle number and the date, and the counts use short numbers (`97.6k docs`). Whatever
  still does not fit is left out, least important first: the inferred/disputed detail, agents, rules, then the
  temperature (unless the Pi is hot);
- times leave out the seconds (` 9:04 PM`);
- messages wrap under their text instead of being cut off. Long runs of article titles are still cut short.

![The feed on a 72-column screen](img/feed-small-screen.png)

Times use a 12-hour clock (` 9:04:10 PM`), the same in the terminal and on `/live`. For a 24-hour clock, set
`POLYMATH_CLOCK=24` in the environment of the feed (and of the dashboard service, for `/live`).

**Version, top right.** `v0.2.0 · ab12cd3 · 2026-10-10 ✓` is the installed version, git commit and commit date;
`✓` means the running agent was started from exactly that build. `↻ agent still on v0.1.0` means an update is
installed but the agent has not restarted into it yet (`install.sh` restarts it; `sudo systemctl restart
polymath` does too). After an update the feed window reloads itself into the new code within 30 seconds, with
the line `updated to v… : reloading the feed`. `polymath version` prints the same check. It compares the Pi with
itself: it does not contact GitHub to look for newer commits.

**The face, top left.** A small animated character shows what it is doing, so the window is worth a glance:

![The face's moods](img/feed-faces.png)

| face | when |
|---|---|
| `[◐‿◐]` ⇄ `[◑‿◑]` | reading (eyes run along the line) |
| `[◔_◔]...` | reasoning, planning, going over mistakes |
| `[◕‿◕]◜` | training its models, downloading (spinner) |
| `[≖_≖]?` | vetting a new site |
| `[•_•]?` | quizzing itself |
| `[◕ω◕]···` | agents at work |
| `[•‿•]✎` | writing the digest or the nightly report |
| `[-‿-]zZ` | waiting for work (`[˘‿˘]zZ` at night) |
| `[^‿^]✧` ⇄ `[^ᵕ^]✦` | a right answer, a better quiz score, a site approved, a new agent, the digest |
| `[◕‿◕]!` | worked out a new fact by reasoning |
| `[⌐■_■]✧` | fixed an earlier mistake, a prediction came true, an improvement to itself |
| `[◕‿◕]↑↑` | improving itself (tuning, checking its rules, its reading strategy) |
| `[ᵒ_ᵒ]!` / `[¬_¬]?` / `[×_×]!` | a wrong answer / a contradiction or refused site / a failed job |
| `[^‿^]★` | a milestone (1, 2, 5 × 10ⁿ facts, documents or rules; also a feed line) |
| `[◉‿◉]▣✧` | a drive was plugged in and added to the brain (wide-eyed) |
| `[^ω^]⌂✦` | the whole brain moved into its new home on the SSD (also when that drive comes back) |
| `[•‿•]→▣` / `[•‿•]▣→` | moving documents onto a drive / bringing them back |
| `[ᵔ‿ᵔ]↓▣` | the nightly backup, landing on a drive |
| `[ᵔ‿ᵔ]▣♪` | a drive came back |
| `[◐_◐]□?` | a drive was unplugged: looking around for it |
| `[^_^]▣/` | a drive was retired: waving goodbye |
| `[>_<]` / `[-_-]` / `○[×_×]` | running hot / paused / agent offline |

The spark on its head (`✦`/`✧`) pulses while the agent is alive and it blinks every few seconds. There is no
language model and no randomness: the face is a function of the agent's state, the last few events and the clock.

The feed reads from the dashboard service (`/api/feed`, localhost), so the desktop user needs no access to
`/srv/polymath`. Options: `--plain` (no pinned header, for logs or `ssh`), `--no-color`, `--once`,
`--interval 3`, `--url http://127.0.0.1:8765` (since v0.9.0 the sign-in-free listener answers only on the Pi; other
machines use the browser, [below](#browser)), `--direct` (read the database
directly, as a user who can). To stop the window opening at login, delete
`~/.config/autostart/polymath-feed.desktop` and the `open-feed.sh` lines in `~/.config/labwc/autostart` and
`~/.config/wayfire.ini`.

## 4. How it keeps the Pi healthy

| condition | mode | effect |
|---|---|---|
| CPU ≥ 75 °C (until < 72 °C) | throttle | light jobs only, half-length slices |
| CPU ≥ 82 °C (until < 77 °C) | pause | nothing runs |
| free disk < `disk_min_free_gb` | yield | light jobs only, half-length slices; eviction requested |
| free disk < half of that | pause | only eviction runs |
| data > `disk_budget_gb` | — | eviction requested |

**Busy is not offline** (v0.8.2). The agent shows it is alive in three ways:
- a heartbeat when a step commits;
- a pulse whenever a job reports progress;
- a ping to systemd's watchdog, which restarts it after 2 minutes without one.

A single long step (the nightly backup, a quiz, a big query on the SD card) used to leave all three silent. The
feed then said `agent offline`, and after 2 minutes systemd restarted it. The **vigil** is a small thread that
vouches for the agent every 10 s while the agent makes progress, and the feed shows the step as
`(one long step: 4 min so far — busy, not stuck)`. After 15 minutes without any progress the vigil stops, so a
really stuck agent is still restarted. Steps that went a minute without a sign of life, or kept the agent busy for
5 minutes, are reported once a day: `polymath caretaker` shows `jobs/slow:<kind>`, and `polymath status` shows
`slow_steps`.

These limits always apply, whatever the mode:
- `CPUQuota=200%` (2 of 4 cores), `MemoryMax=3G`, `Nice=10`, idle-ish IO priority;
- numpy is limited to 2 threads;
- the dashboard is limited to 25 % CPU and 1.2 GB (400 MB was too tight for the ask box on a big brain).

### <a id="orin"></a>Orin the Caretaker (v0.8.0)

Orin keeps Polymath and the Pi running smoothly and safely. It is a job of its own, `caretaker.step`, that runs every
two minutes ahead of everything else, and still runs while the body guard pauses the agent. One turn takes well under
a second.

**What it watches:**
- **The Pi:** the CPU temperature, free space, the agent's memory, and power (the firmware's under-voltage flags).
- **The database:** the write-ahead log, and the age of the last good backup.
- **Learning:** the internet, documents read per hour, and self-test accuracy.
- **Jobs:** jobs that fail again and again, and jobs that gave up.
- **The safety lists:** their age.
- **Every reported problem:** the problem inbox, `core/issues`.
- **Drives:** the brain drive's USB speed (not in SD-card mode).
- **The install:** the installed build against the running one, changes to the configuration, and the brain folder's
  permissions.

**How it decides:**
- **What is normal.** It learns this from a week of its own readings (median ± k·MAD, k = 4 to start). It also
  knows hard limits: 2 °C under the throttle point, 1.5 × `disk_min_free_gb` free, 2.4 GB of memory, a 256 MB
  write-ahead log, 36 h without a good backup, 30 min offline, 14-day-old safety lists.
- **When to open an incident.** A reading outside its normal range twice in a row, or past a hard limit once.
- **Finding the cause.** It ranks the likely causes, runs the most telling read-only checks, then acts.

**What it does itself.** Each fix is bounded, written to `polymath changes` (area `caretaker`), and checked after a
while; when one does not work, it tries the next:

| fix | when | bound |
|---|---|---|
| slow the agent down to 60 % | hot, or high memory | ends by itself after an hour; back to full speed once normal |
| eviction and housekeeping now | the disk is filling | at most every 6 h |
| a backup now | no good backup for 36 h | at most every 6 h |
| a write-ahead log checkpoint | the log keeps growing | at most every 10 min, between job slices |
| ask whether the internet is back | offline for 30 min | at most every 5 min |
| requeue jobs that died of a passing problem (network, disk, a lock) | jobs gave up | once per job, 50 at a time |
| pause a job kind for 6 h | it failed 3 times in an hour | never core work, at most 3 kinds |
| fresh safety lists | older than 14 days | weekly |
| roll back a setting it tuned in the last 3 days, and pin it | self-test scores dropped | daily |
| ask the planner for more reading | nothing left to read | at most every 6 h |

**What it tells you.** Under-voltage, the drive's USB speed, an update that is not running, a configuration or
permission change, unknown problems, and any fix that did not work. Each message comes once, then at most once a day,
and has four parts: what is wrong, the evidence, the most likely cause and how sure it is, and the exact step. The
step is quoted from the known fault in `polymath/agents/faults.toml`, with who may do it (`[needs your approval]`
means it changes the system). Faults you put on hold (`[situation] on_hold`, today: SD-card mode) are noted, never
raised.

**Where you see it:**
- **The feed:** `Orin` lines (`Orin ✓` when a fix held), and the face turns to `•_• ⚙` while it works.
- **`polymath doctor`:** `Orin` lines with its advice.
- **`polymath caretaker`:** the briefing ends with Orin's open incidents and anything it slowed down or paused.
- **`polymath caretaker incidents [--all]`:** its incidents, open ones (or, with `--all`, the last 30 with their
  outcome).

**It never** runs commands, opens network connections, changes permissions, mounts, formats, deletes files, writes
configuration or code, or widens what Polymath may visit. A test reads its source to check. Switch it off with
`caretaker = false` under `[agents]`.

### <a id="always-on"></a>Always on (v0.5.1)

**Without the internet** it keeps working:
- **Noticing:** two failed connections in a row while reading, or failures at two different sites in one step
  with no site answering, mean offline. One dead website never counts. The feed shows `offline` and the face
  turns studious.
- **Holding back:** downloads, dump reading, crawling, feeds and site vetting wait. They keep their place and
  progress and never use up their retries, so a long outage loses nothing.
- **Studying:** reasoning, contradiction and sanity checks, predictions, rule audits, quizzes, self-tuning,
  duplicate merging, re-training from text and the agents run more often than usual (`drive/offline.py`).
- **Coming back:** it asks a server it already reads from (one small request) after 1, 2, 4 and 8 minutes, then
  every 10 minutes. The first answer brings it back: "back online after 3 h: reading again".

**The dashboard** cannot hang the live feed any more:
- requests run on a fixed pool of 8 threads, each with its own read-only connection;
- every query has a deadline: 4 s for the feed and health, 20 s for pages, then the request answers "busy";
- the expensive full-table counts are made by a background thread, never on a request;
- a question in the ask box no longer blocks every other page while it loads the answerer;
- it checks its own `/health` every 15 s and pings systemd only while it answers (`WatchdogSec=60`), so a stuck
  dashboard is restarted within a minute.

**The live feed** never goes dark. If the dashboard does not answer, it tries the database directly (when you
may read it) and then the agent's own snapshot, `/run/polymath/feed.json`. The agent writes that every 5
seconds, in memory, readable by everyone. The feed says which one it shows and what to run, and goes back to the
dashboard as soon as it answers.

**The brain's drive missing** (unplugged, a loose cable, a USB drop-out, or not mounted yet at boot):
- **The agent stays up.** It shows "paused — the brain lives on the drive …, which is not plugged in" in the
  feed and on the dashboard, and starts by itself within seconds of the drive being mounted again.
- **Before v0.5.2**, both services stopped and stayed stopped: the feed showed only "no answer".
- **The dashboard always starts**, even without the brain, and says why.

**It brings the brain's drive back by itself** (v0.5.5):
- `polymath-heal.timer` runs `polymath storage heal` 45 s after boot and then every minute. While the brain is
  healthy it does nothing.
- When the brain is missing or dead, it escalates:
  1. a dead or frozen mount (v0.5.7: one that does not answer at all) is dropped lazily, the USB drive behind it
     is reset, and the services are restarted without waiting on them; then the drive is asked for again;
  2. a USB drive reporting 0 bytes gets a soft reset of just that device;
  3. still missing after 90 s, the USB ports' power is cut for 20 s and restored (`uhubctl`). That is a real
     replug, for enclosures like the DockCase whose capacitor keeps the drive in "safe off" across a reboot. At
     most once every 15 minutes and 6 times a day.
- Every action goes to the caretaker's inbox and to `polymath doctor`. Settings: `[storage] heal`,
  `heal_power_cycle`, `heal_hubs`, `heal_power_off_s`; standby brain: `standby`, `standby_after_s`,
  `standby_budget_gb`, `standby_dir`.
- A drive reporting 0 bytes is never treated as blank and never formatted.
- **It keeps learning without its drive** (v0.6.0, `core/standby.py`):
  - after `[storage] standby_after_s` (5 min) with the brain's drive missing, frozen or dead, the agent starts a
    small, separate **standby brain** on the SD card (`/var/lib/polymath/standby`, systemd's
    `StateDirectory`) and learns there: feeds, the crawler, single Wikipedia articles, vetted sites. No dumps, no
    backups, no drive pool; at most `standby_budget_gb` (3 GB), with the SD card wear guard watching;
  - the feed's header says `learning on standby`, the face looks at its notes, and `doctor` shows a `standby`
    line; the caretaker's inbox records when it started;
  - a restart while the drive is still gone goes straight back to the standby brain (no second 5-minute wait);
  - when the drive is back (the storage helper restarts the agent, or the agent sees it within 30 s), the agent
    restarts on the real brain and folds the standby brain into it: its documents (through the normal store, so
    duplicates are recognised and the real brain reads them again with its whole graph), Wikidata records, the
    crawler's progress and the problems it noted. Then the standby brain is removed. The feed shows a `standby`
    line with how much moved;
  - folding only adds to the real brain, resumes where it stopped after a restart, and never runs twice;
  - switch it off with `[storage] standby = false`.
- **Nothing waits on a frozen drive** (v0.5.7): every look at the brain's storage goes through `core/fsprobe`, a
  child process with a time limit, so a drive that stops answering makes Polymath wait visibly, never hang.

**Every problem is recorded** (`core/issues.py`, the caretaker's inbox):
- what gets recorded: jobs that give up, body pauses (heat, full disk, read-only card), restart loops, unclean
  stops, time spent without the brain's drive, and a fallback snapshot that cannot be written;
- each one shows in the feed as a red or yellow `problem` line, and `polymath doctor` lists the last day's;
- the coming caretaker agent reads the same list.

**Restarts:** both services restart forever, waiting longer between attempts (up to 5 minutes for the agent and
1 minute for the dashboard), so they come back by themselves once whatever stopped them is fixed.

**SD card wear.** An SD card wears out from writes, and nothing on it reports its own wear, so Polymath measures
writes and estimates. Every 15 minutes it records the bytes written to the data disk and the system disk
(`/sys/dev/block/…/stat`), and the agent's own share (`/proc/self/io`):
- **rated life**: `capacity × sd_endurance_cycles ÷ sd_write_amplification` (1,000 and 3 by default, which
  is conservative for consumer cards). The daily budget makes that last `sd_target_years` (5);
- **over budget**: on the data disk it switches to saver mode, with twice-as-long job slices. Each slice is
  one commit, so fewer, larger commits write less. The feed and the digest say so once a day;
- **read-only**: a data disk the kernel turned read-only (the usual sign of a failing card) pauses everything,
  with that reason in the feed;
- **lower baseline**: idle cycles no longer write a heartbeat every 2 seconds (that was about 350 MB a day),
  and a busy job slice writes 25 % less than before (measured).

`polymath wear` shows the numbers ("SD card (data disk): 3.6 GB written in the last day, budget 11.7 GB/day; at
this rate it lasts about 6.6 years"). The figure is a rate, not the card's remaining life, because writes from
before Polymath was installed are unknown. **The permanent fix is a drive** (§8): plug one in and the brain
moves onto it.

The limits leave half the CPU and a quarter of the memory to the desktop. To give the agent the whole Pi, raise
them with a drop-in (`sudo systemctl edit polymath`, for example `CPUQuota=350%` and `MemoryMax=6G` on an 8 GB
Pi).

## 5. Backups and restore

- **Nightly backups** run at 03:00 local time (`body.backup_hour`):
  - SQLite's online backup API copies the database while the agent runs;
  - `PRAGMA quick_check` verifies the copy;
  - the copy is compressed with xz and stored in `/srv/polymath/backups/`;
  - the newest 7 are kept (`body.backup_keep`);
  - (v0.9.0) when the disk is short of room, older backups are deleted first (the newest is always kept); only if
    it still could not hold it is the backup skipped, and logged.
- **In SD-card mode** (v0.9.0) the backup runs **weekly** and **one** is kept: each backup writes about three times
  the brain's size to the card the brain lives on, so a nightly one cost about 5 GB of writes a night. Orin's
  backup-age alarm follows the interval (36 h nightly, 10.5 days weekly).
- **On demand:** `polymath backup` · `polymath backup --list`.
- **Restore:**

  ```bash
  sudo systemctl stop polymath polymath-dashboard
  sudo -u polymath /opt/polymath/venv/bin/polymath restore polymath-20261009-030000.sqlite3.xz
  sudo systemctl start polymath polymath-dashboard
  ```

  The restore checks the backup's integrity before swapping it in, and keeps the replaced database as
  `polymath.sqlite3.pre-restore-<time>`.

The indexes under `/srv/polymath/index` are rebuilt from the database automatically if they are lost.

## 6. Power loss and crashes

Each job slice and its bookkeeping commit in one SQLite transaction (WAL, `synchronous=NORMAL`). After a power
cut:
- the agent restarts;
- interrupted jobs are re-queued from their last checkpoint;
- a job whose process died mid-slice 3 times is dead-lettered (`polymath status` lists dead jobs), so one bad
  input cannot loop forever.

The systemd watchdog (120 s) restarts a hung agent.

## 7. Configuration

Everything is optional; see the commented `/etc/polymath/polymath.toml`. Common changes:

```toml
[senses]
contact = "you@example.org"          # recommended by Wikimedia for dump users
feeds = ["https://example.org/feed"] # replaces the built-in feed list
seeds = ["https://docs.example.org/"]
stackexchange_sites = ["ai", "physics", "math"]
wikipedia_lang = "en"

[body]
disk_budget_gb = 400
```

**A second Wikipedia language** (`second_language = "es"` under `[senses]`; also `de`, `fr`, `it`, `pt`): about one
Wikipedia part in five is then read from that language, only for its infoboxes and tables, to back up or contest
facts. It is on automatically in big-brain mode.

**Big-brain mode** turns on by itself when the brain has a large drive (a budget of 400 GB or more, for example the
brain on your 1 TB SSD). It raises settings you have not set yourself: word vectors for 400,000 words, 40 OpenAlex and
PubMed files, 3,000 books, 15 Stack Exchange sites and Spanish Wikipedia as a second language. The changelog says
when it switches. Any of these set in `polymath.toml` stays as you set it.

**Drop-ins.** Files in `/etc/polymath/polymath.d/*.toml` are read after `polymath.toml`, in name order, and override
it key by key (a typo is refused there too). `scripts/sd-card-mode.sh` switches SD-card mode with one
(`sd-card.toml`), so `polymath.toml` itself is never edited by a script.

**Your other devices** (`[access]`, v0.9.0, see [§3](#browser)):

```toml
[access]
remote = true        # false: the dashboard answers only on the Pi itself
port = 8443          # HTTPS for paired browsers on your home network
networks = ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
            "fc00::/7", "fe80::/10"]   # the only addresses let in at all
names = []           # extra host names (the Pi's own name, <name>.local and localhost are always answered)
```

Apply changes with `sudo systemctl restart polymath polymath-dashboard`.

## 8. Storage pool: drives you plug in

Plug a drive in and it becomes part of the brain. No commands are needed:

1. udev starts `polymath-volume@<device>.service`, which runs `polymath storage attach` as root.
2. The helper checks the drive and mounts it at `/mnt/polymath/<filesystem UUID>`, with a `polymath-brain/`
   folder and a manifest saying how much of it Polymath may use.
3. The agent notices within 30 seconds. The live feed shows `storage  new drive …`, and the header shows the
   total brain space.

| the drive | what happens |
|---|---|
| brand new, nothing on it (no partition table, no filesystem, zeros at both ends) | formatted: GPT, one ext4 partition labelled `POLYMATH`; 90 % of it becomes brain space |
| brand new from the shop: exFAT/NTFS/FAT, 32 GB or more, only the maker's installers and manuals on it | formatted ext4 `POLYMATH` **once** (see below), then the brain moves onto it |
| labelled `POLYMATH*`, or empty | dedicated: 90 % of its free space |
| already holds your files (ext4, exFAT, FAT, NTFS, btrfs, xfs) | shared: half its free space (`shared_drive_share`), always leaving 10 % free; **your files are never touched** |
| the system disk, SD card, encrypted/LVM/RAID/swap, unknown data, ignored or retired | left alone |

**What lives on drives.** Document bodies are the bulk of the database:
- once the main disk passes 75 % of its budget (`storage.spill_at`), the least valuable bodies move to the
  roomiest drive;
- eviction only starts when every drive is full;
- downloads and the nightly backups also go to drives (the backups to the drive chosen for them, [below](#roles)).

The database, indexes and reports stay on the main disk.

**Unplugging.** The agent keeps running. Documents on that drive read as unavailable, and their facts and
metadata stay. They come back when the drive is plugged in again. For a clean removal:

```bash
polymath storage list                 # every drive: brain space, use, documents on it
sudo polymath storage eject <id>      # unmount cleanly, then unplug (for a short while)
polymath storage retire <id>          # bring its documents back (what no longer fits is evicted), then
                                      # unplug for good; it is never adopted again
```

### <a id="roles"></a>Which drive does what: Polymath decides (v0.11.0)

Plug in as many drives as you like (the Pi 4 has two blue USB 3 ports and two black USB 2 ports). Every minute
Polymath looks at each drive and decides by itself:

| role | which drive | why |
|---|---|---|
| **brain** | the best Linux-formatted drive of Polymath's own on USB 3 (or NVMe), with room for three times the brain, plugged in 10 minutes, steady this week | the database and indexes need speed and a connection that does not drop |
| **backups** | the steadiest *other* drive with room (a hard disk or a USB 2 drive is fine) | a dead brain drive must not take the backups with it |
| **extra room** | every other drive | document texts and downloads spill there when the brain's disk fills |

How it ranks drives for the brain: the connection (NVMe, USB 3 at 10 or 5 Gbit/s; USB 2 never), an SSD over a
spinning disk, its measured read speed (checked once a day), its room, and how often it dropped out this week.
A drive that holds your files, or is formatted for Windows/macOS, is never the brain (a fresh shop drive is
formatted for Linux once by itself; a drive with files never is).

**When the brain moves**, by itself:
- **off the SD card**, once the best drive has been plugged in for 10 minutes (so with several drives plugged in
  at once it picks the best, not the first);
- **onto a clearly better drive** (a class better: a hard disk → an SSD, USB 3 → NVMe) at night, after the
  backup (04:00–07:00), at most once a day. The agent stops while the brain is copied and checked;
- **off a failing drive** at once, when the brain's drive dropped out three times this week and a healthy one is
  there.

The old drive keeps its copy of the brain for a week (`polymath-brain/home-previous-…`) and becomes extra room;
the copy is removed after a steady week. A move that fails leaves the brain where it was and is tried again in 6
hours. Nothing outside `polymath-brain/` is ever touched.

```bash
polymath storage roles     # each drive's role, kind, speed, score, and why it is not the brain; the last moves
polymath storage list      # the same drives with brain space, use and documents (a role column)
```

`polymath doctor` shows `drives  brain: … · backups: … · extra room: …`, and warns when the drives ask for more power
than the Pi 4 gives its USB ports (1.2 A together: use a powered USB 3 hub), when the backups have no other drive,
and when the brain's drive is on a USB 2 port.

### <a id="wiped"></a>A brain drive that was reformatted, and drive care (v0.11.2)

**The brain's drive was reformatted** (to fix it, or on another computer): its brain is gone, and Polymath does not
wait for it:
- **in SD-card mode**, once it recognises the drive (its serial number or USB device as recorded when the brain moved
  onto it, the USB device `sd-card-mode.sh on` switched off, or its model), the brain on the SD card becomes the
  brain after 10 minutes: renamed into `/srv/polymath` (not copied), SD-card mode ends, and it moves onto the best
  drive by itself ([drive roles](#roles)). Right away, or when it does not recognise the drive:
  `sudo polymath storage keep-sd-brain`;
- **outside SD-card mode**, the brain is rebuilt on the SD card from the newest backup on another drive (a damaged
  one is skipped, and what the standby brain read meanwhile is added by itself), else from the standby brain, else
  afresh; then it moves onto the best drive.

An old brain drive that comes back after the brain moved on never covers it: its copy is kept for a week as
`home-previous-…` and the drive becomes extra room.

**Drive care.** Plug in a drive formatted for Windows or a Mac (exFAT, NTFS, FAT32) that holds **nothing**, and once
it has been plugged in for 10 minutes it is reformatted for Linux (ext4), so it can hold the brain, backups over 4 GB
and full extra room. A drive with your files on it is never reformatted by itself; a FAT32 one gets a warning (no file
over 4 GB fits). A disk with only an empty partition table (what deleting its volumes in Windows leaves) counts as
blank and is formatted too. Off: `[storage] care_format_empty = false`.

**v0.11.3.** "Nothing" now means none of *your* files: what computers leave behind (macOS `._` files, `.apdisk`,
`.Spotlight-V100`, a Linux `.Trash-1000`, Windows' `System Volume Information`, chkdsk's `FOUND.000`) and a maker's
installers do not count. Until it is reformatted, such a drive shows the role **`pending`** in `polymath storage roles`
and nothing is put on it (no backups, no documents). If Polymath already put data there (an older version, or the
minute before the decision), it is reformatted anyway **only while the brain is stuck on the SD card**: those
documents are read again and the backups taken again. A drive with your files says once what is on it and is left
alone. A drive drive care reformatted takes the brain right after (it already settled).

**v0.11.4: the largest drive.** The brain goes on the **largest** drive that may hold it; among drives within a
quarter of that size, the fastest. Later it moves at night onto a drive a quarter larger (or, about as large, a class
faster), never onto a much smaller one. `[storage] brain_prefer = "fastest"` goes by speed first, as before.

**Paused, "cooling down", at 50 °C?** That was a disk pause with the wrong label (before v0.11.4): the thermal pause
is at 82 °C. With the brain on the SD card outside SD-card mode (after `keep-sd-brain`) the card was held to a
drive's 10 GB reserve. Now it keeps the card's 3 GB (`sd_keep_free_gb`), a disk pause holds until 2.2 GB are free
again (no more on and off), and the feed shows the real reason.

**The brain stays on the SD card and the feed is paused for disk space?** Look at the feed's **DRIVES** line (or
`polymath storage roles`): it names each drive that cannot take the brain and why, what only you can fix first. The
usual one is **on USB 2**: move the drive to a **blue** USB 3 port, with a USB 3 cable and no USB 2 hub. USB 2 is
refused for the brain on purpose: the SSD froze the Pi there before.

### <a id="relearn"></a>What each drive holds, and a drive lost by accident (v0.11.0)

Every hour Polymath writes down what each drive holds: the documents whose texts are there (how many, from which
sources, about which topics, how many facts rest on them), downloads, backups and old brain copies. It keeps this in
the brain and on the drive itself (`polymath-brain/inventory.json`).

When a drive goes away (unplugged, or its connection drops), the agent keeps running: its documents read as
unavailable and every fact stays. If it is still gone after **2 hours** (`relearn_after_h`):
- its documents are marked lost and **relearned onto the other drives, the most important first**: the ones most
  facts rest on, and topics you asked about, before the rest. Wikipedia articles are read again from Wikipedia
  (only the needed bytes), web pages and feed articles are fetched again; documents from other dumps come back
  when that dump is read again;
- a new backup is taken if the drive held backups, and its downloads are planned again;
- the feed says `drive … is still gone: it held N documents (M facts rest on them); relearning K of them`, doctor
  shows `relearning  N read again, M to go`.

Plug it back in and its documents are readable again at once; relearning them stops (`drive … is back`). For a
drive that died: `polymath storage retire <id>` so it is no longer waited for.

### <a id="home"></a>Moving the brain onto a drive (your SSD)

While the brain is on the SD card, the first **dedicated, Linux-formatted** drive with room becomes its new home,
automatically:
- "dedicated" means it was blank and Polymath formatted it, or it is labelled `POLYMATH`, or it is empty;
- "room" means at least `home_min_gb` (32 GB) free, and three times the brain's size.

What happens:
1. the agent and the dashboard stop (each commits and exits cleanly);
2. `/srv/polymath` is copied to `polymath-brain/home` on the drive and checked: the same files and bytes, and
   SQLite's `quick_check` on the database. A 15 GB brain takes a few minutes;
3. the SD card's copy is moved aside to `/srv/polymath-sd-copy`, as a safety net. It is not deleted, and it is
   not used any more;
4. the drive's copy is mounted over `/srv/polymath` and everything starts again. The feed says `storage  moved
   the brain onto <drive>`, `polymath storage list` shows the main disk as that drive, and its budget is the
   drive's (90 % of it).

From then on, every boot mounts the brain from the drive before the agent starts. If the drive is missing, the
agent **waits** (`polymath status` / the journal: "the brain lives on the drive …, which is not plugged in") and
does not run on the old SD copy. Plug the drive back in and it starts by itself. `storage eject` refuses
while the agent runs on it.

**A new SSD straight from the shop** (most come formatted exFAT or NTFS, with the maker's software on them) is
formatted for Linux automatically and the brain moves onto it. Plug it in and wait a few minutes. It counts as
fresh only when all of these hold:
- it is 32 GB or more (`home_min_gb`), so USB sticks stay as they are;
- it holds nothing but the maker's installers and manuals (for example `SamsungPortableSSD_Setup_Win.exe`,
  `WD Discovery for Mac.dmg`, `Quick Start Guide.pdf`, under 1 GB in all) and system clutter
  (`System Volume Information`, `.DS_Store`…). **A single file or folder of yours, and it is not touched**;
- Polymath has not stored documents on it;
- it has never been formatted by Polymath before.

**It is formatted once per drive, ever.** Before formatting, Polymath writes the drive's serial number to
`/var/lib/polymath/formatted-drives.json` on the SD card. Unplugging it, a loose cable or a power cut never
causes a second format: after the first format the drive is Linux-formatted with Polymath's folder on it, so it
is recognised as the brain and simply mounted again. That holds even if the power fails during formatting, and
even if you later reformat it on a laptop and plug it back in. A drive that reports no serial number or model is
never formatted on its own.

Turn it off with `format_fresh_drives = false` under `[storage]`.

**A Windows or macOS drive with your files on it** is used for documents only. The feed tells you what to do.
Reformat it explicitly:

```bash
polymath storage list                              # find its id
sudo polymath storage format <id> --yes            # an empty drive: reformatted ext4, then the brain moves
sudo polymath storage format <id> --yes --erase-files   # it holds files (e.g. the maker's installers): erased
```

`format` lists the files first and refuses without `--erase-files`. It refuses outright if Polymath already
stored documents there.

When you are happy with the drive, free the SD card's space with `sudo rm -rf /srv/polymath-sd-copy` (optional).
To turn the move off: `move_home = false` under `[storage]`.

**Settings** (`[storage]` in `/etc/polymath/polymath.toml`):
- `adopt = false` stops adopting drives;
- `format_blank_disks = false` never formats a blank drive; `format_fresh_drives = false` never formats a fresh shop
  drive (both off: nothing is ever formatted on its own);
- `ignore = ["<uuid>"]` skips a drive (UUIDs are in `polymath storage list` and `lsblk -f`);
- `shared_drive_share` and `reserve_fraction` set how much of a drive is used.

The desktop no longer auto-mounts drives that Polymath adopts. Their files are under
`/mnt/polymath/<uuid>/` (read them with `sudo`), or `eject` the drive first.

### <a id="sd-card-mode"></a>SD-card mode: the whole brain on the SD card (v0.7.0)

For when the brain's drive cannot be trusted: it keeps freezing, drops out, or runs at USB 2 speed and stalls.
Polymath then runs entirely on the SD card until the drive is fixed, and nothing touches the drive.

```bash
cd ~/polymath-src && git pull --ff-only origin claude/polymath-agent
cd polymath && sudo ./scripts/sd-card-mode.sh on      # switch to the SD card (also installs this checkout)
./scripts/sd-card-mode.sh status                       # which mode, how much room (no sudo)
sudo ./scripts/sd-card-mode.sh off                     # back to the SSD, once it works again
```

**`on`**, in seven checked steps (each time-limited, marked ✓/–/✗, logged to `~/polymath-sd-card.txt`):
1. shows what is installed, what will be installed, and the SD card's free space;
2. stops the agent, the dashboard and the drive helper, without waiting on a frozen drive;
3. switches the mode on with one drop-in, `/etc/polymath/polymath.d/sd-card.toml` (`sd_only = true`);
4. lets go of the SSD. It is unmounted (cleanly if it answers, lazily if it is frozen), and its USB device is
   switched off, which is like unplugging it: anything stuck on it is released. You may unplug it now;
5. starts the brain in `/var/lib/polymath/brain`, from the copy the SD card kept when the brain moved onto the
   SSD (`/srv/polymath-sd-copy`). The copy is checked first (SQLite `quick_check`), then **renamed, not copied**,
   so it costs no room. Without a copy, with a damaged one, or with `--fresh`, it starts empty. The SSD's brain is
   never touched;
6. installs this checkout (`install.sh --allow-sd-card`);
7. checks the installed version, the services, and that the agent says it runs on the SD card; then
   `polymath doctor`.

Running `on` again is safe: the SD-card brain carries on, and an SSD that a reboot switched back on is switched
off again. `--keep-ssd-on` leaves its USB device on.

**In SD-card mode**:
- everything runs as usual, dumps included;
- the brain may use all the card can give (what it uses plus what is free) but `sd_keep_free_gb` (3 GB since
  v0.9.0), which stays free for the system. When the card runs low, it slows down and evicts the least valuable
  bodies first (old feeds and web pages; the facts stay);
- one backup is kept and it runs weekly (v0.9.0), and the SD card wear guard watches the writes;
- `polymath storage usage` shows where the room goes and how many days are left at the current rate; the dashboard
  shows the same, and Orin opens a `disk_low` incident when the reserve is under three days away;
- no drive is used: no storage pool, no moving or mounting the brain's drive, no `storage heal`, no standby brain
  (this *is* the brain);
- the feed shows `learning on the SD card` and `(SD-card mode since …: the brain is on the SD card, N GB of room;
  the SSD is off)`. Doctor shows an `sd card` line with the way back, and the caretaker's inbox gets a note once a
  day.

**`off`** switches the SSD's USB device back on, removes the drop-in and asks for the drive. Once it is mounted,
the agent starts on its real brain and **moves what it learned on the SD card onto it**: every document it read
in SD-card mode (not what the copy started with: the SSD has that already), the Wikidata records, the crawler's
progress, the problems it noted, and the agents you set up there (set up again from their directive, with the
same name and status; agents it evolved by itself stay behind, the SSD's society evolves its own). Then the SD-card brain is removed. The feed says `back on its drive after …
on the SD card`. If the SSD does not come back within two minutes, **SD-card mode simply stays on**: nothing else
changes, and the agent keeps learning on the SD card.

### <a id="ssd-watch"></a>Back onto the SSD by itself: the SSD watch (v0.10.0)

You no longer need `off`. In SD-card mode, the drive helper (every minute, as root) looks for the brain's SSD and
**moves the brain back onto it by itself** once it has been healthy for 10 minutes, whether you are at the Pi or not.

**What it sees** is the end of the feed's SD-card line, the `sd card` line in `polymath doctor`, a line under
*Storage* on the dashboard, and `./scripts/sd-card-mode.sh status`:

| it says | meaning | what happens / what to do |
|---|---|---|
| `SSD not plugged in` | nothing on USB at all | plug it into a blue USB 3 port; if it never shows up, it gets no power: a powered USB 3 hub or its own supply |
| `SSD plugged in but not answering: it needs more power` | the enclosure answers, the drive reports 0 bytes | one soft USB reset every 10 min; for good: a powered USB 3 hub or the enclosure's own power |
| `SSD enclosure on USB but no drive` | the enclosure, no drive behind it | unplug it for 10 s, plug it back in; if it stays, more power |
| `SSD on USB 2: move it to a blue USB 3 port` | linked at 480 Mbit/s, what made it freeze | not used until it is on USB 3 (a USB 3 cable, no USB 2 hub) |
| `SSD switched off; tested again later` | `sd-card-mode.sh on` switched its USB device off | switched on to test it after 1 h, 3 h, 6 h, 12 h, then daily; off again if no drive shows up. Replug it to test it now |
| `SSD back: checking it` | plugged in at USB 3 | checked every minute: 16 MiB read at its start, middle and end past the cache, within 30 s, and no new I/O errors. 10 healthy minutes, then it moves back |
| `SSD failed a check; tried again later` | a check failed or took too long | waits 30 min, 1 h, 3 h … a day; a replug tries again at once |
| `moving back onto the SSD` → `back on the SSD` | the move | what `off` does: stop, set the drop-in aside, mount the SSD, start; what it learned on the SD card moves over by itself |
| `the SSD failed again: back on the SD card` | it dropped out twice within a day of coming back | SD-card mode by itself (the SSD's mounts let go, its USB device off); tested again a day later. The SSD's brain is untouched |

A move cut short by a power cut is finished or undone the next minute, and everything is started again. It does
nothing in the first hour after `sd-card-mode.sh on` (unless you replug the SSD), never formats, never
deletes a brain, and never uses a drive that is not the brain's. Every step is in the caretaker's inbox
(`storage/ssd_watch:…`, `polymath caretaker`) and in `/run/polymath/ssd-watch.json`.

Settings (`[storage]`; leave `sd_only` itself to the script): `sd_dir` (`/var/lib/polymath/brain`),
`sd_budget_gb` (0: what the card can give), `sd_keep_free_gb` (3.0), and for the SSD watch `sd_auto_return`
(true), `sd_return_stable_min` (10), `sd_return_allow_usb2` (false), `sd_auto_fallback` (true).

## 9. Troubleshooting

| symptom | check |
|---|---|
| anything | `polymath doctor`: each broken part with the command that fixes it |
| `polymath doctor` prints a Traceback ending in `PermissionError … /etc/polymath/tls/server.crt` (v0.11.0) | fixed in v0.11.1: install the update; meanwhile `sudo polymath doctor` |
| service won't start | `journalctl -u polymath -b`; is `/srv/polymath` mounted (`findmnt /srv/polymath`)? |
| dashboard says "cannot reach the database" | `systemctl status polymath`; `ls -l /srv/polymath/db` |
| `/health` 503 | heartbeat older than 60 s: the agent is stopped, paused for heat, or hung (the watchdog restarts it) |
| not learning anything new | `polymath status` (dead jobs, queue) and `polymath why` (recent decisions) |
| too hot | improve cooling, or lower `throttle_celsius` / `pause_celsius` |
| live feed says "no answer from http://127.0.0.1:8765: showing … instead" | the dashboard is down or hung; the feed carries on from the agent's snapshot. systemd restarts a hung dashboard within a minute; to do it now: `sudo systemctl restart polymath-dashboard` (details: `journalctl -u polymath-dashboard -n 40`) |
| live feed says "waiting: …" (every source failed) | the agent is not running either: `polymath doctor`, then `sudo systemctl restart polymath` |
| the feed says "paused — the brain lives on the drive …, which is not plugged in" | the SSD dropped out or is not mounted: check its cable and power (a Pi 4 port can brown out a bus-powered SSD; a powered hub or the official 3 A supply helps), and that `lsblk` lists it. Polymath starts by itself once it is back |
| `lsblk` shows `sda 0B usb` (doctor: "reports 0 bytes") | the SSD's enclosure answers but the drive does not: unplug it for 10 s, plug it into a blue USB 3 port; if it recurs, power it from a powered hub or its own supply |
| `Job for polymath.service failed because a timeout was exceeded`, or doctor: "does not answer at all" | the brain's drive froze (reads never return). v0.5.7 waits instead of hanging and `storage heal` resets the drive within a minute or two. By hand: `sudo systemctl stop polymath-heal.timer; sudo umount -l /srv/polymath; sudo umount -l /mnt/polymath/*; sudo usbreset 31db:9210` (your enclosure's ID from `lsusb`), wait 10 s, `sudo systemctl restart 'polymath-volume@*' polymath polymath-dashboard; sudo systemctl start polymath-heal.timer`. A reboot or a replug also clears it |
| "Input/output error" on `/srv/polymath` (doctor: "its storage does not answer") | the SSD dropped out and came back as a new device; the old mount is dead. `sudo reboot` clears it (v0.5.4 also replaces a dead mount by itself when the drive is attached again) |
| the feed says `learning on standby` (doctor: `standby`) | the brain's drive has been gone for over 5 minutes, so Polymath learns on its small standby brain on the SD card. Nothing to do for Polymath: bring the drive back (see the rows above). What it read moves onto the drive by itself |
| an `Orin` line in the feed or doctor | Orin the caretaker found something: its line says what, the evidence, the likely cause and the exact step ([§4](#orin)). `polymath caretaker incidents` lists what it is working on. A fix it made itself needs nothing from you |
| anything else, or you want the whole picture | `polymath caretaker` (no sudo) gives the situation now, the recent problems with what each one is, and the next steps. `polymath caretaker known` lists every known problem, `polymath caretaker show <id>` gives its symptoms, causes, checks and fix, and `polymath caretaker history` lists what happened on this Pi |
| the SSD keeps freezing or dropping out, and you want Polymath working now | run everything on the SD card until the SSD is fixed: `cd ~/polymath-src && git pull --ff-only origin claude/polymath-agent && cd polymath && sudo ./scripts/sd-card-mode.sh on` ([§8](#sd-card-mode)); the log is in `~/polymath-sd-card.txt` |
| the feed says `learning on the SD card` (doctor: `sd card`) | SD-card mode is on: the brain runs on the SD card and the SSD is not used. Nothing to do. Once the SSD works again (blue USB 3 port, USB 3 cable, no USB 2 hub): `sudo ./scripts/sd-card-mode.sh off` |
| the SSD is plugged in but Polymath stays on the SD card (and says low on disk) | since v0.10.0 the SSD watch says why at the end of the feed's SD-card line and in `polymath doctor` (`sd card`): not plugged in, needs more power, USB 2, or checking (10 minutes). Do what it says; it moves back by itself ([§8](#ssd-watch)). Before v0.10.0, SD-card mode ignored every drive until `sudo ./scripts/sd-card-mode.sh off` |
| `sd-card-mode.sh off`: "the SSD did not come back … SD-card mode stays on" | the SSD is still not answering: plug it into a blue USB 3 port, check that `lsblk` lists it (sda), then run `off` again. Polymath keeps learning on the SD card meanwhile |
| `sd-card-mode.sh on` shows ✗ | send `~/polymath-sd-card.txt`. Running `on` again is safe; if a process stays stuck on the old SSD, `sudo reboot` (SD-card mode stays on, the SSD is not mounted again) |
| Polymath is stuck and an install does not take (the Pi in general, one command) | `cd ~/polymath-src && git pull --ff-only origin claude/polymath-agent && cd polymath && sudo ./scripts/rescue.sh`: it stops the services, frees a frozen drive (lazy unmounts, USB reset), finishes `dpkg`, adds `uhubctl`, installs, and checks the result. Every step is time-limited and marked ✓/–/✗; the full log is in `~/polymath-rescue.txt` |
| an update "did nothing" (the same problem after `git pull` and `install.sh`) | check what is installed: `cat /opt/polymath/build.json`, and which branch the clone is on: `git -C ~/Blazeffect83 branch -vv`. Update Polymath's own clone, never another project's checkout: `git -C ~/polymath-src pull --ff-only origin claude/polymath-agent` (if it is missing: `git clone -b claude/polymath-agent https://github.com/Blazeffect83/Blazeffect83.git ~/polymath-src`), then `cd ~/polymath-src/polymath && sudo ./install.sh --allow-sd-card` |
| doctor: `/dev/sda runs at USB 2 speed (480 Mbit/s)` | the SSD is in a black USB 2 port, or on a USB 2 cable or hub. Move it to a blue USB 3 port with a USB 3 cable; `lsusb -t` should then show it under the `5000M` bus |
| the feed says `offline` | no internet: it studies what it has and resumes by itself; check the router or cable if it lasts |
| the header said `agent offline` for a few minutes after it learned something (before v0.8.2) | one long step (a backup, a quiz, training, a big query on the SD card) went over a minute without a sign of life. The feed called that offline, and after 2 minutes systemd restarted the agent. Since v0.8.2 the vigil vouches for it: the NOW line says `(one long step: 4 min so far — busy, not stuck)`. Which steps were slow: `polymath caretaker` (`jobs/slow:…` lines) and `polymath status` (`slow_steps`). Whether it was restarted before: `journalctl -u polymath --since -2d \| grep -ciE 'watchdog\|SIGABRT'` |
| `agent offline` even with v0.8.2 | it really stopped or hung. After 15 min with no progress the vigil stops vouching and systemd restarts it. `polymath doctor`; `journalctl -u polymath -n 80` |
| Chrome: *Your connection is not private* (`NET::ERR_CERT_AUTHORITY_INVALID`) on `https://<pi>:8443/` | expected until you trust the Pi's certificate: **Advanced → Proceed**, or trust it once ([§3](#browser), step 4) |
| Chrome: `NET::ERR_CERT_COMMON_NAME_INVALID` after it worked | the Pi got a new address: `sudo /opt/polymath/src/scripts/access-cert.sh && sudo systemctl restart polymath-dashboard`, or use `https://<name>.local:8443/`; a DHCP reservation in the router keeps the address |
| Chrome: *This site can't be reached* on port 8443 | `polymath doctor` (`access` line); `systemctl status polymath-dashboard`; no certificate yet: `sudo /opt/polymath/src/scripts/access-cert.sh` (needs `openssl`), then restart the dashboard |
| `http://<pi>:8765/` from the PC stopped working (v0.9.0) | on purpose: that listener needs no sign-in, so it answers only on the Pi. Use `https://<pi>:8443/` and pair |
| the page says *home network only* | the PC's address is not in `[access] networks` (a VPN on the PC, or a guest Wi-Fi): switch it off, or add that range |
| the pairing page says the code expired or is cancelled | a code works once, for 15 minutes, and five wrong guesses cancel it: `polymath access pair` for a new one |
| a paired browser is sent back to pairing | its key was removed (`polymath access log` says when), cookies were cleared, or 400 days passed: pair again |
| a device you do not recognise in `polymath access devices` | `polymath access revoke <id>` (or `all`), then `polymath access log` to see when it paired |
| the feed: `DRIVES  the brain stays on the SD card: X cannot hold it (on USB 2 …)` | move X to a blue USB 3 port (USB 3 cable, no USB 2 hub). A "formatted for Windows/macOS" reason with none of your files on it fixes itself: it is reformatted for Linux after 10 minutes ([§8](#wiped)) |
| `polymath storage roles` says a drive is `not the brain: …` | the reason it gives: on USB 2 (move it to a blue port), holds your files or is formatted for Windows/macOS (only a Polymath drive holds the brain), too small, still settling (10 min), or dropped out this week. It is used for backups or extra room meanwhile |
| doctor: `the drives ask for … mA, more than the 1200 mA a Pi 4 gives` | too many bus-powered drives for the Pi's ports: a powered USB 3 hub, or drives with their own power supply; otherwise they drop out |
| `SSD: a drive is plugged in (/dev/sda), but it does not hold Polymath's brain` | the brain's SSD was reformatted (its brain is gone) or another drive is plugged in. If you reformatted it: `sudo polymath storage keep-sd-brain` (the SD card's brain becomes the brain and moves onto the SSD by itself); v0.11.2 also does this by itself when it recognises the drive ([§8](#wiped)) |
| the feed: `drive … is still gone … relearning` | a drive has been away over 2 hours: plug it back in if it was an accident (everything on it is readable again at once); if it died, `polymath storage retire <id>` |
| `polymath storage usage` forecast says a few days left | the card fills up: the oldest document texts go first (the facts stay). Plug in a drive, or lower what it reads (`[senses]`); the forecast updates hourly |
| no feed window at login | is a terminal installed (`sudo apt install lxterminal`)? `~/.xsession-errors` shows launcher errors |
| a plugged-in drive is not used | `journalctl -u 'polymath-volume@*'` says why it was skipped; `polymath storage list` |
| the agent waits: "the brain lives on the drive …" | plug that drive in; `journalctl -u 'polymath-volume@*'` shows it being mounted |
| a new SSD did not become the brain's home | the feed's `storage` line says why: run the `storage format` command it shows (§8) |
| `polymath wear` says over budget | expected on an SD card under heavy ingest: plug in a drive (§8) |
| "ignoring retired configuration key(s)" | delete the `minecraft_*` lines from `/etc/polymath/polymath.toml` (or re-run `install.sh`) |
