# Operations

Shorthand: `A="sudo -u aegis AEGIS_ENV_FILE=/etc/aegis/aegis.env /opt/aegis/.venv/bin/aegis"`

## Day to day

| Task | iPad | Server |
|---|---|---|
| status | Overview / System | `systemctl status aegis`, `$A status` |
| logs | System → Logs | `journalctl -u aegis -f`, `/var/lib/aegis/logs/aegis.log` (rotated 10 MB × 5) |
| pause all research | Research → Pause research | — |
| budgets | System → Budgets & quotas | edit env file + restart |
| stop | System → Graceful shutdown | `sudo systemctl stop aegis` |
| start | — | `sudo systemctl start aegis` |

The service starts at boot, restarts on crash (at most 5 times in 10 minutes), keeps tasks across restarts, resumes interrupted work only when that is safe, and pauses anything else for your review.

## Automatic maintenance (worker)

* **Every minute:** CPU, memory, disk and temperature metrics. If temperature is ≥ `MAX_CPU_TEMP_C`, free disk is < `MIN_FREE_DISK_MB`, memory is ≥ `MAX_MEMORY_PERCENT` or load is high, it **pauses** starting new objectives and research and sends a critical notification. It resumes automatically.
* **Every 24 h (`BACKUP_INTERVAL_HOURS`):** an online SQLite backup, gzipped, chmod 600, with a SHA-256 sidecar and a **verification restore** (integrity check plus a schema check). The newest `BACKUP_KEEP` backups are kept, and a failure raises a critical notification.
* **Daily:** database integrity check, audit hash-chain check, retention, orphan cleanup and the daily research report (Research → Daily report).
* **Weekly (Sunday):** WAL checkpoint, VACUUM and FTS optimise.

Retention removes only operational telemetry: metrics after 30 days, captured tool output after 90 days (metadata kept), expired sessions, old login attempts and read notifications after 90 days. **Knowledge is never deleted for age.** Old sources are marked stale and their claims `outdated`, with history kept.

## Backups and restore

```bash
sudo /opt/aegis/scripts/backup.sh                                   # or System → Back up now
$A verify-backup /var/lib/aegis/backups/aegis-20261008T000000Z.db.gz
sudo /opt/aegis/scripts/restore.sh /var/lib/aegis/backups/aegis-….db.gz
```

`restore.sh` verifies the backup, stops the service, saves the current database as `aegis.db.pre-restore-<time>`, restores, restarts and health-checks. Backups contain knowledge, tasks and audit history; they do not contain the env file (secrets). Copy `/etc/aegis/aegis.env` separately to a safe place. For off-device copies, download from System → Backups on the iPad, or rsync `/var/lib/aegis/backups` elsewhere.

## Upgrade and rollback

```bash
cd ~/aegis-src && git pull && cd aegis
sudo ./scripts/upgrade.sh          # backup → keep /opt/aegis.prev → install → migrate → start → health check
sudo ./scripts/upgrade.sh --rollback
```

If the post-upgrade health check fails, `upgrade.sh` rolls back automatically. Migrations only add things. If a migration has already run and you roll back the code, restore the pre-upgrade backup recorded in `/opt/aegis.prev/.pre-upgrade-backup`.

## Uninstall

```bash
sudo /opt/aegis/scripts/uninstall.sh                # keeps /etc/aegis and the data dir (offers a final backup)
sudo /opt/aegis/scripts/uninstall.sh --purge-data   # removes everything
```

This removes the service and its drop-ins, `/opt/aegis`, the AppArmor profile, `/etc/sudoers.d/aegis`, all processes of the `aegis` user and the user itself. It then checks that no units, crontab or processes remain. AEGIS installs no cron jobs, timers or other persistence.

## Resource guidance (Raspberry Pi 5)

| Item | Typical |
|---|---|
| AEGIS service RSS | ~70 MB idle (measured on x86_64 test host; expect similar on ARM64), up to `SANDBOX_MEMORY_MB` (512 MB) extra per sandboxed run |
| systemd caps | `MemoryHigh=1G`, `MemoryMax=1536M`, `CPUQuota=300%`, `TasksMax=256` |
| Database | ~1–3 MB per 100 documents (text + FTS index) |
| CPU | near-idle between steps; research is network-bound; pytest runs use one core |
| Local LLM | a 1–3 B 4-bit model needs ~1–3 GB RAM and runs a few tokens/s on a Pi 5. Planning with small models is noticeably weaker |

Keep the data directory on the SSD rather than a microSD card (write endurance, speed).

## Cost guidance

* AEGIS never sends model requests just to stay busy. Research is deterministic by default (`RESEARCH_USE_MODEL_EXTRACTION=false`), so scheduled research costs **$0**.
* A coding objective usually costs 1 planning call, 0–4 replans and 1 evaluation call. Planning prompts are 2–8k tokens.
* Defaults are hard stops: `DAILY_SPEND_LIMIT_USD=2.00`, `RESEARCH_DAILY_SPEND_LIMIT_USD=0.50`, `MAX_REQUESTS_PER_HOUR=120` and `MAX_TOKENS_PER_OBJECTIVE=200000`. A warning comes at 80 % and the objective pauses at 100 %.
* Set `MODEL_PRICES` with your provider's current prices to get exact cost estimates. Without it, unknown models are charged at a deliberately high placeholder, so limits trigger early and the cost is labelled an estimate.
* `LOCAL_ONLY=true` with `MODEL_PROVIDER=local` means zero API spend.
