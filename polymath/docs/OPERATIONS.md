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
- writes `/etc/polymath/polymath.toml` (never overwritten once it exists);
- installs `polymath.service`, `polymath-dashboard.service` and a journald size limit;
- enables desktop autologin (`raspi-config nonint do_boot_behaviour B4`);
- adds the live feed terminal to the desktop user's autostart (XDG, labwc and wayfire), plus menu entries for the
  live feed and the dashboard. An upgrade replaces the dashboard autostart of earlier versions;
- installs the storage pool: a udev rule, `polymath-volume@.service` and `/mnt/polymath`, then adopts drives that
  are already plugged in (§8);
- comments out the `minecraft_*` settings of earlier versions in an existing configuration (backup:
  `polymath.toml.bak`). Left in place, they would be ignored with a warning;
- starts everything and waits for `/health`.

## 3. Everyday

| | |
|---|---|
| live feed | opens in a terminal at login · `polymath feed` in any terminal · menu: *Polymath live feed* |
| dashboard | `http://<pi>:8765/` · menu: *Polymath dashboard* |
| status | `polymath status` · `systemctl status polymath polymath-dashboard` |
| logs | `journalctl -u polymath -f` (JSON lines; `-o cat` for compact) |
| nightly report | `/srv/polymath/reports/report-YYYY-MM-DD.md` or `polymath report` |
| research a topic | `polymath learn "topic"` or `polymath learn https://example.org/` |

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
| `report` / `backup` / `error` | nightly report, backups, failed job slices (with retry or give-up) |

Busy streams are capped per refresh (for example 5 facts and 4 documents every 1.5 s), and the rest are counted
(`+4,312 more facts`), so a Wikidata ingest stays readable. The pinned header shows the state, what it is doing
now, its knowledge counts, the latest quiz score and the CPU temperature.

The feed reads from the dashboard service (`/api/feed`, localhost), so the desktop user needs no access to
`/srv/polymath`. Options: `--plain` (no pinned header, for logs or `ssh`), `--no-color`, `--once`,
`--interval 3`, `--url http://<pi>:8765` (watch from another machine), `--direct` (read the database
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

These limits always apply, whatever the mode:
- `CPUQuota=200%` (2 of 4 cores), `MemoryMax=3G`, `Nice=10`, idle-ish IO priority;
- numpy is limited to 2 threads;
- the dashboard is limited to 25 % CPU and 400 MB.

The limits leave half the CPU and a quarter of the memory to the desktop. To give the agent the whole Pi, raise
them with a drop-in (`sudo systemctl edit polymath`, for example `CPUQuota=350%` and `MemoryMax=6G` on an 8 GB
Pi).

## 5. Backups and restore

- **Nightly backups** run at 03:00 local time (`body.backup_hour`):
  - SQLite's online backup API copies the database while the agent runs;
  - `PRAGMA quick_check` verifies the copy;
  - the copy is compressed with xz and stored in `/srv/polymath/backups/`;
  - the newest 7 are kept (`body.backup_keep`);
  - a backup is skipped, and logged, if the disk could not hold it.
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
| labelled `POLYMATH*`, or empty | dedicated: 90 % of its free space |
| already holds your files (ext4, exFAT, FAT, NTFS, btrfs, xfs) | shared: half its free space (`shared_drive_share`), always leaving 10 % free; **your files are never touched** |
| the system disk, SD card, encrypted/LVM/RAID/swap, unknown data, ignored or retired | left alone |

**What lives on drives.** Document bodies are the bulk of the database:
- once the main disk passes 75 % of its budget (`storage.spill_at`), the least valuable bodies move to the
  roomiest drive;
- eviction only starts when every drive is full;
- downloads and the nightly backups also go to drives.

The database, indexes and reports stay on the main disk.

**Unplugging.** The agent keeps running. Documents on that drive read as unavailable, and their facts and
metadata stay. They come back when the drive is plugged in again. For a clean removal:

```bash
polymath storage list                 # every drive: brain space, use, documents on it
sudo polymath storage eject <id>      # unmount cleanly, then unplug (for a short while)
polymath storage retire <id>          # bring its documents back (what no longer fits is evicted), then
                                      # unplug for good; it is never adopted again
```

**Settings** (`[storage]` in `/etc/polymath/polymath.toml`):
- `adopt = false` stops adopting drives;
- `format_blank_disks = false` never formats anything;
- `ignore = ["<uuid>"]` skips a drive (UUIDs are in `polymath storage list` and `lsblk -f`);
- `shared_drive_share` and `reserve_fraction` set how much of a drive is used.

The desktop no longer auto-mounts drives that Polymath adopts. Their files are under
`/mnt/polymath/<uuid>/` (read them with `sudo`), or `eject` the drive first.

## 9. Troubleshooting

| symptom | check |
|---|---|
| service won't start | `journalctl -u polymath -b`; is `/srv/polymath` mounted (`findmnt /srv/polymath`)? |
| dashboard says "cannot reach the database" | `systemctl status polymath`; `ls -l /srv/polymath/db` |
| `/health` 503 | heartbeat older than 60 s: the agent is stopped, paused for heat, or hung (the watchdog restarts it) |
| not learning anything new | `polymath status` (dead jobs, queue) and `polymath why` (recent decisions) |
| too hot | improve cooling, or lower `throttle_celsius` / `pause_celsius` |
| live feed says "waiting: no answer from http://127.0.0.1:8765" | `systemctl status polymath-dashboard`; it reconnects by itself |
| no feed window at login | is a terminal installed (`sudo apt install lxterminal`)? `~/.xsession-errors` shows launcher errors |
| a plugged-in drive is not used | `journalctl -u 'polymath-volume@*'` says why it was skipped; `polymath storage list` |
| "ignoring retired configuration key(s)" | delete the `minecraft_*` lines from `/etc/polymath/polymath.toml` (or re-run `install.sh`) |
