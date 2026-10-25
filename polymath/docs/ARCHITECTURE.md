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
   - **The vigil** (`core/vigil.py`, v0.8.2) is a daemon thread that does both every 10 s while the main thread
     makes progress, and writes `/run/polymath/pulse.json` (`what`, `job`, `since`; memory, not the SD card).
     - **Progress** is a section change (`vigil.enter`: a slice, each planner, the WAL checkpoint, "" between
       them) or a tick (`vigil.progress`).
     - **A hang:** after 15 min without progress it stops vouching, and systemd's watchdog restarts the agent.
     - **Readers:** the feed and dashboard (`Feed(run_dir=…)`, `/health`) and `cli.agent_running`. The feed's
       `activity` comes from `what`, the step running now (the cycles table only holds finished slices), plus
       `long_step` after 30 s.
     - **Slow steps:** `Agent._slow_step` records the slowest per kind in kv `slow_steps` when a slice went 60 s
       without progress or ran 5 min. It reports `jobs/slow:<kind>` (fault `job-slow`) at most daily.
     - The thread never touches the database. `Notifier.watchdog` takes a lock, and its `sent` log is a 64-entry
       deque.
4. **INTEGRATE, EVALUATE, LEARN.** These happen **in the same transaction** as the slice:
   - the handler's writes;
   - the job's checkpoint or completion;
   - the cycle record;
   - the heartbeat;
   - the bandit's reward update (value per CPU second).

   A power cut therefore loses at most the uncommitted slice, and the job resumes from its last checkpoint.

**Efficiency found by measuring (v0.8.2).** The real agent ran on a copy of a real 480 MB brain, with every
silence between watchdog pings recorded and slices profiled.
- **Text search** (`memory/text_index.py`): a snippet means decompressing the hit's whole document. A quiz spent
  77 % of its time doing that, decompressing 803 times for 255 distinct documents.
  - `TEXTS` keeps decoded texts, bounded at 24M characters, LRU, and shared by threads. Its key includes
    `content_hash` and `codec`, so it is never stale. Spilled bodies are not kept.
  - `TextIndex.count` ranks without opening documents. Link prediction used `len(search(…))` just to count.
  - A quiz went from 22 s to 8 s.
- **Small fixes:**
  - `embed.train` reads its vocabulary every 15 min, not on every slice.
  - Orin's reading rate (`caretaker.docs_since`) is a binary search over ids, not a scan of `documents`.
  - The LEARNING lines look at the newest 200 documents, not a sort of all documents of one source.
  - Orin no longer queues a backup, probe or eviction that is already due within the hour. The backup used to
    run twice in a row.

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

**What it is learning** (`interface/focus.py`, v0.8.1). `Feed.status` adds a `focus`: the learning thread under
the job slices. `pick` takes the newest cycle of a learning job kind (reading, downloading, asked-for learning)
from the last 15 minutes, and keeps the job already in focus while it is still queued and taking turns, so two
interleaved jobs do not make it flicker. `current` reads, read-only:
- who chose it, from the key its producer gave the job (`pursue:`, `learn:`, `relearn:`, `agent:`) and the
  payload's `about` and `topic_id` (written by `drive.learn` and `eval.remedy` from v0.8.1);
- this round's progress, from the job's own checkpoint: `wikipedia.titles` counts the batch's titles among the
  documents, `wikidata.dump` the compressed bytes behind its cursor, the record readers their `stored` count;
- the topic's `known`, the measure `drive/priority.py` ranks by: `(right + 1) / (asked + 2)` once quizzed on it,
  else `(read + 1) / (linked + 2)` over the things its documents mention. It is computed live, with a 30 s cache,
  for topics of up to 400 documents, and taken from `topic_priority` above that.

`start`, the value when the process first saw the round, is kept in memory (at most 64 rounds), so nothing is
written. Any database error leaves the focus out of that poll. The terminal renders it as two header lines (the
header is 6 rows); `static/focus.js` renders the same data as a card on both web pages, with DOM text only (no
HTML from data) and widths set through the CSSOM (the CSP forbids inline styles). `transitions` turns a new
round and a finished one into `focus` feed lines.

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

## Always on (v0.5.1)

- **Internet state** (`core/netstate.py`, kv `net`) is learned passively by the loop.
  - A slice that raises a connection-level error (no DNS, refused, timed out: a `FetchError` with no HTTP
    status) counts against the internet.
  - So does a slice in which two or more different hosts failed to connect while none answered
    (`HttpClient.failed_since`). That catches jobs that handle their own fetch errors, like the crawler and feed
    poller.
  - Any server answering (`HttpClient.responses` rising) proves the internet works.
  - Two failures make it offline; the change is an `events` row of kind `net`, so it shows in the feed.
- **Network jobs** (`register(..., network=True)`: downloads, dump reading, crawl, feeds, site vetting, Wikipedia
  titles) are held back while offline. They are removed from the ready list before the policy chooses, so the
  bandit only weighs work that can run. A network job that dies on a dead connection is deferred
  (`Scheduler.defer`): its attempts and checkpoint stay, so no outage dead-letters a download.
- **Offline study** (`drive/offline.py`) runs the reasoning, checking, training, quiz, tuning and agent jobs on
  faster schedules, one queued job per kind at most.
- **Coming back:** `senses/connectivity.py` probes known servers (HEAD, through the usual SSRF checks and proxy)
  with growing gaps; the first answer brings it back online.
- **Dashboard** (`interface/dashboard.py`):
  - a `PooledHTTPServer` with 8 worker threads, each with its own read-only connection;
  - per-request query deadlines (`Database.limit_queries`, an SQLite progress handler);
  - a background thread for the full-table counts;
  - its own lock for the ask box;
  - a self-checking systemd watchdog.
- **Feed** (`interface/feed.py`): `resilient_source` tries the dashboard, then the database directly, then the
  agent's snapshot (`interface/snapshot.py`, `/run/polymath/feed.json`, written by a planner every 5 s with
  approximate counts). Events already shown are not shown again when it switches source.
- **The brain's drive missing** (v0.5.2):
  - `check_storage` raises `BrainDriveMissing`. `polymath init` passes, so systemd's `ExecStartPre` does not
    fail, and `polymath run` waits in `cli.wait_for_storage`: it pings the watchdog and writes the "waiting"
    snapshot until the drive is mounted.
  - The drive helper's `detach` stops both services, releases the mounts and starts both again, so they wait
    visibly. `mount_home` restarts them onto the brain.
  - The dashboard's `ReadWritePaths=-/srv/polymath/db` lets it start without the brain; it then serves the
    agent's snapshot as `/api/feed`.
- **Frozen-drive guard** (`core/fsprobe.py`, v0.5.7): every look at the brain's storage (agent start, dashboard,
  doctor, `storage heal`, the volume helper) is a `stat` or small read in a child process with a time limit. A
  drive that stops answering reads as `hang` and is handled like a missing one; a task stuck in the kernel is
  left in a disposable child, never in Polymath itself.
- **Standby brain** (`core/standby.py`, v0.6.0): `polymath run` waits `standby_after_s` for the brain's drive,
  then runs a second, small brain from `standby_config` (data under `/var/lib/polymath/standby`, the dump kinds
  in `DROPPED_KINDS` not registered, no drive pool) with a watcher that stops it (exit 75) once the real brain
  answers. On the real brain, `standby.planner` queues `storage.fold_standby`: standby documents go through
  `DocumentStore.add` (dedup, state `new`, so the real brain perceives them again), then Wikidata records,
  crawled pages and issues; the standby id is recorded in kv `standby_folded` and the files are removed.
- **SD-card mode** (`core/sdcard.py`, v0.7.0): `storage.sd_only`, set by a configuration drop-in
  (`/etc/polymath/polymath.d/sd-card.toml`, merged by `load_config` after `polymath.toml`), makes
  `config.apply_sd_only` move the data directory to `storage.sd_dir` (`/var/lib/polymath/brain`, a systemd
  `StateDirectory`) and switch off everything that touches a drive: adoption, formatting, `move_home`, `heal`, the
  standby brain, the pool (`mount_root` points nowhere). The budget is `config.sd_budget`: the brain's bytes plus
  the card's free space less `sd_keep_free_gb`, so it stays constant as the brain grows; two backups. All job kinds
  run. At start, `sdcard.start` writes the marker `sd-card.json` (`fold_after`: the newest document it started
  with), kv `sd_card` (the feed and dashboard) and an issue `storage/sd_card`. Once the mode is off, the brain is
  an `Away` brain for `standby.planner`, like the standby one: `fold_job` takes documents after `fold_after`,
  and Wikidata records, crawled pages, issues and the agents Christian set up (spawned again from their
  directive) since `started`, then removes it. `scripts/sd-card-mode.sh`
  does the switching (drop-in, unmount, USB `authorized=0`, renaming the SD card's old copy into place, install,
  checks); `scripts/ops-lib.sh` holds what it shares with `rescue.sh`.
- **Orin the Caretaker** (`agents/caretaker.py`, v0.8.0).
  - **When it runs.** Job `caretaker.step` is kept recurring every 120 s by `caretaker.planner` at priority 3.6,
    above the bandit's `URGENT`. The body guard allows it during a pause.
  - **A turn.**
    - It reads `SIGNALS` from `vitals`, `/proc/self/status`, the `-wal` file, `backups`, `netstate`, `documents`,
      `quizzes` and `jobs`.
    - It samples them into `signal_samples` (every 10 min, a week kept) and recomputes `signal_bands`
      (median/MAD).
    - A reading outside its band (k·1.4826·MAD, never narrower than the signal's floor; k in kv `caretaker_k`, raised
      by self-resolving false alarms) twice in a row, or past a hard limit once, opens an `incidents` row (one per
      key).
    - `watch_jobs` adds crash loops (`cycles` failures per kind) and dead jobs. `watch_body_and_install` adds
      firmware under-voltage, slow USB disks, a build mismatch, the config hash and the data-directory mode.
      `triage_inbox` turns `events` issues into `inbox` incidents tagged with their known fault (on-hold faults
      only noted).
  - **Working an incident.** Each `MODELS` entry has causes with priors, read-only checks with likelihoods, and
    remedies per cause.
    - Priors are blended with how often each cause was it before (`fault_stats` remedy `''`).
    - `posterior` is Bayes. `best_check` picks by expected entropy drop per cost, at most 3 checks a turn.
    - At p ≥ 0.6 it applies the remedy with the best Beta P(fix)/cost. Tier 1 remedies change the database only:
      jobs; the kv switches `caretaker_slowdown` (read by `Guard.observe`), `caretaker_quarantine` (read by
      `Agent._quarantine_filter`) and `caretaker_checkpoint` (run by `Agent._checkpoint_if_asked`, outside a
      transaction); `selftune.reset(by="Orin")`.
    - After the model's window it verifies, updates `fault_stats`, and tries the next remedy or escalates.
  - **Escalation.** It quotes the fault's first `you`/`sudo`/`approval` step, repeats at most daily, and goes to
    events kind `caretaker` (the feed), kv `caretaker` (snapshot, dashboard feed status, face mood `mending`,
    doctor) and `polymath caretaker`.
  - **Migration 0018** adds `incidents`, `fault_stats`, `signal_samples` and `signal_bands`.
- **Caretaker knowledge** (`agents/faults.toml` + `agents/faults.py`, v0.6.1): known faults (codes, words,
  symptoms, causes with priors, checks, self-healing, steps tagged by who may act), the situation on the Pi,
  the incident history and per-release updates. `issues.report` tags each problem with its faults
  (`detail.known`); `polymath caretaker` and `doctor` read it; `tests/test_faults.py` keeps it current.
- **Issues** (`core/issues.py`): every problem becomes an `events` row of kind `issue` (area, code, severity),
  rate-limited to once an hour per code: the caretaker's inbox, the feed's `problem` lines and doctor's list.
- **`polymath doctor`** (`interface/doctor.py`) runs as you, reading only HTTP, `/run/polymath` and, if you may,
  the database.

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

## Private access from your own browser (v0.9.0, `interface/access.py`)

The dashboard runs **two listeners** from one process, sharing one `DashboardData`:

| listener | address | who | sign-in |
|---|---|---|---|
| plain HTTP | `127.0.0.1:8765`, always loopback (an old `host = "0.0.0.0"` is overridden) | the Pi: the menu entry, the live feed, the CLI | none |
| HTTPS (`TLSServer`) | `0.0.0.0:8443` (`[access] port`), only if `/etc/polymath/tls/server.{crt,key}` load | paired browsers on the home network | device key |

Each request passes a `Gate` before any route runs, in this order:
1. **Client address** (remote only): inside `[access] networks` (the private and link-local ranges), else 403 and a
   log line. systemd's `IPAddressDeny=any` + `IPAddressAllow=localhost link-local 10/8 172.16/12 192.168/16
   fc00::/7` drops other packets before the process sees them.
2. **Host header**: an IP literal or one of the Pi's names (`<host>`, `<host>.local`, `localhost`, `[access] names`),
   else 421. This defeats DNS rebinding (a page on `evil.example` re-pointed at the Pi's address).
3. **POSTs**: `Content-Type: application/json` (415 otherwise: an HTML form cannot send it), and `Origin` must be
   `<scheme>://<Host>` (403). The loopback listener checks `Origin` only when one is sent (the CLI sends none).
4. **Device key** (remote only): the `pm_device` cookie (`HttpOnly; Secure; SameSite=Strict`, 400 days) must hash
   to a paired device. Without it, pages redirect (303) to `/pair` and the API answers 401; only `PUBLIC_REMOTE`
   (the pairing page and its script and style, the manifest, the service worker, `ca.crt`, `/api/access/me`,
   `/api/access/pair`) is served.

**Pairing** (`Access`, state in `/var/lib/polymath/access`, 0700, files 0600, written atomically):
- `new_code()` (loopback, or a paired browser): 8 characters from a 32-letter alphabet without look-alikes
  (40 bits), shown as `XXXX-XXXX`; one at a time, valid 15 minutes, one use.
- `pair(code)`: five wrong guesses cancel the code; ten attempts per address per ten minutes are refused before the
  code is even compared (`hmac.compare_digest`). Success makes a 256-bit token (`secrets.token_urlsafe(32)`); only
  its SHA-256 is stored, with a name, when, and the last address. At most 20 devices.
- `check(token)`: a dictionary lookup of the hash; last-seen is kept in memory and written at most every
  10 minutes. The file's mtime is checked, so a revoke through the other listener (or the CLI) takes effect at once.
- `revoke(id|all)`, `devices()`, and `access.log` (JSON lines, trimmed): codes made, pairings, revokes, refusals.

**TLS** (`scripts/access-cert.sh`, run by `install.sh`): a local CA (P-256, 10 years, key 0600 root) signs a server
certificate (P-256, 397 days, SANs: the host name, `.local`, `localhost`, 127.0.0.1 and the current addresses;
key 0640 group `polymath`). It is renewed when missing, within 30 days of expiry, or when an address is no longer
named, always with the same CA, so a PC that trusts the CA once keeps trusting it. `tls_context` requires TLS 1.2+;
the handshake runs on the worker thread with a 10 s timeout (`HANDSHAKE_S`), so a stalled client cannot block
`accept`. Remote `/health` shows only `ok`, `heartbeat_age_s` and `state` (no version or build).

**The browser side:** `/pair` (`pair.html`, `pair.js`: the code, the certificate thumbprints, trusting the CA in
Windows), `access.js` (the header badge, the *Who can see this* card with revoke and "pair another device"), a web
app manifest and a service worker that caches nothing, so Chrome offers *Install page as app*. Every response
carries the CSP (no inline script, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `nosniff`,
`Referrer-Policy: no-referrer`, COOP/CORP `same-origin`, a `Permissions-Policy` that turns off camera, microphone,
location and the rest, and `Cache-Control: no-store`.

**CLI:** `polymath access [status|pair|devices|revoke ID|log]` runs as the desktop user (no sudo) and talks to the
loopback listener; `doctor`'s `access` line checks the certificate (present, not expiring, naming the current
addresses) and that the HTTPS port answers.

## Security audit (v0.9.0)

A full review of every privileged path and every input that comes from outside:
- **Root never writes through a link** (`core/safefs.py`). The root helpers write into directories the `polymath`
  user owns (a drive's `polymath-brain`, `/run/polymath`). `safe_write` opens the directory with `O_NOFOLLOW`,
  creates the temporary file with `O_CREAT|O_EXCL|O_NOFOLLOW` relative to it, sets mode and owner with
  `fchmod`/`fchown`, and renames within it. `safe_dir` makes or opens a folder the same way. A crafted drive whose
  `polymath-brain` is a link to `/etc` is skipped (`body/volumes.py`); `body/heal.py` saves its state the same way.
- **Units:** the agent and the dashboard add `ProtectKernelLogs`, `ProtectClock`, `ProtectHostname`,
  `ProtectProc=invisible`, an empty capability set, `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`,
  `UMask=0027` and `RemoveIPC`; the agent also `SocketBindDeny=any` (it never listens). The root helpers
  (`polymath-heal`, `polymath-volume@`) cannot use namespace options (their mounts must reach the system), so they
  get the subset that still applies: `NoNewPrivileges`, `RestrictAddressFamilies=AF_UNIX AF_NETLINK`,
  `IPAddressDeny=any`, `RestrictNamespaces`, `RestrictSUIDSGID`, `LockPersonality`, `MemoryDenyWriteExecute`.
- **No terminal escapes from the web:** page titles and texts lose control characters and bidi overrides
  (`senses/html_text.py`), and the feed strips them again before painting (`interface/feed.py`).
- **Bounded decompression:** `StreamResponse.read(n)` returns at most `n` decompressed bytes (`unconsumed_tail`), so
  a small gzip bomb never becomes one huge chunk.
- **Smaller fixes:** a garbage `Content-Length` is a 413, not a crash; the rate limiter forgets idle clients and
  never holds more than 4,096; root owns the installed code (`chown -R root`, `chmod -R go-w`).

## Storage on a small disk (v0.9.0, `body/usage.py`)

- **SD-card mode** keeps one backup (`SD_BACKUPS`) and runs it weekly (`SD_BACKUP_EVERY_DAYS`, via
  `maintenance.backup_every_s`), and keeps 3 GB free (`sd_keep_free_gb`). A backup short of room deletes older
  backups first (never the newest); eviction keeps one backup in SD-card mode, two otherwise.
- **`usage.files`**: sizes of the database (+ WAL), backups, vector indexes and downloads, free space and budget.
- **`usage.forecast`**: a least-squares line through hourly averages of the vitals' free space over 72 h (at least
  6 h of history); slower than 0.02 GB/day counts as steady. `days_left` is how long until the reserve.
- **`usage.breakdown`** (`--detail` only, it reads every page): SQLite `dbstat` grouped into search index, document
  texts, knowledge graph, language statistics, reasoning, other.
- **Orin** reads `days_to_full` as a signal (`disk_low`, hard limit under 3 days), and its backup-age limit is
  1.5 × the backup interval.

## Drive roles (v0.11.0, `body/roles.py`)

Run by `polymath storage heal` (root, every minute) after the drive helper and the SSD watch; not in SD-card mode.

- **Facts per drive** (`Drive`): the pool's mounted volumes and their manifests; `disk_facts` asks `lsblk -d` for the
  partition's disk, transport and `ROTA`, and sysfs for the USB link speed and `bMaxPower`; `shutil.disk_usage` for
  size and free space; `pool.room` for what Polymath may still put there. The record keeps, per drive, since when it
  has been plugged in, its drop-outs this week (it vanished between two runs; for the brain's drive also the drive
  helper's `dead_mount`/`usb_reset`/`usb_power`), and its measured read speed (`ssdwatch.read_check`, one drive per
  run, each once a day; a failed read counts as a drop-out).
- **Score** (`brain_score`): link (NVMe 6, 10 Gbit/s 5, 5 Gbit/s 4, SATA 4, unknown 2, USB 2 1) + 2 for an SSD +
  min(MB/s ÷ 150, 2) + min(log2(free ÷ 32 GB + 1) ÷ 4, 1) − drop-outs. **Eligibility** (`eligible`): POSIX
  filesystem, dedicated, not USB 2 (unless `brain_allow_usb2`), fewer than `FLAKY_DROPS`, and for a new home free
  space ≥ max(`home_min_gb`, 3 × the brain) and `brain_settle_min` minutes plugged in.
- **Where the brain is** (`_where`): bind-mounted from a drive (`drive`), on the SD card (`sd`: the data directory on
  the system disk, no marker), away (`missing`: the SD card's `MOVED-TO-DRIVE.json` without a mount), or on its own
  disk (`own`: never moved).
- **Moves**: from `sd` onto the best eligible drive (`Helper.move_home`); from `drive` onto a candidate scoring
  `MOVE_MARGIN` more, inside the night window and `MOVE_EVERY_S` after the last move, or at once when the home has
  `FLAKY_DROPS` (`Helper.move_brain`). Under the helper's lock. A failure sets `next_move` 6 h ahead.
  `Helper.should_move` (the immediate move on attach) is off while `auto_roles` is on.
- **`Helper.move_brain`**: stop the services; copy the data directory (the bind mount of the old home) onto the new
  drive's `polymath-brain/home`, compare files and bytes, `quick_check` the database and add a `home` event to it;
  write `home-drive.json`; unmount the bind (a busy mount aborts and removes the copy); rename the old `home` to
  `home-previous-<time>`, flip both manifests (`home`, `budget_bytes`, `previous_home`), point the SD card's
  `MOVED-TO-DRIVE.json` at the new drive, bind-mount the new home and start the services.
- **Backups and extra room** (`_assign`): the backup drive is the one, on another physical disk than the brain's,
  with the fewest drop-outs and the most room ≥ 1.5 × the database. `memory.pool` reads the decision from
  `/run/polymath/drive-roles.json`: `backup_dir` uses that drive while it has room, and `choose` (spills, downloads)
  ranks it last.
- **Warnings**: the largest `bMaxPower` per USB device summed over the drives > 1200 mA; no other drive for backups;
  the brain's drive on USB 2.
- **Cleanup**: a `home-previous-*` copy goes after `PREVIOUS_KEEP_S` if the brain's drive has not dropped out since
  the move; the manifest's budget grows by what it freed.
- **Record**: `/var/lib/polymath/drive-roles.json` only when the decision or a drive's presence, drop-outs or
  measurement changed (`_stable`); `/run/polymath/drive-roles.json` every run.

## A wiped brain drive, and drive care (v0.11.2)

- **Recognising the brain's drive** (`SSDWatch._reformatted`): `MOVED-TO-DRIVE.json` on the SD card now carries the
  drive's `serial`, `model` and `usb` (vendor:product), written by `Helper.move_home`/`move_brain`
  (`Helper.drive_identity`). A disk counts as the brain's drive, reformatted, when its serial or USB device matches,
  or the USB device `sd-card-mode.sh on` recorded, or its model matches the marker's model or name, and no partition
  carries the brain's filesystem id.
- **In SD-card mode** (`_absent` → `promote`): after `sd_return_stable_min` minutes it stops the services, renames
  the marker to `MOVED-TO-DRIVE.json.replaced-<time>`, moves the SD-card brain's entries into the data directory
  (`_move_dir_in`: `os.replace`, `shutil.move` across filesystems, anything in the way renamed aside; the SD-card
  marker and seed file renamed hidden so it is not folded as an away brain), removes the drop-in and the SD-card
  mode record, and restarts every `polymath-volume@` unit and the services. `polymath storage keep-sd-brain` calls
  `promote` at once.
- **Outside SD-card mode** (`_on_ssd` → `rebuild`): with the marker present and nothing mounted, a recognised wiped
  drive for `sd_return_stable_min` minutes leads to a rebuild: the newest `polymath-*.sqlite3.xz` under the drives'
  `polymath-brain/backups` restored with `maintenance.restore` (integrity-checked; a damaged one is skipped), else
  the standby brain moved in, else an empty start; ownership back to `polymath`; drives and services restarted. The
  standby brain's marker stays in its own directory, so the rebuilt brain folds it in.
- **An old home coming back** (`Helper.mount_home`): when the data directory holds a brain of its own, or the marker
  names another drive, the returning drive's `home` is renamed `home-previous-<time>` and its manifest made extra
  room, instead of being mounted over the brain.
- **Empty partition table** (`Helper._attach`): a whole disk with `PTTYPE` and no filesystem or partition is
  repartitioned (`sfdisk --wipe always`) and formatted when `format_blank_disks` allows.
- **Drive care** (`DriveRoles._care`, `care_format_empty`): a mounted, settled, non-home drive with a Windows/macOS
  filesystem that is empty (`_empty`: only system clutter at its top and only the manifest and inventory in its
  `polymath-brain`) is reformatted with `Helper.format_empty(by="drive roles (empty drive)")`, which checks again,
  refuses any file and any Polymath document, records the drive, runs `mkfs.ext4` and attaches it again. One drive a
  run, a drive at most once a day. `format_empty` now looks for files at any depth in `polymath-brain` (empty folders
  and Polymath's own notes do not count).

## What each drive holds, and relearning (v0.11.0, `memory/inventory.py`)

In the agent (`storage.inventory` hourly, `storage.recover` every 10 minutes; neither in SD-card mode nor on standby).

- **Inventory** (`volume_inventory`, `files_on`): per known drive, the spilled documents (`codec = 'spilled'`,
  `meta.spill.vol`): count, characters, by source, top topics (`doc_topics`), facts resting on them (distinct
  `provenance.triple_id`), and how many are lost; for a mounted drive also its bodies, downloads, backups and old
  brain copies. Into `kv inventory` and `polymath-brain/inventory.json` on the drive.
- **Lost** (`mark_lost`): a drive offline (`volumes.online = 0`, not retired) for `relearn_after_h` gets
  `meta.lost = {vol, at}` on its documents (the spill pointer stays) and one row each in `relearn` (migration 0019)
  with priority 10 × provenance rows + 50 if a document topic is in `user_interests` + min(chars, 200 000) ÷ 100 000,
  and `how`: `wikipedia`, `web` (`web`/`feed` sources with a URL) or `later`.
- **Sending** (`_queue`): up to `PER_RUN` a run and `PER_DAY` a day, by priority: Wikipedia titles in batches of 100
  per language as `wikipedia.titles` jobs (byte ranges from the dump), URLs into the crawler's frontier with a
  `crawl.step`. Queued rows still lost after `QUEUED_STALE_S` become `later`.
- **Replacing** (`DocumentStore.add`): a stored document with `meta.lost` takes the new copy's body and meta whatever
  the content hash (same text: the index entries stay valid; changed text: its chunks are dropped and it is read
  again, `state = 'new'`). `storage.recover` then marks the row `done`.
- **Back** (`came_back`): an online drive's documents lose `meta.lost`, its rows become `back`.
- **Around it**: a `volume_events` row `lost`/`back` (the feed), an issue `storage/relearn:lost|back:<id>`, a
  `body.backup` when the drive held backups, a `sources.plan` for its downloads, and `relearn_status` in the snapshot
  (doctor's `relearning` line) and on the dashboard.

## The SSD watch (v0.10.0, `body/ssdwatch.py`)

Run by `polymath storage heal` (root, `polymath-heal.timer`, every minute) right after the drive helper. It builds two
configurations: the one running now and the one without SD-card mode's drop-in (`load_config(skip_dropins=…)`),
which says where the brain lives on the SSD.

- **Finding the SSD.** The target is the filesystem id in the SD card's own `/srv/polymath/MOVED-TO-DRIVE.json`
  (written when the brain moved onto the drive). `lsblk -J` lists the block devices (the SD card, loop, zram and
  the like are never candidates); the USB device behind a disk comes from sysfs (`usb_device_dir`), the enclosure
  from the vendor:product `sd-card-mode.sh on` recorded in `/var/lib/polymath/sd-card-mode.json`.
- **States** (`SHORT` maps each to the feed's words): `absent`, `no_power` (0 bytes: one soft reset of its USB device
  per `RESET_EVERY`), `enclosure_only`, `other_drive`, `usb2` (refused unless `sd_return_allow_usb2`),
  `switched_off` → `waking` (its `authorized` switch set to 1, `WAKE_WAIT_S` to show up, else back to 0;
  `WAKE_AFTER` backoff), `waiting`, `checking`, `unhealthy`, `returning`, `returned`, `return_failed`, `probation`,
  `fallback`, `home`, and `off`/`manual`/`no_ssd` when it stands aside.
- **The check** (`read_check`): a child process opens the partition with `O_DIRECT` and reads 16 MiB at its start,
  middle and end into a page-aligned buffer. Over 30 s, or slower than 2 MB/s, or an error: unhealthy. A child stuck
  in the kernel is never waited for; its pid file stops a second one from starting while it is stuck. The disk's
  SCSI `ioerr_cnt` must not grow during the streak, and the streak belongs to one USB connection (disk, bus and
  device number): a replug starts it over and clears the backoff.
- **The return** after `sd_return_stable_min` minutes and at least `MIN_PROBES` checks: stop the agent and the
  dashboard (a clean stop, `KILL` after `STOP_WAIT_S`), rename the drop-in to `.off`, restart every
  `polymath-volume@` unit (their attach now runs without SD-card mode: the SSD's `mount_home` binds the brain and
  restarts the services, and drives plugged in during SD-card mode, which were left alone, are adopted), and wait up to `MOUNT_WAIT_S` for the brain's mount with `home-drive.json` readable. Then
  the `.off` file and the SD-card-mode record go, and the agent folds the SD-card brain in
  (`core.standby.fold_job`). Otherwise the drop-in comes back, the mounts are dropped lazily, everything starts on
  the SD card, and `RETRY_AFTER` backoff applies. A move cut short (a power cut, the unit's time limit) leaves
  the `.off` file without a drop-in: the next run finishes it if the brain is mounted, else puts the drop-in back,
  and starts everything either way, so the agent is never left stopped.
- **Probation and fallback.** For `PROBATION_S` after an automatic return, the drive helper's own record
  (`dead_mount`, `usb_reset`, `usb_power`) is counted; at `PROBATION_FAILS` it writes the drop-in itself, drops the
  mounts lazily, switches the SSD's USB device off, records it like `sd-card-mode.sh on` does, and starts everything
  on the SD card; the next try is a day later. Its own drop-in write sets the episode, so that day is kept.
- **Episodes.** The drop-in's modification time names a stretch of SD-card mode; a new one resets the backoff and
  holds everything for `HOLD_S` (an hour), so a deliberate `sd-card-mode.sh on` is not undone at once.
- **Record and wear.** `/var/lib/polymath/ssd-watch.json` is written only when something changed; the copy in
  `/run/polymath` every minute. `core.sdcard.status` puts it in the snapshot (`sd_card.ssd`), so the feed, doctor
  and the dashboard show it; the agent reports its events to the caretaker's inbox at start
  (`report_ssd_watch`, code `storage/ssd_watch:<event>:<time>`).

## Process boundaries and safety

- **Agent unit:**
  - It runs as the unprivileged `polymath` user.
  - `ProtectSystem=strict` leaves only `/srv/polymath` writable, and it has no access to home directories.
  - Limits: `CPUQuota=200%`, `MemoryMax=3G`, `Nice=10`, best-effort IO class 7.
  - `RequiresMountsFor=/srv/polymath`: it cannot start if the NVMe drive is missing.
- **Dashboard:**
  - It opens the database with `mode=ro`.
  - `/health` is 200 only when the database opens and the agent's heartbeat is under 60 s old.
  - Every other method than GET/HEAD, `POST /api/ask` and `POST /api/access/*` returns 405.
  - A strict Content-Security-Policy is set, request bodies are capped at 4 KB, and `/api/ask` is limited to 20 requests per minute per client.
- **Outbound HTTP** only reaches public addresses. The resolved IP is pinned at connect time, so DNS rebinding cannot reach the LAN.
