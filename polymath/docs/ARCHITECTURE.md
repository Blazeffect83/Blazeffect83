# Architecture

Polymath is one Python process (`polymath run`, started by `polymath.service`) plus a read-only dashboard
process (`polymath dashboard`). Both share one SQLite database in WAL mode on the NVMe drive.

```
                 ┌────────────────────────── polymath.service (Type=notify, watchdog 120 s) ──────────────────────────┐
  dumps, feeds,  │                                                                                                    │
  crawler  ────► │  SENSES ──► MEMORY ──► PERCEPTION ──► EMBEDDINGS ──► REASONING ──► DRIVE ──► EVALUATION         │
                 │    │          │            │               │              │           │          │               │
                 │    └──────────┴────────────┴───────────────┴──────────────┴───────────┴──────────┘               │
                 │                    every unit of work is a *job slice* in one durable queue                       │
                 │                                                                                                    │
                 │   OBSERVE (body, inbox, planners) → DECIDE (bandit) → ACT (one slice) → INTEGRATE → EVALUATE → LEARN │
                 └──────────────────────────────────────┬─────────────────────────────────────────────────────────────┘
                                                        │ SQLite WAL  /srv/polymath/db/polymath.sqlite3
                 polymath-dashboard.service ◄───────────┘ (read-only; the ask box runs the offline answerer)
                          │ /api/feed (localhost)
                 polymath feed  — the terminal that opens at login: a live feed of what it learns
```

## The loop (`core/loop.py`)

Each cycle:

1. **OBSERVE.** The body guard reads temperature, load, memory and disk. Then:
   - inbox requests from the CLI or dashboard are applied;
   - due planners run. Planners are cheap, network-free functions that keep recurring work queued.
2. **DECIDE.** The Thompson-sampling bandit (`drive/bandit.py`) picks an *action group* (read, perceive, learn,
   reason, crawl, plan, evaluate, maintain…) for the current context: body mode × day/night × backlog.
   - Jobs with priority ≥ 2.9 (housekeeping) go first.
   - In `yield`/`throttle` mode only light jobs are eligible.
   - In `pause` mode nothing runs, except eviction when the disk is full.
3. **ACT.** One bounded *slice* of the chosen job runs, with a budget of 15 s × intensity.
   - Every slice must make progress.
   - Long handlers call `ctx.tick()`, which feeds the systemd watchdog and touches the heartbeat pulse file.
4. **INTEGRATE, EVALUATE, LEARN.** These happen **in the same transaction** as the slice:
   - the handler's writes;
   - the job's checkpoint or completion;
   - the cycle record;
   - the heartbeat;
   - the bandit's reward update (value per CPU second).

   A power cut therefore loses at most the uncommitted slice, and the job resumes from its last checkpoint.

## The job queue (`core/scheduler.py`)

- **SQLite table.** Jobs have an idempotency key, priority, `not_before`, a JSON checkpoint, attempts and crashes.
- **Recurring work.** `ensure_recurring(kind, interval, phase)` enqueues one job per time window; the key is the window's slot.
- **Failures:**
  - Failed slices back off exponentially.
  - A job whose process died mid-slice three times is dead-lettered, so one poisonous input cannot crash-loop the Pi.
- **Inbox.** Other processes never write to the database while the agent runs. They drop JSON files into
  `<data>/inbox/` instead, and the agent applies them inside its own transaction (`core/inbox.py`).

## Modules

| package | responsibility |
|---|---|
| `core` | config, logging (JSON to journald), database and migrations, scheduler, loop, app assembly |
| `body` | sensors, systemd notify/watchdog, guard (thermal, disk, storage pool), backups, eviction, drive helper |
| `senses` | HTTP client (SSRF-safe, resumable ranges), dump readers, bz2 block seeking, 7z, feeds, crawler, robots.txt |
| `memory` | documents (lzma bodies), passages + FTS5, near-duplicates, knowledge graph with provenance, topic map, IVF vector index |
| `perception` | tokenizer, Porter stemmer, Punkt sentences, NPMI phrases, Aho–Corasick, entity linker, infoboxes, TextRank, relation patterns, SGNS embeddings |
| `reasoning` | source reliability (truth discovery), contradictions, rule learning + forward chaining, link prediction |
| `drive` | PageRank, topic priorities (curiosity), targeted reading, user-requested learning, the bandit |
| `evaluation` | held-out facts, quizzes, nightly report |
| `agents` | the agent society: directives → scopes, skills with verifiable tasks, rewards, per-agent learning, evolution |
| `interface` | CLI, answering engine, dashboard, live feed |

## The agent society

Agents are logical: they share the one process and the one CPU budget, so the Pi is not oversubscribed. Their
work runs in four jobs, all in the `agents` action group:

- **`agents.step`** (every 2 minutes) runs turns until the slice budget is spent:
  - it picks an agent by Thompson sampling on verified reward per step;
  - the agent answers your pending questions;
  - the agent picks an action with its own arms;
  - the action records tasks, and immediately verifiable ones are rewarded on the spot.
- **`agents.verify`** (every 15 minutes) settles delayed tasks: predictions against newly arrived facts,
  disputes, and reading requests.
- **`agents.evolve`** (daily) forks, judges and adopts.
- **`agents.command`** applies your CLI commands, through the inbox while the agent runs.

The state lives in five tables:
- `agents`: directive, kind, scope, heritable parameters, XP and level, totals;
- `agent_scope`;
- `agent_tasks`: every task with its action, state, reward and reason;
- `agent_rewards`: the ledger;
- `agent_arms`: per-agent action preferences.

## The live feed (`interface/feed.py`)

The feed needs no event log. Every refresh reads, read-only, the rows added to the agent's own tables since the
client's cursor:
- `documents`, `triples` (with their first provenance), `reasoning_runs`;
- `quiz_answers` and `quizzes`, `agent_rewards` (with the task it was for) and `agents`;
- `drive.learn` jobs, `reports`, `backups`, and failed `cycles`.

The cursor is the highest id seen in each table. Busy tables are capped: the newest few rows are rendered and
the rest counted, and id spans over 200k are estimated rather than counted. Facts about things whose name it
has not read yet (`Q123`) are counted, not shown. Status changes (body mode, agent online/offline, curiosity)
are turned into feed lines by the client, which compares successive statuses.

The dashboard serves the feed at `/api/feed`. `polymath feed` polls it every 1.5 s, pins a status header with an
ANSI scroll region, and spreads each batch over the interval so it reads as a stream. `scripts/open-feed.sh`
opens it at login in the first terminal it finds (lxterminal on Raspberry Pi OS). A lock file stops the XDG and
compositor autostarts from opening two windows.

## Open-web learning, the digest, learning from mistakes

- **Open-web learning** (`senses/openweb.py`):
  - Wikipedia ingestion counts cited sites (`site_citations`, `site_urls`).
  - `web.blocklists` downloads the safety lists; `web.blocklist` loads each one in slices as 64-bit hashes
    (`blocked_domains`).
  - `web.vet` runs the gate (see SOURCES) and records `sites`; `web.trust` judges sites on probation.
  - The crawler fetches only the allow list, approved feeds' sites and vetted sites. It follows links only within
    the allow list, approved sites, and sites on probation that are under their quota.
- **Learning from mistakes** (`evaluation/remedy.py`): `eval.remedy` turns wrong self-test answers (from quizzes
  and agents) into `remediation` items. Each one queues `wikipedia.titles` for the subject, the answer and, later,
  the wrong choice, then re-tests after `learning.relearn_delay_hours`, up to `relearn_attempts` times. The hidden
  fact stays hidden, so a fix comes only from what was read.
- **The digest** (`evaluation/digest.py`): `eval.digest`, daily at `learning.digest_hour`, stores `digests`:
  - what it read and learned, notable new facts, the quiz trend;
  - mistakes and fixes, vetted sites, disputes, the best agent, what's next.

  The feed (`digest` lines), the dashboard panel and `polymath digest` show it. Vetting and relearning notices go
  to the `events` table, which the feed streams.

## The storage pool (`memory/pool.py`, `body/volumes.py`)

The pool has a privileged half and an unprivileged half.

**Privileged: mounting.** udev starts `polymath-volume@<dev>.service` for each drive plugged in. It runs
`polymath storage attach` as root, which follows strict rules:
- never the system disk;
- only completely blank devices are formatted;
- nothing outside `polymath-brain/` is touched.

The drive is mounted under `/mnt/polymath/<uuid>` with `nodev,nosuid,noexec`, plus a manifest with its budget.
Removing the drive stops the unit, which unmounts lazily.

**Unprivileged: using.** The agent only discovers mounted manifests. It never mounts anything.
- `polymath.service` may write there (`ReadWritePaths=-/mnt/polymath`), and new mounts propagate into its
  namespace.
- The guard records arrivals and departures (`volumes`, `volume_events`, shown in the feed) and asks for
  `body.spill` when the main disk passes `storage.spill_at`.
- Spilled bodies go to append-only pack files: a record is `magic, doc id, length, crc32` plus the blob, fsynced
  before the database pointer commits. A crash can leave an orphan record, never a dangling pointer.
- Reads go through `load_body`. A body whose drive is away reads as empty (`meta.unavailable`); an update to it
  waits until it is back, because a contentless FTS delete needs the old text.
- `body.recall` brings a drive's bodies home before retiring it.

## Storage layout (`/srv/polymath`)

```
db/polymath.sqlite3      the only database (WAL); migrations in polymath/core/migrations
index/                   alias automaton (.npz), embeddings (w_in/w_out .npy), IVF generations (memmaps)
raw/                     dump files being downloaded/read; deleted once consumed
backups/                 polymath-YYYYmmdd-HHMMSS.sqlite3.xz, newest 7 kept
reports/                 nightly report-YYYY-MM-DD.md / .json
inbox/                   requests from the CLI and dashboard
heartbeat                pulse file (mtime) touched by the agent while it works
storage-ignore.json      drives retired with `polymath storage retire` (never adopted again)

/mnt/polymath/<uuid>/polymath-brain/   one per plugged-in drive
  .polymath-volume.json  manifest: id, label, size, budget, dedicated or shared
  bodies/pack-NNNNNN.bin spilled document bodies (append-only, crc-checked)
  raw/  backups/         downloads and backups placed on the drive
```

## Process boundaries and safety

- **Agent unit:**
  - It runs as the unprivileged `polymath` user.
  - `ProtectSystem=strict` leaves only `/srv/polymath` writable, and it has no access to home directories.
  - Limits: `CPUQuota=200%`, `MemoryMax=3G`, `Nice=10`, best-effort IO class 7.
  - `RequiresMountsFor=/srv/polymath`: it cannot start if the NVMe drive is missing.
- **Dashboard:**
  - It opens the database with `mode=ro`.
  - `/health` is 200 only when the database opens and the agent's heartbeat is under 60 s old.
  - Every other method than GET/HEAD and `POST /api/ask` returns 405.
  - A strict Content-Security-Policy is set, request bodies are capped at 4 KB, and `/api/ask` is limited to 20 requests per minute per client.
- **Outbound HTTP** only reaches public addresses. The resolved IP is pinned at connect time, so DNS rebinding cannot reach the LAN.
