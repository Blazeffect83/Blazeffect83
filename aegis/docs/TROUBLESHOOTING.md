# Troubleshooting

Start with `journalctl -u aegis -n 100 --no-pager`, then check System → Health in the dashboard, or run `$A status` (see OPERATIONS.md for `$A`).

| Symptom | Cause | Fix |
|---|---|---|
| `refusing to listen on a non-loopback address without an admin password` | `BIND_HOST` is not loopback and no password is set | `sudo /opt/aegis/.venv/bin/aegis --env-file /etc/aegis/aegis.env set-password` |
| Login page says no password configured | `ADMIN_PASSWORD_HASH` empty | same as above, then `sudo systemctl restart aegis` |
| `too many failed logins` | 5 failures in 15 min | wait 15 minutes |
| `400 Invalid host header` | `ALLOWED_HOSTS` doesn't include the hostname you browse to | add it (e.g. `aegis-pi.tailnet.ts.net`) and restart |
| Logged out immediately on HTTPS | — | set `COOKIE_SECURE=true` only when serving over HTTPS; leave it `false` for plain `http://127.0.0.1` |
| Sandbox: `no sandbox available` / dashboard warning | bubblewrap missing or blocked | `sudo apt install bubblewrap`; on Ubuntu 23.10+ install the AppArmor profile (`sudo install -m644 /opt/aegis/deploy/bwrap-userns.apparmor /etc/apparmor.d/aegis-bwrap && sudo apparmor_parser -r /etc/apparmor.d/aegis-bwrap`) |
| `bwrap: Can't mount proc on /newroot/proc` | a systemd drop-in added `ProtectKernelTunables`, `ProtectKernelLogs`, `ProtectProc` or `ProcSubset` | remove them (they over-mount `/proc`) |
| `bwrap: setting up uid map: Permission denied` | AppArmor userns restriction | see the AppArmor fix above |
| Objective FAILED: `no model provider is configured` | `MODEL_PROVIDER=none` | configure a provider, or give research objectives explicit URLs |
| Objective PAUSED: `daily API spending limit reached` | budget hard stop | raise it under System → Budgets, or wait until 00:00 UTC; then Resume |
| Objective QUEUED with `planning deferred` | provider overloaded or network outage (transient) | retried automatically on the next worker cycle |
| Objective PAUSED: `interrupted during …` | crash or power loss during a non-idempotent step | inspect the workspace (`/var/lib/aegis/workspace/<id>`), then Resume (the step runs again) or Cancel |
| Objectives don't start, Overview shows "resources paused" | temperature, disk, memory or load limit | check cooling and free disk; thresholds are `MAX_CPU_TEMP_C`, `MIN_FREE_DISK_MB`, `MAX_MEMORY_PERCENT`, `MAX_LOAD_PER_CPU` |
| Research source `rejected: … not public` | SSRF protection (internal address) | intended. For your own LAN search service use `SEARXNG_URL` |
| Research source `rejected: domain … not on the allowlist` | global or topic allowlist | add the domain to `RESEARCH_ALLOWED_DOMAINS` or to the topic |
| Research source `robots.txt disallows` | the site disallows crawling | intended; use another source |
| Document flagged "prompt-injection markers" | strong injection signal found | intended; claims are not promoted. If it's a false positive, check which flag appears on the document page and report the phrase |
| `database is locked` in logs | very long external read transaction | AEGIS retries with backoff; stop external `sqlite3` sessions |
| Health `backups ✗` | no verified backup in 2× the interval | System → Back up now; check free disk |
| Audit `CHAIN BROKEN` | the audit table was edited outside AEGIS | investigate: the DB was modified with external tools. Restore a backup if needed |
| `pip_install` approved but fails | no network to the index, or package build failure | check `PACKAGE_INDEX_URL`; the tool output appears in the task detail |
| `service_restart` fails with sudo error | expected default (`NoNewPrivileges=yes`) | see SECURITY.md → Service control |
| Deep research stops quickly with "sources exhausted" | discovery limited to Wikipedia/OpenAlex for a practical or niche topic | add `BRAVE_SEARCH_API_KEY` or `SEARXNG_URL`, add seed URLs (e.g. vendor docs), or rephrase the topic with its key terms |
| Deep research report has few "Key findings" but much "Background" | sources discuss parts of the topic, not the whole | expected and honest; widen or rephrase the topic, or add seed URLs |
| Deep research discovery errors in the activity log | global `RESEARCH_ALLOWED_DOMAINS` excludes the discovery APIs | add `api.wikimedia.org`, `wikipedia.org`, `api.openalex.org` |
