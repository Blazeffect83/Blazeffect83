# Architecture

```
iPad Safari ──HTTPS (Tailscale)──▶ FastAPI: dashboard + JSON API (auth, CSRF)
                                        │
                     ObjectiveManager ──┤── Approvals, Budgets, Research topics
                                        ▼
               Worker thread (always on) ── schedules · metrics · backups · maintenance
                   │                         │
                   ▼                         ▼
          Agent Controller             Research Engine
   plan → act → verify → learn     discover → fetch → parse → dedupe → screen → extract → score → store
          │                                  │
   Planner ── Model Router (budgets, usage) ─┘
          │
   Executor ── PolicyEngine (tiers) ── Approvals (exact-action, single-use)
          │
   Tool Registry (frozen) ── filesystem · sandbox (bwrap) · research · knowledge · system
          │
   SQLite (WAL): objectives · tasks · task_events · documents(+FTS5) · claims(+FTS5) · evidence · history ·
                 relationships · experiments(+FTS5) · tool_runs · approvals · model_usage · metrics · audit
```

Everything runs in one process (`aegis serve`): uvicorn serves the web app, one background worker thread does scheduling, objective runs use a small thread pool (`CONCURRENT_OBJECTIVES`, default 1 on a Pi), and research has a single thread. Separate services would add moving parts without helping a single-board deployment.

## Control loop (`app/agent/controller.py`)

1. **Observe.** Load the objective and its checkpoint (the persisted state and tasks). Check the time budget, step limit and spend budget.
2. **Reason and plan.** Screen the goal against tier-3 rules. Retrieve relevant claims, documents, previous attempts and known failures (FTS5), each with references such as `claim:12`. The planning model returns JSON (tools with exact schemas and deterministic checks), and code **validates** it: unknown tools, bad arguments and disallowed tools are rejected, with one repair round. Missing requirements trigger a clarification question and a pause.
3. **Act.** The next READY task goes to the executor: argument validation, then the policy decision (allow / approval / deny), then the approval check, then the idempotency key, then a `tool_runs` row (`started`), then execution, then output capture with redaction and the audit record.
4. **Verify.** Deterministic checks run: `ok`, `exit_code`, `tests_pass`, `output_contains`, `regex`, `file_exists` and `min_results`. "Attempted" never counts as "succeeded".
5. **Learn.** Every step writes an `experiment` (context, procedure, outcome, lesson, error class). After an objective recovers through replanning, a summary experiment records the failure and its resolution. The next plan retrieves these.
6. **Repeat or recover.** The retry policy classifies each error: *transient* (timeout, network, rate limit) is retried with exponential backoff if the tool is safe to repeat; *persistent* (test failure, non-zero exit, bad arguments) triggers a replan that includes the error output; *blocking* (policy, rejected approval, budget, no sandbox) stops the run. Tier-2 and non-idempotent actions are never repeated automatically.
7. **Finish.** When all tasks, including the `[verify]` final checks, have passed, an optional model judge reviews the success criteria. It can request another round but cannot pass failed checks. The objective is then COMPLETED, with an evidence report.

**Stopping conditions:** success; impossible (no plan or replans exhausted); time budget; token or spend budget (→ PAUSED); repeated failures; approval needed (→ WAITING_FOR_APPROVAL); policy conflict; user cancel or pause.

## State machine (`app/agent/state_machine.py`)

`QUEUED → PLANNING → READY → RUNNING → (VERIFYING | RETRYING | WAITING_FOR_APPROVAL | PLANNING) → … → COMPLETED | FAILED | CANCELLED`, with `PAUSED` reachable from any non-terminal state.

Every transition is validated against an explicit table, persisted in `task_events` with a reason, and applied with **compare-and-set**. If the worker expects RUNNING but you have just paused the objective, the worker's write does nothing and it stops. A user's pause or cancel can never be overwritten.

## Crash safety

* A `tool_runs` row with an idempotency key (objective, task, attempt) is committed **before** a tool runs. If the process dies after the tool finished but before state was saved, the recorded result is reused rather than run again.
* On startup `reconcile()` handles interrupted work. Runs that were `started` and belong to idempotent tier ≤1 tools are retried under a new attempt. Anything else is **paused for review**, with the reason "interrupted during an action that may not be safe to repeat". Interrupted planning goes back to QUEUED. Tested with a real `SIGKILL` mid-action (`test_operations.py`).
* SQLite uses WAL, `synchronous=NORMAL`, a 30 s busy timeout and lock retries.

## Learning — what it is and is not

AEGIS does **not** fine-tune model weights. It improves by:

1. Acquiring documents from permitted sources and extracting claims, definitions, procedures, limitations, dates and open questions.
2. Storing each claim with **evidence passages** and source references. Its *corroboration* counts independent domains only: duplicates, near-duplicates and same-publisher pages don't count.
3. Tracking confidence with a transparent rubric (corroboration, primary sources, experiments). It is not a probability.
4. Flagging contradictions (negation or numeric conflicts) as `contradicts` relationships and marking both claims `contested`. You resolve them. Every status change is kept in `claim_history`.
5. Marking claims `outdated` when every supporting source falls outside the freshness window. The history is kept and nothing is deleted for age.
6. Recording experiments (what was tried, in which context, the outcome and the lesson) and retrieving them before planning.
7. Keeping agent inferences (`origin='agent'`) separate from source statements.

## Research pipeline (`app/research/`)

Discovery (feeds, seed URLs, SearXNG) → `research_sources` queue with method and origin → SSRF-safe fetch (`fetcher.py`) → parse (`parser.py`: HTML without scripts, nav or hidden text; RSS/Atom/JSON Feed via defusedxml) → exact dedupe (SHA-256 of normalised text) and near dedupe (64-bit SimHash, Hamming ≤ 6) → injection screen → extraction (deterministic, or the model with verbatim-passage verification) → quality score (`source_quality.py`, an itemised breakdown) → store documents, topics, claims and evidence → contradiction detection → stale marking. Daily reports count only rows that exist in the database.

## Deep research campaigns (`app/research/campaign.py`)

A campaign is an objective of kind `deep_research`. Its whole state lives in the objective's checkpoint: parameters, sub-questions, query queue, URL frontier, visited set, cited document ids, per-domain counts, term statistics, yields and log. Every unit of work is checkpointed, so pause, resume, crash and reboot continue exactly where they left off, and nothing is fetched twice.

* **Time slices.** The controller runs a campaign for `slice_seconds` (default 300 s, always at least one unit), then hands it back as READY. The worker schedules by priority, then least-recently-updated, so other objectives run between slices. Budget is measured in *active* research time.
* **Units of work.** A unit is either one search (across all enabled providers) or one candidate fetch. Searches and fetches are interleaved; refreshes of questions and verification happen every 15 or 30 cited documents and on saturation.
* **Relevance model.**
  * The topic is split into concepts at function words. A concept counts in full when all its words occur (light stemming), and half when only its head word occurs.
  * *Core* material covers essentially the whole topic. *Background* covers part of it. Anything else is off-topic: kept in the knowledge base but never cited, and its links are not followed.
  * Topic-term weights adapt to document frequency within the campaign. Extraction ranks sentences by the within-document rarity of query terms, and long documents get up to 40 claims.
* **Discovery providers** (`app/research/discovery.py`): Wikimedia search API, OpenAlex (search and `cites:` snowballing; abstracts are ingested directly, and independence is tracked per DOI-prefix publisher), SearXNG, Brave Search API. Wikipedia's `/w/api.php` and arXiv's export API disallow crawlers in robots.txt, so they are not used.
* **Saturation.** If the last 20 documents together add fewer than 3 new or reinforced claims, new questions are generated. A third consecutive strike, or a refresh that yields nothing new, ends the campaign ("diminishing returns").
* **Model use** (optional). The model generates sub-questions and queries, with findings passed as untrusted data, and writes the executive summary from cited findings only; citations to non-existent sources are stripped. If the budget runs out, the model is switched off for the rest of the campaign and research continues deterministically.

## Model routing (`app/models/router.py`)

Roles: `planning`, `debugging`, `evaluation` (heavy) and `extraction`, `classification`, `summary` (light, using `MODEL_NAME_LIGHT` if set). Before every call the router checks the daily spend limit, the research spend limit, the hourly request cap and the per-objective token budget, and stops hard if any is exceeded. Each call (including failures) is recorded in `model_usage`. A fallback provider is used only if one is explicitly configured, and `LOCAL_ONLY` disables paid providers completely.

## Data model

See `app/migrations/0001_initial.sql`. It has foreign keys with cascades, indexes on status and time columns, FTS5 external-content tables kept in sync by triggers, and audit triggers that make the audit table append-only.
