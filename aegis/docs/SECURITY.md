# Security model

Security is built into the architecture. The model **proposes** actions; deterministic code **decides** whether they run. Nothing the model or retrieved content says can change tiers, allowlists, approvals, budgets or the audit log.

## Trust boundaries

| Input | Trust | Handling |
|---|---|---|
| You (dashboard/API, authenticated) | trusted | server-side auth + CSRF on every request |
| Model output | untrusted proposal | plans validated in code: known tools only, schema-validated arguments, allowed-tools filter, step limits |
| Web pages, feeds, search results | untrusted data | SSRF-safe fetch, parsed as data, injection screening, wrapped in `<untrusted_document>` when shown to a model, never given tool access |
| Tool output (incl. sandboxed code) | untrusted data | redacted, size-capped, wrapped in `<untrusted_tool_output>` when fed back for replanning |
| Packages (pip) | untrusted code | approval required; installed and run only inside the sandbox |

## Permission tiers

| Tier | Meaning | Examples | Enforcement |
|---|---|---|---|
| 0 | read-only | knowledge search, metrics, file_read, research_ingest (public web) | runs autonomously |
| 1 | restricted execution | file_write (workspace), python_run, run_tests, knowledge notes | runs autonomously inside workspace/sandbox limits |
| 2 | human approval | pip_install, python_run with `network=true`, service_restart | blocked until you approve that exact action |
| 3 | prohibited | attacking third-party systems, stealing credentials, malware, evading monitoring, self-replication, changing its own permissions | no tool exists; the registry refuses tier-3 registrations; obviously out-of-scope objectives are refused before planning |

Tiers are fixed in code (`app/tools/*.py`). Each tool declares a name, purpose, input/output schema, permissions, timeout, resource limits, reversibility, idempotency and audit level. Some tools escalate automatically: `python_run` becomes tier 2 when `network=true`. After startup the registry is **frozen**. No tool can read or write the config file, the database, approvals or the audit log (tests: `test_every_tool_declares_contract`, `test_config_and_database_not_reachable_from_tools`).

## Approvals

For each tier-2 action you see: the proposed action, why it is needed, what it affects, possible consequences, whether it can be undone, the exact operation, its arguments and the permission scope.

* **Bound to the exact action:** SHA-256 of tool name plus canonical arguments. Changing a single argument invalidates it.
* **Single use:** consumed atomically on execution.
* **Expiring:** `APPROVAL_TTL_MINUTES` (default 60) for both pending and approved requests.
* **Never broad:** no "always allow". A new action needs a new approval.
* Rejected or expired approvals never execute (tests in `test_tools_security.py` and `test_agent.py`).

## Sandbox (bubblewrap)

`python_run`, `run_tests` and `pip_install` run inside `bwrap` with:

* all namespaces unshared (user, PID, network, IPC, UTS, cgroup); **network off** unless an approved action enables it
* uid/gid 65534 inside the namespace; `--die-with-parent`; `--new-session` (blocks TIOCSTI)
* root filesystem **read-only** (`--remount-ro /`); only `/usr`, `/etc` essentials and the Python runtime are mounted, read-only
* the **objective's own workspace** is the only writable host path; private tmpfs `/tmp`
* **cleared environment**: no API keys, no `.env`, no data directory, no `/home`, no Docker socket, no host processes visible
* rlimits on address space (`SANDBOX_MEMORY_MB`), CPU time, file size, open files and processes (when not root), plus a wall-clock timeout that kills the whole process group
* stdout/stderr capped at 256 KB

Escape attempts that fail in the tests: reading env secrets, reading the data dir and `.env`, writing to `/usr`, `/etc`, `/root`, `/home`, seeing host PIDs, network connections, memory bombs, runaway output, infinite loops.

**Weak fallback.** `SANDBOX_BACKEND=rlimit` gives rlimits and a cleared environment, but no filesystem or network isolation. It is refused unless you set `SANDBOX_ALLOW_WEAK=true`, and the dashboard shows a warning. Do not use it on a machine that matters.

**AppArmor trade-off (Ubuntu 23.10+).** Ubuntu restricts unprivileged user namespaces. The optional profile `/etc/apparmor.d/aegis-bwrap` allows `/usr/bin/bwrap` (only) to create them. This is the mechanism Ubuntu uses for browsers. The cost: any local user can then run bwrap with user namespaces, which slightly widens the kernel attack surface. The alternative is no code execution.

## Web fetching (SSRF)

* Only http/https, ports 80/443, no credentials in URLs.
* Blocked: localhost and loopback, RFC 1918, link-local (including `169.254.169.254` cloud metadata), CGNAT, multicast, reserved and unspecified addresses, IPv4-mapped/6to4/Teredo IPv6, `.local`/`.lan`/`.internal` names, and numeric encodings such as `2130706433` or `0x7f000001`.
* **Connect-time validation.** A custom network backend resolves the hostname, checks *every* address, and connects to the vetted IP. TLS still verifies the original hostname. DNS rebinding between check and connect therefore cannot reach an internal service (test: `test_dns_rebinding_blocked_at_connect_time`, against a real local socket).
* Redirects are followed manually and each hop is re-checked against the full policy and allowlists.
* Limits on response size, connect and read timeouts and content types. Proxies from the environment are ignored, robots.txt is respected, and requests to each domain are paced.
* Domain allowlists and blocklists apply globally (`RESEARCH_ALLOWED_DOMAINS`) and per topic. The optional SearXNG backend may live on the LAN, so it gets a pinned-origin policy that allows exactly that scheme, host and port.

## Prompt injection

* Research processing never has tool access. A web page cannot cause a tool call, an approval or a config change (test: `test_prompt_injection_page_is_inert`).
* Heuristic screening separates **strong** signals (AI-addressed instruction overrides, role hijacks, secret-exfiltration requests) from **weak** ones (imperatives that are normal in docs, such as "run this command"). A page is flagged if it contains a strong signal, if weak signals appear in CSS-hidden text, or if several weak signals appear together. Flagged documents are stored for inspection, down-ranked, and **their claims are not promoted** into the knowledge base. A live run against python.org and sqlite.org documentation produced false positives with an earlier, cruder rule set; the current rules are regression-tested against those exact phrases.
* When model-assisted extraction is on, every claim must quote a passage found verbatim in the document. Hallucinated claims are dropped.

## Authentication and web security

* Single admin user. The password is stored as an scrypt hash (N=2¹⁴, r=8, p=1).
* Sessions are server-side; only a SHA-256 of the session id is stored. Cookies are `HttpOnly` and `SameSite=Strict`, plus `Secure` when `COOKIE_SECURE=true`. Default TTL is 12 h; logout invalidates the session.
* CSRF: a per-session token is required for every state-changing request authenticated by cookie (hidden form field or `X-CSRF-Token`).
* Login rate limit: 5 failures per client per 15 minutes.
* API bearer tokens: only the SHA-256 is stored; the token is shown once.
* Headers: strict CSP (no inline script), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `no-store`, and HSTS when secure.
* Every sensitive endpoint checks authorisation on the server. Hiding a button is never the control (tests in `test_api.py` check every route).
* Binds to `127.0.0.1` by default, and refuses non-loopback binding without a password. Use a VPN for remote access (INSTALL.md §6).
* Request bodies are capped at 2 MB, with Pydantic validation on every input.

## Audit trail

`audit_events` records timestamp, actor, objective and task IDs, tool, validated arguments, permission decision, approval reference, result, exit code, duration, error and resource use. Credentials, keys, auth headers and values that look like secrets are redacted first.

* SQLite triggers reject `UPDATE` and `DELETE` on the table.
* Each row stores the hash of the previous row. `aegis verify-audit` and the dashboard detect out-of-band edits, and the daily maintenance run raises a critical notification if the chain is broken.

## Secrets

* Secrets live only in `/etc/aegis/aegis.env` (640 `root:aegis`) and in process memory. They are never written to the database, logs, notifications, tool output or audit rows; a redactor checks known secret values plus common patterns.
* `/api/config` and the dashboard show `***set***` instead of values.
* The sandbox cannot see the env file, the environment or the data directory.

## Service control (optional, off by default)

`service_restart` (tier 2) runs `sudo -n /usr/bin/systemctl restart <service>` for services listed in `APPROVED_SERVICES`. It is inert by default because the unit sets `NoNewPrivileges=yes`, which blocks sudo. To enable it:

1. Add a sudoers rule naming each exact command (`deploy/sudoers.aegis.example`).
2. Add a drop-in `/etc/systemd/system/aegis.service.d/20-sudo.conf` containing `[Service]` and `NoNewPrivileges=no`.

Every restart still needs a per-action approval.

## Known limitations

* The tier-3 objective screen is a keyword filter, and a determined user can rephrase around it. The real guarantee is structural: no tool can act on third-party systems or credentials, and the sandbox has no network unless you approve it. With network approved, sandboxed code can reach the internet, so approve only what you understand.
* Prompt-injection detection is heuristic. It lowers risk; architectural isolation is what prevents harm.
* Cost accounting is an estimate unless `MODEL_PRICES` is set. Unknown prices use a deliberately high placeholder so limits trigger early.
* A long-running sandboxed step finishes or times out before a pause or cancel takes effect.
