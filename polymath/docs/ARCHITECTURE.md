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
| `drive` | PageRank, topic priorities (curiosity), targeted reading, user-requested learning, the bandit, self-improvement (tuning, changelog, specialists, reading strategy) |
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

**Face and version.** `interface/face.py` picks an expression from the job being run (its kind, or the prefix
before the dot), the body mode, and reactions to events (each with a duration and a priority, so a fixed mistake
is not overwritten by a routine inference). The clock animates it in 0.5 s frames: the client redraws only the
header between polls. Every glyph is single-width and in DejaVu Sans Mono, which a test checks against the
font's cmap. `polymath/version.py` reads `build.json`, which `install.sh` writes after the code is installed;
in a development checkout it reads the commit from `.git` with no subprocess. The agent puts its build in the
heartbeat. The feed compares it with the installed build for the `✓`/`↻` badge, and `os.execv`s itself into new
code when the installed build changes.

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

## Telling, predicting, surprise, recap, the map

- **`interface/tell.py`** writes a paragraph from relation templates, in a fixed order:
  1. what it is: the description with the right article;
  2. a life sentence for people: born and died, using the surname and never a pronoun;
  3. the facts that matter for that kind of thing;
  4. what points to it;
  5. a few plain facts, from an allow-list (identifiers and codes never appear).

  Inferred and disputed facts are labelled. Every sentence carries citations, and a citation to reasoning
  names its premises. The answerer uses it for "tell me about / who is" questions.
- **`reasoning/predictions.py`** (`reason.predict`, every 2 h):
  - candidate relations: single-valued, entity-valued, with 30+ subjects and no housekeeping, each with its
    subjects' usual P31 class;
  - gaps: members of that class lacking the relation entirely, not even as a hidden quiz fact. A cursor walks
    the class;
  - candidate answers: the relation's common objects plus objects already linked to the subject;
  - the link predictor chooses, and the guess is kept at ≥ 0.4 confidence when some evidence spoke;
  - settling: a later *sourced* fact (never an inferred one) marks it confirmed or refuted;
  - expiry: 90 days.
- **`evaluation/surprise.py`** (`eval.surprise`, every 3 h) looks at new facts, important subjects first:
  - numbers that are extreme among peers of the same class, relation and unit;
  - single-valued facts that the predictor, with the fact hidden, expected otherwise (at most 40 such
    predictions per run).
- **`evaluation/recap.py`** (`eval.recap`, Sundays 08:00 local): last 7 days against the 7 before.
- **`interface/knowledge_map.py`** (`memory.snapshot`, daily):
  - nodes: the top 120 topics by documents;
  - edges: cosine-normalised co-occurrence, each node's 4 strongest;
  - clusters: label propagation;
  - layout: Fruchterman–Reingold in numpy, warm-started from the stored layout and rescaled to the unit square;
  - time-lapse: rebuilt from document read times with adaptive steps (hours to weeks, at most 96 frames), and
    raised to daily snapshots so evictions do not erase history.

## Reasoning skills and cleaner knowledge (v0.5)

**Question skills** (`polymath/qa/`). `Answerer.ask` offers each question to the skills first, most specific
first: `numbers`, `places`, `when`, `causes`, `howto`, `kinds`, `chains`. Each matches its own question shapes with
regular expressions and answers from the graph with citations, or returns None so the general answerer handles the
question as before. Names are resolved by `qa.common.strict_entity`: an exact name (or its singular), and of several
things with that name the one that has the facts the question needs (coordinates for distances, dates for ages),
then the most facts, an article, and PageRank.

| part | module | job (every) | tables |
|---|---|---|---|
| dated facts | `reasoning/temporal.py`, `senses/wikidata.py` | (ingestion) | `fact_time` |
| impossible facts | `reasoning/sanity.py` | `reason.sanity` (3 h) | `sanity`, `value_ranges` |
| coordinates | `qa/places.py` | `memory.geo` (1 h) | `geo` |
| causes and kinds from text | `perception/semantic.py` | `perception.semantic` (1 h) | triples `text:causes`, `text:prevents`, `text:is_a` |
| what kinds can do | `perception/semantic.py` (while reading) | `perception.read` | `category_props` |
| how-to steps | `perception/howto.py` | `perception.howto` (6 h) | `howto`, `howto_fts` |
| data tables | `senses/wikitext.py`, `perception/tables.py` | `perception.anchors` | triples `table:<header>` |
| second language | `senses/sources.py`, `perception/jobs.py` | `wikipedia.part`, `perception.anchors` | `wiki_sitelinks` |
| duplicates | `memory/merge.py` | `memory.merge` (12 h) | `entity_merges` |
| big-brain mode | `core/config.py` (`apply_capacity`), `drive/capacity.py` | (planner) | kv `brain_tier` |

- **Time.** `parse_entity` keeps start time, end time and point in time per statement (`claims["_when"]`), and
  earlier values that ended (a normal-rank statement with an end time next to a preferred one). `_claims_to_triples`
  stores them in `fact_time`. `forward_chain` gives a conclusion the validity of its premises. `contradictions.detect`
  and `rule_audit.contradicted` skip ended facts. `Answerer.facts` and `Teller.facts_of` put current values first and
  show ended ones as "was … (1949–1990)", or drop them when a current value exists.
- **Second language.** A document in another language (`documents.lang`) is indexed without an entity, a topic or the
  text index. `perception.anchors` resolves its title and links through `wiki_sitelinks` (Wikidata), extracts its
  infobox under `infobox:<lang>:<key>`, maps aligned keys onto the Wikidata property (a second source for the same
  fact), reads its tables, and sets `stage = 3`, so it is never read as English text and never trains the embeddings.
- **Scale.** The sanity rules and the coordinate index scan fixed windows of triple ids per run (200,000 and
  500,000), so a run costs the same on a 100-million-fact graph. Contemporaries and events search the 200,000
  best-known entities (`entities_pagerank` index).

## Self-improvement (v0.4)

It improves itself from what it measures, never by rewriting its code: only settings, rule trust, its reading
strategy, specialist agents and sentence phrasings change. Every change goes to one log, `self_changes`
(`drive/changelog.py`), which feeds the feed ("improved" lines; the face looks proud, or "oops" when it undoes
something), the digest, the weekly recap, the dashboard panel *How it improved itself* and `polymath changes`.

| job (action group) | every | module | what changes |
|---|---|---|---|
| `self.tune` (improve) | 8 h | `drive/selftune.py` | one setting per run, by a paired test; watched and rolled back if the self-test drops |
| `reason.audit` (reason) | 6 h | `reasoning/rule_audit.py` | each rule's trust; demotes bad rules, withdraws contradicted conclusions |
| `self.specialists` (improve) | 6 h | `drive/specialists.py` | spawns a *predict* agent for each weak relation, retires it when it recovers |
| `self.strategy` (improve) | daily | `drive/strategy.py` | reading weights per source (job priorities) and per topic (curiosity) |
| `perception.phrasing` (learn) | daily | `perception/phrasing.py` | sentence phrasings learned from relation patterns, used by `tell` |

- **Settings** (`tuned()`): `link.subjects`, `link.features` (link predictor), `predict.min_confidence`
  (predictions), `infer.min_confidence` (forward chaining). Each module reads its value through
  `selftune.tuned(db, name, default)` (a one-minute cache over the `tuned` kv entry). A trial's questions and
  answers are its job checkpoint, so a trial runs in normal slices and survives restarts. Settings you reset are
  pinned (`tune_pinned`) and left alone.
- **Rule trust** (`rule_trust`, `withdrawn`): inference multiplies a rule's confidence by
  min(1, trust / 0.8) and skips demoted rules and withdrawn conclusions. Promotion clears the rule's
  `demoted` withdrawals and rewinds the inference cursor, so its conclusions can come back.
- **Specialists** are ordinary agents with `origin = 'auto'`: they share the agent society's scheduler, rewards
  and evolution, at most 3 at a time. Your own agents are never touched.
- **Reading weights** multiply the priority of `sources.plan` jobs, `feeds.poll` and `crawl.step`; topic factors
  (0.5–1.5) multiply topic priority. "Basics first": unread articles are ordered by how often what it already
  read refers to them, times PageRank.
- **Phrasings** (`phrasings` table) are kept across pattern-table rebuilds.

## Disk writes and the brain's home (`body/wear.py`, `body/volumes.py`)

- `WearMeter` samples the device counters and the process counters every 15 minutes into `disk_writes`. Counter
  resets are handled: a reboot or restart counts from zero.
- `report()` derives per-day rates, the SD budget and years-at-this-rate. The guard switches on saver
  (`BodyState.slice_factor = 2`, capped at 60 s per slice), and pauses on a read-only data disk.
- Fewer writes per cycle:
  - idle cycles commit the heartbeat only on change or every 30 s, because the pulse file proves liveness;
  - busy cycles commit it every 10 s;
  - the redundant `jobs_kind` index is dropped;
  - `wal_autocheckpoint` is 4,000 pages.
- `Helper.move_home` (root, from the udev-started unit): stop the services, `copytree`, compare file count and
  bytes, `quick_check`, write `home-drive.json` (its budget is read by `config.apply_home`), move the SD copy
  aside, leave `MOVED-TO-DRIVE.json` (`check_storage` refuses to start on it), set the manifest's `home` and a
  pool budget of 0, bind-mount, start. `mount_home` repeats the bind mount at boot or replug; `detach` stops
  the services first.

## The storage pool (`memory/pool.py`, `body/volumes.py`)

The pool has a privileged half and an unprivileged half.

**Privileged: mounting.** udev starts `polymath-volume@<dev>.service` for each drive plugged in. It runs
`polymath storage attach` as root, which follows strict rules:
- never the system disk;
- formatted on its own: a completely blank device, or a fresh shop drive (exFAT/NTFS/FAT, at least
  `home_min_gb`, holding only the maker's installers and manuals, under 1 GB). A fresh drive is formatted
  **once per physical drive**: its identity (`lsblk` serial, or model and size) is written to
  `/var/lib/polymath/formatted-drives.json` on the SD card, fsynced, *before* `mkfs` runs. A replug, a power
  cut mid-format, or a later laptop reformat cannot trigger a second format. `storage format` (by hand) is
  recorded there too;
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
