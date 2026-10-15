# Agent society v2: addendum C, the Caretaker

**Status:**
- spec written 2026-10-13 on Christian's request;
- its inbox has been live since v0.5.2 (C.2b);
- its knowledge base has been live since v0.6.1 (C.6b);
- the agent itself is built after stage S1.

Christian's request:

> "one of them dedicated just for fixing and working on the pi or the polymath intelligence if there is any
> technical issues detected … in charge of troubleshooting, security, and any simple to advanced issues it can
> handle"

> "make sure that the caretaker initiative knows about this … every time we do an update like this, it's updated
> on the current situation and it knows to fix it in the future in case it happens again"

**Baseline:** v0.6.1. It builds on:
- `core/netstate`, `polymath doctor`, the agent snapshot and the dashboard watchdog (v0.5.1);
- the issue inbox (v0.5.2);
- `storage heal` (v0.5.5–v0.5.7);
- `core/fsprobe` (v0.5.7);
- the standby brain (v0.6.0);
- the knowledge base (v0.6.1).

**Precedence:** the spec's hard constraints H1–H9 win over everything here. So does the standing rule: it never
runs commands, never edits code, and never makes privileged changes without Christian.

---

## C.1 Who it is

| | |
|---|---|
| Role id | `caretaker` (system role, like the manager: created by migration, **not counted in `max_agents`**, never retired or paused by lifecycle rules) |
| Name | **"Orin the Caretaker"** (invented, D8) |
| Directive | **Keep Polymath and the Pi running smoothly and safely.** Notice problems early, find their cause, fix what it can fix itself, and tell Christian exactly what to do about the rest. |
| Works with | everyone. It reads all agents' health. It may pause, slow or quarantine *work*, never judge agents (the lifecycle does that). It sends `inform` and `request` to the manager like any agent (§8). |
| Turn | first after the manager in every `agents.step` slice: ≤ 0.5 s CPU (resource:cpu; 0.5 + 0.5 + 5 × 2 s = 11 s ≤ 15 s). Light checks every turn; heavy checks (integrity, permissions) on their own schedules. **It runs even when the society is paused** (`society pause` stops the others, not the caretaker; `society stop --yes` stops it too). |

## C.2 What it watches (its senses)

Everything below is already collected or readable without root. Nothing new touches the network.

| Area | Signals (source) |
|---|---|
| **Body (Pi)** | CPU temperature and trend (`vitals`); throttle and pause episodes (`BodyState`); under-voltage (`/sys/class/hwmon/*/in0_lcrit_alarm`, spec §11.2); load; agent RSS (`/proc/self/status`); free disk per drive; write rate and SD wear (`body/wear`); read-only data disk |
| **USB / drives** | brain drive mounted (`home-drive.json`, `MOVED-TO-DRIVE.json`); **link speed** of each brain drive (`/sys/bus/usb/devices/*/speed`: `480` = USB 2, `5000` = USB 3); drive online and offline events (`volume_events`); spill and recall errors |
| **The loop** | heartbeat and pulse age; slice failures per job kind (`cycles`); dead jobs; jobs stuck `running` past their lease; slices over budget; starts per hour (`kv starts`: restarts and watchdog kills); planner exceptions (log counter) |
| **The dashboard** | a pulse the dashboard writes to `/srv/polymath/db/dashboard-pulse` (new: it may write there) plus its last query-deadline hits. A stale pulse means down; many deadline hits mean struggling |
| **Internet** | `core/netstate` (offline since, probes, reason) |
| **Database** | WAL file size versus `journal_size_limit`; `freelist_count`/`page_count` (bloat); last backup age and its verification result; a weekly `PRAGMA quick_check` **on the newest backup copy** (never the live file, so no lock on the brain) |
| **Intelligence** | quiz accuracy trend versus chance; contradiction and sanity-flag rates; rules demoted per day; self-tune rollbacks; linker validation score; embedding quality; ingest throughput (documents and facts per hour) versus its own 7-day median |
| **Security** | safety-list age (`blocklists_loaded`); share of sites refused or dropped; changes to `crawler.allow` / `user_allow_domains` (only Christian may add); outbound hosts that were not vetted (from `HttpClient.failed_since` and the fetch log); the dashboard bound to `0.0.0.0` without a society token (D6); data-directory mode and owner drift (`0750 polymath`); a config file hash change (`/etc/polymath/polymath.toml`); installed build versus running build; malformed inbox files; `/api/ask` rate-limit hits |

## C.2b Its inbox: every issue reaches it (built in v0.5.2)

Christian: "make sure we inform caretaker of all issues". Every problem Polymath notices is written in one place
before the caretaker exists, so nothing is missed when it arrives.
- **The inbox:** `core/issues.report(db, area, code, text, severity)` writes an `events` row of kind `issue`
  (rate-limited to once an hour per code).
- **Reports already wired in v0.5.2:**
  - jobs that give up (`jobs/dead:<kind>`) or cannot be done (`jobs/permanent:<kind>`);
  - body pauses and slowdowns (`body/thermal:*`, `body/disk:*`, `body/read_only:*`);
  - restart loops (`agent/restarts`: 4 or more starts in 10 min);
  - unclean stops (`agent/unclean_stop`: power cut, crash, kill);
  - time spent without the brain's drive (`storage/brain_missing`);
  - a fallback snapshot that cannot be written (`feed/snapshot`).
- **The caretaker also reads:**
  - internet changes (events kind `net`);
  - drives arriving and leaving (`volume_events`);
  - the agent's waiting state (`/run/polymath/feed.json`);
  - the dashboard pulse and deadline hits (C.2).
- **Reports added since:**
  - the drive helper's actions (`storage/heal:<action>:<at>`, v0.5.5);
  - learning on the standby brain (`storage/standby`, v0.6.0).
- **Every report is tagged** with the known faults it matches (`detail.known`, v0.6.1, C.6b).
- **Everything new must report too.** Every new subsystem reports its failures through `core/issues`. A test
  (`test_every_failure_path_reports_an_issue`, added with the caretaker) scans for `except` blocks that log at
  WARNING or above in `polymath/` and requires an `issues.report` beside each, or an explicit
  `# not an issue: <why>`.
- **The caretaker marks issues it has taken on** with `incidents.issue_ids`, so nothing is handled twice.

## C.3 How it thinks (no LLM)

An **incident loop**, every turn, deterministic given the database and a seed:

1. **Detect.** Each signal has a healthy band, which it *learns*: median ± k·MAD of its own last 7 days (k = 4),
   plus fixed hard limits from the fundamentals (C.6). A signal outside its band is a **symptom**. A symptom
   that lasts ≥ 2 checks, or one hard-limit breach, opens an **incident** (one per root symptom; dedup key
   `symptom:area`).
2. **Hypothesize.** A fault model in data (`polymath/agents/faults.toml`, packaged and fixed like roles, D4)
   lists causes, the symptoms each produces with likelihoods, and the cheap checks that tell causes apart. It
   ranks causes by Bayes: `P(cause | symptoms) ∝ prior × Π P(symptom | cause)`. Priors start from the
   fundamentals and are updated from resolved incidents (Beta counts per cause).
3. **Check.** It picks the check with the highest **value of information**: the expected entropy drop over the
   causes, divided by the check's cost. It runs at most 3 checks per turn. Every check is a read: a query, a
   `/sys` file, a log counter.
4. **Act.** When the top cause has `P ≥ 0.6`, it picks the remedy with the best `P(fix | cause) / cost`. The
   remedy comes from its own tier (C.4). `P(fix)` is a Beta posterior per (cause, remedy), learned from
   verified outcomes. That is how it gets better at fixing things.
5. **Verify.** After the remedy's window (for example 10 min for "dashboard slow", 1 h for "disk filling"), the
   symptom must be back in band and stay there for one more window.
   - Verified: the incident is **resolved** and the (cause, remedy) Beta gets a success.
   - Not verified: the remedy gets a failure, and it re-plans from step 2 with that remedy excluded.
6. **Escalate.** It escalates to Christian when any of these holds:
   - the top cause is Tier 2;
   - two remedies have failed;
   - the incident is older than 1 h and still getting worse;
   - it is a security incident.

   **An escalation is one clear message.** It goes to the feed (style `caretaker`, a new face mood), to
   `polymath doctor`, and to `polymath agents mail inbox Christian` if Q13 is answered yes. It has a fixed
   form: **what is wrong, the evidence, the most likely cause and how sure it is, and the exact command or step
   to take.** It repeats at most once a day per incident until resolved.

Every step writes a why line (spec §9.5):

```
caretaker: dashboard slow (feed 4.8 s, band ≤ 0.4 s) → likely: big counts on a request path (p 0.71) or
           low memory (p 0.18) → checked memory: fine (p 0.86) → nothing I can change myself →
           tell Christian: "sudo systemctl restart polymath-dashboard" (it restarts itself within a minute anyway)
```

## C.4 What it may do (tiers)

**Tier 1: it does these itself.** Each is bounded, logged in `changelog` (area `caretaker`), reversible, and
goes only through the job allowlist or inbox ops:

| Remedy | When | Bound |
|---|---|---|
| Requeue dead jobs of a kind | their cause is gone (network back, disk freed) | once per job; ≤ 50 per turn |
| Quarantine a job kind (stop its recurring schedule for 6 h) | it crashed ≥ 3 times in 1 h with the same error | ≤ 3 kinds at once; core kinds (`body.*`, `eval.holdout`, `net.probe`) never |
| Run maintenance now: `body.housekeeping`, `body.evict`, `body.backup` | disk filling, backup stale or failed | each ≤ once per 6 h |
| WAL checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`, the agent owns the writer) | WAL above 2 × `journal_size_limit` | ≤ once per 10 min |
| Slow down: lower `loop.intensity` through kv `caretaker_slowdown` (0.5–1.0) | heat rising toward throttle, memory near `MemoryHigh` | restored automatically when back in band for 1 h |
| Pause the society (not itself) | runaway agent CPU or writes, H5 | ≤ 1 h, then re-check |
| Probe the internet now (`net.probe`) | it suspects offline detection is stale | ≤ once per 5 min |
| Rebuild an index (`perception.automaton`, `embed.rebuild`) | an index file fails to load or validate | ≤ once per day each |
| Roll back a self-tuned setting (`changelog --reset` path) | quiz accuracy dropped after that change, beyond the existing guard | the knob is pinned until Christian allows it again |
| Drop a site (`sites.status = dropped`) | security: it served unvetted redirects or content far off-domain | can only make the allowlist **smaller** |
| Refresh the safety lists (`web.blocklists`) | lists older than 14 days | weekly |

**Tier 2: it tells Christian, with the exact step.** It never does these:

| Finding | Message (example) |
|---|---|
| Brain drive on USB 2 | "Your SSD runs at USB 2 speed (480 Mbit/s). Move it to a blue USB 3 port and use a USB 3 cable: reading gets about 5× faster." |
| Under-voltage | "The Pi reported under-voltage 6 times today. Use the official 5.1 V / 3 A power supply; weak power corrupts SD cards." |
| Too hot | "It reached 79 °C three times today. Add a heatsink or fan, or keep the case open." |
| Dashboard hung repeatedly | "The dashboard restarted 4 times today. Details: `journalctl -u polymath-dashboard -n 80`. Restart now: `sudo systemctl restart polymath-dashboard`." |
| Agent crash loop | "The agent restarted 5 times in an hour; last error: … Run `polymath doctor`, then `journalctl -u polymath -n 80`." |
| Brain drive missing | "The brain's drive is unplugged or not mounted: plug it in, and Polymath starts by itself." |
| SD-card mode on (v0.7.0; `storage/sd_card`, once a day) | "Polymath runs on the SD card since 15 Oct (11.4 GB of room, 6.2 GB free). Once the SSD works again (blue USB 3 port, USB 3 cable, no USB 2 hub): `cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh off`. What it learned on the SD card moves onto the SSD by itself." |
| Brain drive keeps freezing | "Your SSD froze 3 times today. Until it is fixed, Polymath can run entirely on the SD card: `cd ~/polymath-src/polymath && sudo ./scripts/sd-card-mode.sh on` (nothing on the SSD is touched)." |
| SD card failing (read-only remount, I/O errors) | "The SD card was remounted read-only, the usual sign it is failing. Back up now: `polymath backup`, then replace the card." |
| Update installed but not running | "v0.6.0 is installed but the agent still runs v0.5.1: `sudo systemctl restart polymath`." |
| Config changed | "/etc/polymath/polymath.toml changed at 14:02 (3 keys). If that was not you, check who has sudo." |
| Dashboard open to the network | "The dashboard answers on all networks without a token. If the Pi is on a shared network, set `dashboard.host = \"127.0.0.1\"`." |

**Never** (enforced by tests; it has no code path to any of these):
- running commands or opening process, `ctypes` or socket APIs outside `senses/net.py`;
- writing code, roles, fault models or config;
- changing permissions;
- `systemctl`, mounting or formatting (H8);
- widening the crawl scope or disabling safety lists;
- deleting user data or backups;
- changing hard ceilings;
- touching other devices on the network.

## C.5 Security duties

- **Watch the boundary:** outbound hosts must be vetted sites or known sources.
  - Anything else becomes a security incident, and Christian is told what fetched it.
  - The crawl allow-list may only grow by Christian (`polymath learn <url>`).
  - Every change is diffed against the last known list.
- **Watch the integrity of the install:**
  - the running build matches `build.json`;
  - the package directory is not writable by `polymath`, checked from the mode bits;
  - the config hash is recorded and any change is announced.
- **Watch exposure:** the dashboard binding and token (D6); `/api/ask` abuse (rate-limit hits from one address);
  malformed inbox files (someone writing to the spool).
- **Respond only by narrowing:** drop a site, quarantine a job kind, pause the society. Everything else is a
  Tier 2 message.

## C.6 The fundamentals it is taught (data in `faults.toml` and code constants, cited where they come from)

- **Raspberry Pi 4:**
  - the firmware soft-throttles at 80 °C and hard-throttles at 85 °C; Polymath throttles at 75 and pauses at 82
    (78 after S0, D7);
  - under-voltage is below about 4.63 V and shows as the `in0_lcrit_alarm` flag;
  - USB 3 ports are blue (5 Gbit/s), USB 2 is 480 Mbit/s;
  - SD cards wear and fail by going read-only.
- **Linux:**
  - ENOSPC means the disk is full; EROFS means remounted read-only (failing medium);
  - an OOM kill shows as a sudden restart with no Python traceback;
  - systemd's watchdog restarts a service that stops pinging.
- **SQLite:**
  - in WAL mode readers never block the writer;
  - a long reader blocks checkpoints, so the WAL grows;
  - `quick_check` finds corruption;
  - `freelist_count` is reclaimable space;
  - "database is locked" after `busy_timeout` means a stuck writer.
- **Networks:**
  - no DNS, no route, or refused means the internet or the router;
  - an HTTP 5xx is the server's problem;
  - 429 means slow down;
  - a TLS failure means a clock or certificate problem. It checks the Pi's clock against HTTP `Date` headers
    and reports more than 5 minutes of skew.
- **Polymath itself:**
  - job states and leases;
  - slices commit atomically;
  - what each job kind does and whether it needs the internet;
  - the healthy ranges of its own learning signals.

## C.6b Its knowledge base (built in v0.6.1): `polymath/agents/faults.toml`

It holds everything Polymath has learned about going wrong, so the caretaker starts out knowing it. Each update of
Polymath updates it in the same commit; `tests/test_faults.py` fails otherwise (rule: `polymath/CLAUDE.md`).

- **`[[fault]]`**, one per known problem. Each has:
  - the issue codes it matches (globs) and the words that recognise it in text (install output, doctor lines,
    dmesg);
  - the symptoms, and likely causes with priors (these seed C.3 step 2);
  - checks (C.3 step 3);
  - what Polymath already does about it by itself;
  - steps tagged `auto`, `caretaker` (Tier 1), `you`, `sudo` (Tier 2: tell, with the exact command) or
    `approval` (changes the system: always ask Christian first).
- **`[situation]`**: what is true on the Pi right now, what Christian still has to do, and the suspected root
  cause.
- **`[[history]]`**: every incident on the real Pi, in order, with its cause, its fault and the version that
  fixed it. `open = true` until it is confirmed fixed on the Pi.
- **`[[update]]`**: what each release changed that the caretaker should know.

**Used today, before Orin exists:**
- `core/issues.report` tags every problem with its faults;
- `polymath doctor`'s problems line gives the known fix;
- `polymath caretaker` gives the briefing (situation, recent problems with what each is, open incidents);
- `polymath caretaker known` lists the known problems, `show <id>` gives the full entry, and `history` lists
  the incidents.

**What Orin does with it:**
- **Hypotheses:** the fault entries are its starting hypotheses. Their priors are updated by resolved
  incidents (C.3 step 4).
- **New problems:** a problem with no known fault opens an incident marked "new to me". When it is resolved,
  Orin drafts a `[[fault]]` entry for Christian to approve. Orin never writes the file itself (C.4: no writing
  fault models).
- **Escalations:** these quote the matching fault's steps word for word, `needs` tag included.

## C.7 Its track record (it earns trust like every agent)

- **Verified outcomes** (spec §3.6 `VERIFIED_KINDS` += `incident`):
  - **success:** an incident resolved by its Tier 1 remedy and not back within 24 h;
  - **success:** an escalation Christian confirms (`polymath agents feedback orin correct`);
  - **failure:** a false alarm (closed by itself within one window with no action) or a remedy that failed.
- Its alarm thresholds (`k` in C.3) are a `selftune.Knob` (2–8). Too many false alarms raise `k`; missed
  incidents (found later by `doctor` or by Christian) lower it.
- **Every incident is kept:** table `incidents`, with symptoms, hypotheses, checks, actions, verification and
  outcome, and 1 year retention. You can read them with `polymath agents why orin` and
  `polymath caretaker incidents`.

## C.8 Where it shows up

- **Feed:** a `caretaker` tag and a new face mood "mending" (spanner accessory) while it works an incident;
  "proud" when one is verified fixed.
- **`polymath doctor`:** a section with open incidents and the caretaker's latest advice.
- **Mission control:** the caretaker's row is pinned to the top of the roster.

## C.9 Persistence

The next free migration (after 0018–0021, or wherever it lands in the order rule §12) adds:
- `incidents`: id, key, area, opened, updated, state (`open`, `checking`, `acting`, `verifying`, `resolved`,
  `escalated`, `closed`), symptoms JSON, hypotheses JSON, actions JSON, outcome;
- `fault_stats`: cause, remedy, a, b;
- `signal_bands`: signal, median, mad, n, updated.

All three are small (fewer than 10k rows a year).

## C.10 Tests (new `tests/test_caretaker.py`)

- **The incident loop:**
  1. Detection opens exactly one incident per root symptom and none for a single blip.
  2. Bayes ranking on a fixture fault model matches hand-computed posteriors.
  3. The value-of-information check choice picks the discriminating check.
  4. A Tier 1 remedy is applied once, logged, and verified; on failure the next remedy is tried.
  5. Escalation text has all four parts: what, evidence, cause with probability, exact step.
- **Safety:**
  6. Every Tier 2 cause has no code path that acts. An AST scan finds no `subprocess`, `os.system`, `systemctl`,
     writes under the package directory, or permission changes in `agents/caretaker*.py`.
  7. Security responses only ever narrow the allow-list (fuzzed incidents).
  8. Quarantine never touches core kinds, and the bounds in C.4 hold under adversarial signal streams.
- **Behaviour:**
  9. The learned bands adapt (a slowly growing brain does not raise alarms).
  10. It still runs during `society pause`; `society stop` stops it.
  11. **USB 2 detection** from a fixture `/sys` tree produces the "move it to a blue port" message.
  12. Each Tier 1 remedy has an end-to-end drill on a real agent process, like v0.5.1 §12f.

## C.11 When to build it

Right after **S1** (it needs the role, name and caps machinery, not the blackboard or mailbox), as stage
**S1b**. It is the stage that does most for "operational no matter the situation". Its mail and blackboard
hooks are added when S2/S3b land.

## C.12 Questions for Christian (each has a default; the build does not wait)

- **C-Q1.** May it slow the agent down by itself when the Pi runs hot? Default **yes** (Tier 1, restored
  automatically).
- **C-Q2.** Should escalations also go to your phone? That needs a channel; there is none today, so the default
  is the feed, `doctor`, and the dashboard.
- **C-Q3.** For security incidents, should it pause open-web learning entirely until you look? Default: only
  drop the offending site.
