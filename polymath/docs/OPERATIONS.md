# Operations

## <a id="nvme"></a>1. Prepare the NVMe drive (once)

Polymath refuses to keep bulk data on the SD card. It needs a filesystem mounted at `/srv/polymath`.

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
sudo ./install.sh --dry-run    # show what it would do
sudo ./uninstall.sh            # remove services and code; KEEPS /srv/polymath and /etc/polymath
sudo ./uninstall.sh --purge --yes   # also delete everything it learned, and its user
```

`install.sh` does the following:
- checks the mount;
- creates the `polymath` system user;
- installs `python3-venv`/`curl` if missing;
- copies the code to `/opt/polymath/src` and builds `/opt/polymath/venv` (numpy only);
- writes `/etc/polymath/polymath.toml` (never overwritten once it exists);
- installs `polymath.service`, `polymath-dashboard.service` and a journald size limit;
- enables desktop autologin (`raspi-config nonint do_boot_behaviour B4`);
- adds the dashboard to the desktop user's autostart (XDG, labwc and wayfire);
- starts everything and waits for `/health`.

## 3. Everyday

| | |
|---|---|
| dashboard | `http://<pi>:8765/` (on the Pi's screen automatically) |
| status | `polymath status` · `systemctl status polymath polymath-dashboard` |
| logs | `journalctl -u polymath -f` (JSON lines; `-o cat` for compact) |
| nightly report | `/srv/polymath/reports/report-YYYY-MM-DD.md` or `polymath report` |
| research a topic | `polymath learn "topic"` or `polymath learn https://example.org/` |

The CLI reads the same configuration as the service (`/etc/polymath/polymath.toml`). Run it as a user who can
read `/srv/polymath`, or prefix it with `sudo -u polymath`.

## 4. How it shares the Pi

| condition | mode | effect |
|---|---|---|
| CPU ≥ 75 °C (until < 72 °C) | throttle | light jobs only, half-length slices |
| CPU ≥ 82 °C (until < 77 °C) | pause | nothing runs |
| players online on the Minecraft server | yield | light jobs only, half-length slices |
| free disk < `disk_min_free_gb` | yield | eviction requested |
| free disk < half of that | pause | only eviction runs |
| data > `disk_budget_gb` | — | eviction requested |

These limits always apply, whatever the mode:
- `CPUQuota=200%` (2 of 4 cores), `MemoryMax=3G`, `Nice=10`, idle-ish IO priority;
- numpy is limited to 2 threads;
- the dashboard is limited to 25 % CPU and 400 MB.

The Minecraft check is the standard Server List Ping to `127.0.0.1:25565`, once a minute. Nothing is installed
on the server. Change the host and port under `[body]`.

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
minecraft_port = 25565
```

Apply changes with `sudo systemctl restart polymath polymath-dashboard`.

## 8. Troubleshooting

| symptom | check |
|---|---|
| service won't start | `journalctl -u polymath -b`; is `/srv/polymath` mounted (`findmnt /srv/polymath`)? |
| dashboard says "cannot reach the database" | `systemctl status polymath`; `ls -l /srv/polymath/db` |
| `/health` 503 | heartbeat older than 60 s: the agent is stopped, paused for heat, or hung (the watchdog restarts it) |
| not learning anything new | `polymath status` (dead jobs, queue) and `polymath why` (recent decisions) |
| too hot | improve cooling, or lower `throttle_celsius` / `pause_celsius` |
