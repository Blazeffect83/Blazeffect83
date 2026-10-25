# Polymath

An autonomous learning agent for the Raspberry Pi 4/5 that teaches itself from public data, around the clock,
and answers questions about what it has learned — **without** an LLM, external AI services or pretrained
models. Every intelligent part (tokenizer, stemmer, sentence splitter, phrase finder, entity linker, relation
extractor, word embeddings, vector index, truth discovery, rule learning, curiosity, self-evaluation) is
implemented here and learns only from data the agent downloads itself.

- **Runtime dependencies:** Python 3.11+ standard library and **numpy**. That is all (a test enforces it).
- **Data:** Wikipedia, Wikidata, OpenAlex, PubMed, Project Gutenberg, Stack Exchange dumps, RSS/Atom feeds and
  a polite crawler (robots.txt, ≤ 1 request/s per host). See [docs/SOURCES.md](docs/SOURCES.md).
- **Grows onto any drive you plug in:** a USB SSD, HDD or stick becomes extra brain space automatically. A blank
  drive, or a new SSD straight from the shop (exFAT/NTFS with only the maker's software on it), is formatted
  for Linux and the whole brain moves onto it. It is formatted **once per drive, ever**: unplugging, a power
  cut or a replug never formats it again. On a drive with your files, only free space is used and the files
  are never touched. It can start on the SD card (`--allow-sd-card`).
- **Learns from the open web, safely:** new sites come only from links Wikipedia cites, and each is checked
  before its first visit: offline safety lists (adult, malware, phishing, gambling), a valid HTTPS
  certificate, robots.txt. Then it stays on probation until its facts check out.
- **Learns from its mistakes:** every wrong self-test answer is read up on and re-tested.
- **Tells you what it learned:** a daily digest in the feed, the dashboard, and `polymath digest`, and every
  Sunday a recap of the week against the week before (`polymath recap`).
- **Tells you about anything it knows:** `polymath tell "Marie Curie"` writes a short encyclopedia-style
  paragraph from its facts ("Curie was born in Warsaw on 7 November 1867 …") with numbered sources.
- **Predicts facts before it reads them:** it guesses missing facts, checks the guesses when the facts arrive,
  and keeps score against chance (`polymath predictions`). On earlier real data it got 25 of 27 hidden facts
  right, where guessing would score 23 %.
- **Did you know?** It flags facts that surprised its own model, such as the largest or smallest of their kind,
  or a different answer from what everything else it knew pointed to.
- **Knowledge map:** an interactive star map of what it has read about on the dashboard, with a time-lapse of
  its brain growing. Click a topic and it tells you what it knows.
- **Watch from your PC's browser, privately** (v0.9.0): `https://<pi>:8443/` in Chrome on your home network, after
  pairing once with an 8-character code that `install.sh` (or `polymath access pair`) shows. Only paired browsers
  get in: HTTPS with the Pi's own certificate, home network only (anything from the internet is refused before it
  reaches the dashboard), a revocable key per device (`polymath access devices` / `revoke`), and a log of every
  refusal. Install it as a Chrome app for its own window. The sign-in-free page stays on the Pi itself.
- **Makes the SD card last** (v0.9.0): in SD-card mode it backs up weekly and keeps one backup (instead of two
  nightly ones, about 5 GB of writes a night), keeps 3 GB free, and forecasts when the room would run out
  (`polymath storage usage`, the dashboard, and the caretaker, who acts three days ahead).
- **Watch from your phone:** `https://<pi>:8443/live` (paired like the PC) shows the face and the live feed.
- **Busy, never falsely offline** (v0.8.2): while one long step works (a backup, a quiz, a big query on the SD
  card), a small watchdog thread vouches for it, and the feed says `one long step: 4 min so far — busy, not stuck`
  instead of `agent offline`. A really stuck agent is still restarted. Quizzes and search are about 3× faster.
- **See what it is learning, and how far along** (v0.8.1): the feed, the dashboard and `/live` name the topic it
  is on and who chose it (curiosity, you, a mistake, an agent), the article it just read, a bar for how well it
  knows the topic (and what this round added), and a bar for the round itself (`2 of 4 articles`).
- **Protects the SD card:** it measures how much it writes and estimates how long the card lasts at that rate.
  It writes less (idle cycles now write nothing), and when you plug in a drive, the whole brain moves onto it.
- **Watch it learn:** at login a terminal opens with a live feed of everything it reads, learns, infers,
  tests itself on and gets rewarded for. A little animated face at the top shows its mood (reading, thinking,
  proud of a fixed mistake, wide-eyed at a new drive, looking around when one is unplugged, dozing when idle), and the top right shows the exact version it runs, with ✓ when
  the agent runs the installed update.
- **Keeps the Pi healthy:** at most 2 cores and 3 GB, `nice 10`, so the desktop stays responsive. It throttles
  at 75 °C and pauses at 82 °C.
- **Survives power loss:** every step commits atomically (SQLite WAL); jobs resume where they stopped.
- **Always on** (v0.5.1):
  - **No internet:** it notices, holds back everything that needs the internet (no download is ever given up),
    and studies what it already has: reasoning, checking its facts, re-training, quizzing and tuning itself,
    more often than usual. When the internet is back it resumes reading by itself.
  - **The dashboard cannot freeze the feed:** slow pages are stopped, counting happens in the background, and
    systemd restarts the dashboard within a minute if it ever stops answering.
  - **The live feed never goes dark:** if the dashboard is down, the feed reads the agent's own snapshot
    instead and tells you the one command that fixes it.
  - **The brain's drive unplugged or dropped out:** nothing goes dark. The feed says which drive is missing,
    and Polymath starts by itself the moment it is back (v0.5.2).
  - **It brings its drive back by itself:** a dead connection, a drive reporting 0 bytes, or an enclosure stuck
    in its safe-off state after a reboot: it resets the drive, and as a last resort cuts and restores the USB
    power, like a replug (v0.5.5).
  - **A frozen drive never freezes it:** every look at the brain's drive has a time limit (v0.5.7).
  - **It keeps learning when the drive is gone** (v0.6.0): after 5 minutes without its drive, it starts a small
    *standby brain* on the SD card and keeps reading (feeds, the web, single articles; no big dumps, at most
    3 GB, wear-guarded). When the drive is back, everything it read moves onto it by itself, and the real brain
    reads it again with all it knows. The feed shows `learning on standby`.
  - **Orin the Caretaker looks after it** (v0.8.0). Every two minutes, even during a pause, Orin:
    - checks the Pi and Polymath (heat, disk, memory, the database, backups, the internet, how much it reads, its
      self-test scores, failing jobs, every reported problem, power, USB speed, the install);
    - learns what is normal for this Pi;
    - finds the most likely cause of anything unusual with a few read-only checks;
    - fixes what it may itself (slow down, free room, back up, retry jobs that died of a passing problem, pause a
      job that keeps failing), then checks that the fix held;
    - tells you the exact step for everything else, once a day at most.

    It never runs commands, never edits files or permissions, and never widens what Polymath may visit. See
    `polymath caretaker` and the `Orin` lines in the feed and in `polymath doctor`.
  - **SD-card mode, for a drive that cannot be trusted** (v0.7.0): `sudo ./scripts/sd-card-mode.sh on` runs the
    whole brain on the SD card. It lets go of the SSD and switches its USB device off, and the SSD's brain is not
    touched. It carries on from the copy the SD card kept when the brain moved onto the SSD, installs, and checks
    that it runs. Everything runs as usual, dumps included, inside what the card can give (all but 2 GB). The feed
    shows `learning on the SD card`. Once the SSD works again, `sudo ./scripts/sd-card-mode.sh off` switches back,
    and what it learned on the SD card moves onto the SSD by itself.
  - **Decides which drive does what** (v0.11.0): with several drives plugged in, it picks the brain (the fastest,
    steadiest Linux drive of its own on USB 3), a different drive for the backups, and uses the rest as extra room.
    It moves the brain onto a clearly better drive at night, or at once off one that keeps dropping out
    (`polymath storage roles` says what and why).
  - **A wiped brain drive is no dead end** (v0.11.2): reformat the SSD and the SD card's brain takes over (or the brain
    is rebuilt from its newest backup on another drive), then moves onto the best drive by itself. Empty drives
    formatted for Windows/macOS are reformatted for Linux by themselves; a drive with your files never is.
  - **Knows what each drive holds, and relearns a lost one** (v0.11.0): if a drive is gone over two hours, what it
    held is read again onto the other drives, the documents most facts rest on first; plugged back in, it is all
    readable again.
  - **Back onto the SSD by itself, with nobody at the Pi** (v0.10.0, the SSD watch): in SD-card mode it watches for
    the SSD every minute and says what it sees (`SSD not plugged in`, `needs more power`, `SSD on USB 2`). Plugged in
    at USB 3, the SSD is checked every minute (reads from its start, middle and end, no I/O errors); after ten
    healthy minutes the brain moves back onto it by itself. If it drops out twice within a day of coming back, the
    brain goes back to the SD card by itself and the SSD is tested again a day later. Nothing is formatted or deleted.
  - **Every problem is recorded** and shown as a `problem` line in the feed, ready for the coming caretaker agent.
    Each one is matched against the caretaker's knowledge of known problems and their fixes
    (`polymath caretaker`), which every update keeps current (v0.6.1).
  - `polymath doctor` checks every part and says exactly what to do. Services restart forever, waiting longer
    between attempts.
- **Improves itself from what it learns** (never by editing its code):
  - it tunes its own settings with fair A/B tests and undoes a change if its self-test score drops;
  - it drops rules whose conclusions sources keep contradicting;
  - it spawns specialist agents for its weak spots;
  - it reads more of what teaches it most and learns how to phrase sentences from what it reads.

  Every change is logged with its numbers: `polymath changes`, the dashboard, the feed.
- **Reasons, not just looks up** (v0.5), every answer with its sources:
  - multi-step questions: "Who is the head of government of the capital of France?";
  - numbers and units: "Is Mount Everest taller than K2?" (feet and metres compared properly), "How many times
    bigger is Brazil than Portugal?", population density;
  - places: distances, "Which is further north?", "What is near the Eiffel Tower?";
  - time: facts with dates (Germany's capital was Bonn *until 1990*), ages, "Which came first?", who lived at the
    same time, what happened in a year;
  - why: what causes something and what it leads to; kinds of things and what they can do, with exceptions ("Can
    penguins fly?"); how-to steps from Stack Exchange.

  It also catches facts that cannot be true (died before being born, a city bigger than its country, numbers far
  outside their normal range), merges duplicate entries, reads Wikipedia's data tables, cross-checks a second
  Wikipedia language, and switches to big-brain mode on a large drive.
- **Answers offline**, with citations and confidences, and says "I don't know yet" instead of guessing.

![Live feed](docs/img/feed.png)

![Knowledge map](docs/img/knowledge-map.png)

![How it improved itself](docs/img/self-improvement.png)

## Install (Raspberry Pi OS Bookworm, 64-bit)

Best with an NVMe drive mounted at `/srv/polymath` ([docs/OPERATIONS.md](docs/OPERATIONS.md#nvme)). No drive
yet? Use `sudo ./install.sh --allow-sd-card`, then plug drives in later; they are added by themselves. Is a drive
there, but keeps freezing? Run everything on the SD card until it is fixed: `sudo ./scripts/sd-card-mode.sh on`
([docs/OPERATIONS.md](docs/OPERATIONS.md#sd-card-mode)). Then:

```bash
git clone --branch claude/polymath-agent https://github.com/Blazeffect83/Blazeffect83.git
cd Blazeffect83/polymath
sudo ./install.sh
```

That's it. The agent and dashboard start now and at every boot. The desktop logs in automatically and opens
a terminal with the **live feed**. The browser dashboard (charts, topics, agents, ask box) is in the menu. For
your PC, the install ends with a pairing code and the address to open (`https://<pi-address>:8443/`): type the
code in once and that browser is in ([docs/OPERATIONS.md §3](docs/OPERATIONS.md#browser)).

## Use

```bash
polymath feed                                    # the live feed (what the terminal at login shows)
polymath ask "What is the capital of France?"   # offline, with sources
polymath ask "Is Brazil bigger than Angola?"     # comparisons, distances, ages, causes, kinds, how-to, chains
polymath learn "black holes"                     # research a topic as a priority (or give a URL)
polymath topics --weakest                        # where its knowledge is thinnest
polymath why "Black holes"                       # why it is (or is not) working on something
polymath status                                  # health, queue, knowledge counts
polymath version                                 # installed version and commit; is the agent running it?
polymath storage list                            # the drives in its brain
polymath digest                                  # what it learned today
polymath recap                                   # the week in review (this week against last week)
polymath tell "Marie Curie"                      # a paragraph written from what it learned, with sources
polymath predictions                             # facts it guessed before reading them, and how many came true
polymath wear                                    # disk writes per day; how long the SD card lasts at this rate
polymath sites                                   # new sites it vetted, refused or dropped, and why
polymath changes                                 # what it changed about itself, and why (--reset NAME to overrule)
```

### Agents with their own directives

Spawn as many specialised agents as you like. Each one learns on its own and earns rewards **only when its work is
verified correct**: against hidden facts, against facts that arrive later in the dumps, or against your verdict.
Rewards add up to XP and levels. Agents that do well get more of the CPU and fork mutated children that compete
with them. A child that does better passes its traits back to your agent.

```bash
polymath agents spawn "research black holes"          # also: "watch news about SpaceX", "fact-check populations",
polymath agents spawn "predict the country of cities" #       "answer questions about chemistry", …
polymath agents task research-black-holes "What is a quasar?"
polymath agents feedback 42 correct                   # your verdict is a reward (or a penalty)
polymath agents list                                  # levels, XP, right / wrong, status
polymath agents show research-black-holes             # what it learned to prefer, recent tasks
```

`polymath --help` lists the rest (`report`, `backup`, `restore`, `sources`, `sample`, …).

## Documentation

| | |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | the loop, the database, how the parts fit together |
| [ALGORITHMS](docs/ALGORITHMS.md) | every learning algorithm, from scratch, and why |
| [SOURCES](docs/SOURCES.md) | where knowledge comes from, licenses, politeness |
| [OPERATIONS](docs/OPERATIONS.md) | install, NVMe, logs, backups, restore, tuning, uninstall |
| [VERIFICATION](docs/VERIFICATION.md) | what was measured, where, and what is still pending |
| [CARETAKER](docs/CARETAKER.md) | the caretaker agent (Orin) and its knowledge of every known problem and fix |

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest --cov=polymath && .venv/bin/ruff check . && .venv/bin/mypy polymath scripts
.venv/bin/python scripts/benchmark.py            # measures every subsystem on this machine
```
