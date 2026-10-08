# Verification record

This file separates **what has been verified** from **what still has to be verified on the target Raspberry Pi**. Nothing here is claimed without evidence.

## Build/test host

The development host was a cloud container, **not the Pi**: Ubuntu 24.04.5 LTS, x86_64, kernel 6.18, 4 vCPU, 16 GB RAM, Python 3.13.16, SQLite 3.45.1 (FTS5), bubblewrap 0.9.0. No systemd as PID 1 and no Docker daemon.

## Automated tests — 214 passing (`python -m pytest`, ~25 s)

| File | Tests | Covers (spec Part 13) |
|---|---|---|
| `test_foundation.py` | 18 | config parsing (inline comments, quotes, `.env.example` validity), migrations, foreign keys, **database lock retry**, secret redaction, append-only + hash-chained audit (tamper detection), state transitions, compare-and-set vs. user pause, terminal states |
| `test_models.py` | 17 | Anthropic/OpenAI/local request shapes, structured output, tool calls, **429 + Retry-After**, auth errors not retried, timeouts bounded, no default model id, **daily spend hard stop (no request sent)**, hourly cap, per-objective token budget, unknown-price estimate, **local-only blocks paid**, **no silent fallback**, explicit fallback, light/heavy routing |
| `test_network_security.py` | 37 | 24 internal/unsafe URL forms rejected (localhost, RFC1918, metadata IP, IPv6 loopback/mapped, numeric encodings, `.local`, file/ftp/gopher, credentials, ports), allowlist suffix tricks, resolver rejects private answers, **DNS rebinding blocked at connect time against a real socket (server never hit)**, redirect to metadata IP, redirect limits, off-allowlist redirect, size/content-type limits, robots.txt, transient classification, pinned search origin, safe transport is the default |
| `test_research.py` | 31 | HTML parsing (scripts/nav/hidden text), source line breaks, RSS, **XML entity-expansion bomb rejected**, deterministic extraction (definitions, procedures, limitations, dates, questions), verbatim-passage check, exact/near dedupe, transparent quality rubric, injection detection (hostile flagged, **real python.org/sqlite.org phrasing not flagged**), end-to-end ingestion with evidence, cross-domain corroboration, **contradiction flagged not overwritten + resolution history**, duplicates not counted as corroboration, **prompt-injection page inert (no tool runs, approvals or model calls)**, model claims without support dropped, feed discovery, rejections recorded, **report counts equal DB counts**, search filters, FTS operator injection, retrieval references, stale marking without deletion |
| `test_tools_security.py` | 32 | frozen registry, tier-3 unregistrable, tool contracts, no permission-changing tools, tier decisions + escalation, out-of-scope objective screen, **approval bound to exact args / single-use / expiry**, file roundtrip, **path traversal (6 forms)**, **symlink escapes**, DB/logs unreachable, log/service allowlists, **sandbox: env secrets invisible, `.env`/DB/Docker socket invisible, uid 65534, network blocked, root FS read-only (host unaffected), host PIDs hidden, timeout kill, memory limit, output cap**, weak sandbox refused unless allowed, workdir confinement, pip argument injection, pytest result parsing |
| `test_agent.py` | 22 | **coding objective: generate code → tests fail → diagnose from output → fix → tests pass → independent verification** (+ learning records), **state persists across restart**, research objective with no model, clear failure without a model, **approval gate blocks until approved, then executes once**, rejected approval never executes, expired approval re-requested, prohibited objective blocked before planning, disallowed tool rejected, plan repair, malicious plan arguments contained, clarification flow, **budget exhaustion pauses**, per-objective spend budget, pause/resume, cancel, time budget, replans exhausted, transient tool failure retried, provider outage deferral, model judge can force replan, **model cannot override a failed deterministic check** |
| `test_operations.py` | 14 | **real SIGKILL mid-action**: non-idempotent step paused for review (not re-run); idempotent step resumed under a new idempotency key and completed; interrupted planning requeued; finished-but-unrecorded step not repeated; worker runs objectives and research without the dashboard; background thread lifecycle; research pause; **resource pressure pauses work and notifies**; failing topic back-off; **backup → verify → restore round trip**; corrupted backup detected and refused; rotation; retention keeps knowledge; maintenance tick; export and delete |
| `test_api.py` | 43 | **every API GET/POST route returns 401 without auth**; dashboard pages redirect; dashboard POSTs need auth; minimal public health; bearer tokens; **login rate limiting**; cookie flags; **CSRF required**; all 12 pages render with real data; approve/reject through the UI writes an audit record; security headers; body-size limit; input validation; logout and expired sessions; secrets never in the API; budget updates |

The tests use scripted mock models and an in-process fake web. **No paid API calls, and no exploit is tried against a real third-party system.**

## Live verification on the build host

1. **Real server process** (`aegis serve` via the CLI, worker thread, `/health` → `{"ok": true}`). Unauthenticated `/api/status` → 401.
2. **Real-internet research** (only the public docs at sqlite.org and python.org) through the SSRF-safe transport. TLS verified; `169.254.169.254` was rejected. A research objective submitted over the authenticated API was planned, executed and verified by the background worker: 2 documents (quality 82 and 68), 15+ source-backed claims, clean summaries.
3. **Bugs found by the live runs and fixed, each with a regression test:**
   * `aegis --env-file … serve` ignored the file;
   * inline `# comments` in the env file broke parsing;
   * injection heuristics flagged ordinary documentation phrasing ("execute the … command", "act as", "call this function"), which blocked claims from official docs;
   * HTML source line breaks split sentences;
   * table-of-contents fragments leaked into summaries;
   * the uninstaller aborted on hosts without systemd.
4. **Dashboard in Chromium with Playwright device emulation:** iPad Pro 11 portrait (light), iPad Pro 11 landscape (dark) and iPhone 13. 11 pages per device: no horizontal overflow, no JS errors, primary buttons 44 px tall. An objective was submitted with tap input. Two layout defects were found and fixed (word-breaking in tables; state history clipped in half-width cards).
5. **Sandbox:** after an escape attempt, `/etc/evil` was not created on the host. No orphaned sandbox processes remained after the SIGKILL test.
6. **Installer** (`install.sh --yes --no-systemd`) ran on the host: pre-flight, user creation, code copy, venv + pip install, config creation, data dir. Its first run stopped at `aegis init` because of the env-comment bug above, which is fixed. A second end-to-end run was blocked by the session's permission policy (system-wide install), so **a full successful run of the installer is not yet verified**. **`uninstall.sh --purge-data` was verified**: it removed the user, `/opt/aegis`, `/etc/aegis` and the data dir, and reported no units, crontab or processes.
7. Lint: `ruff check app tests` is clean.
8. **systemd hardening and bubblewrap:** a test reproduced that a `/proc` overmount, as created by `ProtectKernelTunables` and similar settings, makes `bwrap --proc` fail. Those directives are therefore excluded from the unit, with a comment explaining why.

## Definition of Done — status

| # | Requirement | Status |
|---|---|---|
| 1 | Runs on the target Raspberry Pi | ⏳ **Needs on-device check.** Pure Python plus the stdlib; all dependencies ship ARM64 wheels; tested on Ubuntu 24.04 x86_64 only |
| 2 | Submit objective from dashboard | ✅ live (Playwright, iPad emulation) and tests |
| 3 | Agent creates and executes a plan | ✅ tests (mock model) and live (deterministic research planner); ⏳ with your real model provider |
| 4 | Approved web research | ✅ live against sqlite.org / python.org |
| 5 | Stores/retrieves source-backed knowledge | ✅ tests and live |
| 6 | Restricted development task | ✅ tests (real bwrap sandbox, real pytest) |
| 7 | Tests and verifies its own work | ✅ tests |
| 8 | Recovers from bounded failures | ✅ tests (retry, replan, provider outage, crash) |
| 9 | State survives reboot | ✅ restart and SIGKILL tests; ⏳ real power-cycle on the Pi |
| 10 | Research continues without iPad | ✅ worker tests and live (the objective ran with no browser attached) |
| 11 | Resource and spending limits work | ✅ tests |
| 12 | Dangerous operations blocked or gated | ✅ tests |
| 13 | Dashboard authentication | ✅ tests and live |
| 14 | Backups restore successfully | ✅ tests |
| 15 | Automated tests pass | ✅ 214/214 |
| 16 | Installation and maintenance docs | ✅ README + docs/ |
| 17 | Clean stop and removal | ✅ uninstall verified on the host; ⏳ systemd unit removal on a systemd host |

### On-device checklist (≈15 minutes, after `install.sh`)

```bash
systemctl is-enabled aegis && systemctl is-active aegis          # enabled / active
curl -s http://127.0.0.1:8600/health                               # {"ok": true}
sudo -u aegis AEGIS_ENV_FILE=/etc/aegis/aegis.env /opt/aegis/.venv/bin/aegis status | grep -A3 sandbox   # "strong_isolation": true
cd /opt/aegis && sudo -u aegis PYTHONDONTWRITEBYTECODE=1 /opt/aegis/.venv/bin/python -m pytest -q -p no:cacheprovider \
   -o cache_dir=/tmp/x tests/test_tools_security.py                 # sandbox tests on the Pi kernel
sudo reboot   # then: the objective/knowledge counts on Overview are unchanged and research resumes
```

On the iPad: sign in over Tailscale, submit *"Research Raspberry Pi 5 active cooling using https://www.raspberrypi.com/documentation/computers/raspberry-pi.html"*, wait for COMPLETED, then open Research and confirm the document and its claims appear.
