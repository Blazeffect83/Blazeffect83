# AEGIS — autonomous research & execution agent

AEGIS runs on a Raspberry Pi 5 around the clock. You give it an objective from your iPad. It plans the work, carries out the permitted steps, checks the results, and keeps a growing knowledge base of what it learns from the sources it reads and from its own successes and failures.

```
OBSERVE → REASON → PLAN → ACT → VERIFY → LEARN → REPEAT
```

* **Plans and executes.** It turns vague goals into validated, testable steps. It retries transient errors within limits, replans on real failures, and asks you when a requirement is missing.
* **Verifies its work.** Deterministic checks (exit codes, tests passing, files present) decide success. A model "judge" can send work back for another round, but it can never pass a check that failed.
* **Researches a topic in depth, for hours.** Give it a topic and a time budget. It breaks the topic into sub-questions, searches Wikipedia, OpenAlex (scholarly abstracts), and optionally your SearXNG instance or Brave web search. It follows citations and relevant links, snowballs through papers that cite good sources, and hunts for corroboration of single-source claims. It filters off-topic material and detects diminishing returns. The result is a **cited report**: answers per sub-question, key findings with confidence, contested points, background, gaps and a numbered source list. It runs in time slices, survives pauses and reboots, and never blocks other work.
* **Researches continuously.** It reads RSS/Atom feeds, seed URLs and optional self-hosted SearXNG search. Fetching is SSRF-safe. It scores source quality, removes duplicates, detects prompt injection, extracts source-backed claims with citations, flags contradictions (it never silently overwrites them), and writes daily reports.
* **Learns without retraining.** "Learning" here means saved, source-backed knowledge plus a record of past experiments (what worked and what failed, in which context). It looks these up before every plan. The model's weights never change.
* **Safe by design.** Tools fall into four permission tiers. Code runs in a bubblewrap sandbox with no network, no secrets, uid 65534 and only its own workspace writable. Approvals are single-use and bound to the exact action. The audit log is append-only and hash-chained. All retrieved content is treated as untrusted data.
* **Controllable from an iPad.** A responsive dashboard (tested in iPad-sized Chromium) covers objectives, plans, approvals, research, knowledge search, contradictions, reports, budgets, backups, logs and shutdown.
* **Bounded cost.** It enforces a daily spend limit, a per-objective token budget and an hourly request cap, all as hard stops. A local-only mode turns off paid APIs completely. It never falls back to a paid provider unless you configure that.

## Quick start (on the Pi)

```bash
git clone <this repo> && cd <repo>/aegis
sudo ./scripts/install.sh                 # interactive; asks before every privileged change
sudoedit /etc/aegis/aegis.env             # set MODEL_PROVIDER / MODEL_NAME / API key (optional)
sudo systemctl restart aegis
```

To use the external SSD for data: `sudo ./scripts/install.sh --data-dir /mnt/ssd/aegis`.
For the full walkthrough (Ubuntu on SSD, AppArmor, Tailscale for the iPad, a local model) see **[docs/INSTALL.md](docs/INSTALL.md)**.

## Operating entirely from the iPad

You need the server once, to run the installer and join it to Tailscale. After that, everything happens in Safari:

1. **Connect.** Install Tailscale on the iPad and open `https://<pi-name>.<tailnet>.ts.net`. In Safari choose *Share → Add to Home Screen* to get an app-like icon.
2. **Submit work.** On *Overview* or *Tasks*, type a goal (for example *"Research and compare local LLM runtimes for Raspberry Pi 5"*). You can add success criteria, constraints, a spend cap, a time budget and allowed tools.
3. **Watch.** *Tasks → objective* shows the live state (it refreshes itself), the plan versions, each step's arguments, results and verification, the full state history and the tool runs. *Why:* explains every wait or stop.
4. **Approve.** *Approvals* lists every tier-2 request: what it will do, why, what it affects, possible consequences, whether it can be undone, and the exact command. Approve once or reject.
5. **Answer questions.** If the agent needs clarification, the objective pauses and shows the question with an answer box.
6. **Control.** You can pause, resume, cancel or retry any objective. *Research* lets you pause or resume all research, add topics (feeds, seed URLs, per-topic domain allowlists, frequency) and run a topic now.
7. **Knowledge.** Search documents and claims. Open a claim to see its evidence passages, sources, corroboration, contradictions and history. You can resolve contradictions and export everything as JSON or Markdown.
8. **System.** Health checks, resource history, budgets and quotas (editable), backups (create, verify, download), the audit log with hash-chain status, redacted logs, the tool registry and graceful shutdown.

Research and objectives keep running with the iPad closed. The worker lives in the service, not in the browser.

## Deep research (topic → hours of research → cited report)

On the iPad, open **Research → Deep research**, enter a topic (for example *"Effects of intermittent fasting on insulin sensitivity"*), choose the hours and sources, and start. You can also use the API (`POST /api/research/deep`) or the CLI (`aegis research "topic" --hours 4`).

| Phase | What happens |
|---|---|
| Questions | Sub-questions and search queries: from the model if one is configured, otherwise templates, plus terms mined from findings |
| Discovery | Wikipedia (Wikimedia API), OpenAlex scholarly abstracts, SearXNG and Brave web search when configured. Only APIs whose robots.txt permits automated access are used |
| Ingestion | SSRF-safe fetching, comment and boilerplate removal, dedupe, injection screening, claim extraction, quality scoring |
| Relevance | The topic is split into concepts ("thermal throttling", "cooling", "Raspberry Pi"). Material must address several of them, so pages sharing only a name or an aspect are kept but never cited |
| Expansion | Citation and reference links, relevant outbound links (bounded depth), papers citing on-topic papers (snowballing), searches for related titles |
| Verification | Targeted searches for corroboration of single-source claims; contradictions between sources are flagged |
| Stopping | Time budget used, document limit reached, sources exhausted, or diminishing returns (new sources stop adding knowledge) |
| Report | Executive summary (model-written, only when a model is set, citations validated), answers by sub-question, key findings, background, contested points, definitions, procedures, limitations, gaps, method statistics, sources |

The **Interim report** button shows results while research is still running. Progress, budget used, sub-questions, next searches and an activity log appear on the objective page.

**How far it reaches.** Without a web search API, discovery is encyclopedic and scholarly: Wikipedia, OpenAlex and the links they cite. That is strong for science and technology topics. Topics covered mainly by vendor docs, forums or news run out of sources sooner, and AEGIS says so ("sources exhausted"). For hours of web-scale research, set `BRAVE_SEARCH_API_KEY` (free tier) or `SEARXNG_URL`.

## Configuration

Everything goes in `/etc/aegis/aegis.env`. Every key is documented in [`.env.example`](.env.example). Model identifiers are **never hardcoded**: choose a current one from the provider's model list (links are in the example file). With `MODEL_PROVIDER=none`, AEGIS still runs research objectives that include explicit URLs, using a deterministic planner and extractor.

| Provider | Settings |
|---|---|
| Anthropic | `MODEL_PROVIDER=anthropic`, `MODEL_NAME`, `ANTHROPIC_API_KEY` |
| OpenAI | `MODEL_PROVIDER=openai`, `MODEL_NAME`, `OPENAI_API_KEY` |
| Local (Ollama / llama.cpp / any OpenAI-compatible) | `MODEL_PROVIDER=local`, `LOCAL_MODEL_BASE_URL`, `MODEL_NAME`, `LOCAL_ONLY=true` |

## Repository layout

```
app/
  agent/         controller (loop), planner, executor, evaluator, retry policy, state machine, objectives
  models/        provider interface, Anthropic, OpenAI, local (OpenAI-compatible), mock, router + budgets
  research/      fetcher (SSRF-safe), parser, extractor, injection screening, quality rubric, dedupe, engine
  knowledge/     store (claims/evidence/contradictions/history), FTS5 search, retrieval, reports
  tools/         registry + filesystem, sandboxed python/tests/pip, research/knowledge, system tools
  security/      policy tiers, approvals, sandbox (bubblewrap), network policy, redaction
  scheduler/     background worker, research schedules, resource guard, crash reconciliation
  observability/ audit log (hash chain), metrics, health
  maintenance/   backups/restore, retention, environment report
  api/ dashboard/  authenticated JSON API and server-rendered iPad dashboard
  migrations/    SQLite schema (FTS5, foreign keys, indexes, append-only audit triggers)
deploy/          systemd unit, AppArmor profile for bubblewrap, sudoers example
scripts/         install, uninstall, upgrade/rollback, backup, restore
config/          sample research topics
tests/           236 automated tests (unit, integration, security, end-to-end, crash)
docs/            install, security, API, operations, troubleshooting, architecture, verification
```

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m pytest
```

No test makes a paid API call or touches a real third-party system. Models are scripted mocks and the web is an in-process fake. The tests include a real `SIGKILL` crash in the middle of an action, real bubblewrap escape attempts, and a DNS-rebinding attack against a real local socket. [docs/VERIFICATION.md](docs/VERIFICATION.md) records what was verified, where, and what still needs checking on the Pi itself.

## Documentation

* [INSTALL.md](docs/INSTALL.md) — Pi preparation, install, model setup, iPad access, HTTPS
* [SECURITY.md](docs/SECURITY.md) — threat model, tiers, sandbox, SSRF, prompt injection, approvals, audit, secrets
* [ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, control loop, state machine, data model, learning
* [API.md](docs/API.md) — REST endpoints and authentication
* [OPERATIONS.md](docs/OPERATIONS.md) — backups, restore, upgrade, rollback, uninstall, resource and cost guidance
* [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
* [VERIFICATION.md](docs/VERIFICATION.md) — test evidence and the Definition-of-Done checklist

## Why systemd and no Docker

The sandbox relies on unprivileged user namespaces (bubblewrap). Running it inside Docker would need a privileged or seccomp-relaxed container, which is weaker than the plain systemd service. A systemd unit also reads Pi temperatures and metrics directly and restarts on failure without another layer. So there is one layer: a hardened systemd unit ([deploy/aegis.service](deploy/aegis.service)).
