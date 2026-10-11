# Algorithms

Everything below is implemented in this repository on top of the Python standard library and numpy.
No model, embedding or weight file was downloaded, and nothing was pretrained: each learning component
starts empty and learns from the documents the agent reads. (The only fixed lexical resource is a 116-word
English function-word list used by the HTML boilerplate filter.) Where a constant is a design choice, the value and its reason are given.

## Senses

- **Wikipedia multistream** (`senses/wikipedia.py`).
  - The dump's index lists `offset:page_id:title`, and each bz2 stream of ~100 pages is fetched with an HTTP range request and decompressed alone.
  - Dumps are pinned through `dumpstatus.json`, so only finished, dated dumps are read.
  - The title index is stored, so single articles can be fetched later for targeted reading.
  - Its own wikitext cleaner handles templates, links, references, tables and infoboxes.
- **Wikidata JSON** (`senses/streams.py`).
  - The 100+ GB dump is one bz2 stream, which normally cannot be resumed.
  - The reader scans for the 48-bit bz2 block magic at **bit** granularity, decodes blocks independently, and checkpoints `(block_bit, byte_offset)`.
  - A restart therefore resumes in milliseconds instead of re-reading gigabytes.
- **Wikidata properties** (`senses/wikidata_props.py`).
  - Property records arrive late in the JSON dump, so the XML multistream index (~100 parts × ~5 MB) is scanned for `Property:` pages.
  - Only the streams holding them are range-read; properties already used by known facts come first.
- **7z** (`senses/sevenzip.py`). Stack Exchange archives use 7z. The reader parses the container (headers, streams, folders, CRCs) and decodes LZMA/LZMA2 with `lzma`.
- **Crawler** (`senses/crawler.py`, `robots.py`, `html_text.py`).
  - A robots.txt parser follows RFC 9309: longest match, `$` and `*` wildcards, `Crawl-delay`.
  - Each host has a persisted token bucket of capacity 1 at ≤ 1 request/s, and the frontier lives in SQLite.
  - The main-text extractor classifies blocks jusText-style (length, link density and stop-word density, using a short built-in list of English function words — the one fixed word list in the system, used only to tell prose from navigation; navigation class names are also penalised) and keeps the good ones and their good neighbours.
  - It also takes the title, meta description, language, canonical URL and the license link (`rel=license`, Creative Commons).

## Memory

- **Passages and full text** (`memory/text_index.py`).
  - Documents are cut into ~900-character passages at paragraph and sentence boundaries.
  - Passages go into a *contentless* FTS5 table, so text is not stored twice; bodies live lzma-compressed.
  - Query planning drops unknown words, prunes terms in > 3 % of passages (keeping the rarest), and rejects queries whose rarest term is in > 25 % of passages.
  - `fts5vocab` provides the document frequencies.
- **Near-duplicates** (`memory/dedup.py`).
  - Exact duplicates are caught by a normalised content hash.
  - Near duplicates are caught two ways, both over weighted word 3-shingles:
    - 64-bit SimHash (Charikar);
    - MinHash, 128 permutations with LSH banding of 16 bands × 8 rows, near-duplicate at ≥ 0.85 estimated Jaccard.
- **Knowledge graph** (`memory/graph.py`).
  - Triples `(s, p, o | value)` carry a status (`sourced`, `inferred` or `disputed`), a confidence, a hold-out flag and a provenance row per evidence: source, document, kind, weight.
  - Aliases are normalised surface forms with counts and sources.
- **Topics** (`memory/topics.py`). A DAG built from Wikipedia categories, MeSH headings, OpenAlex concepts and Stack Exchange tags, with depth levels and subtree document counts rolled up.

## Perception

- **Tokenizer** (`perception/tokenize.py`). Unicode-aware, with offsets; it keeps abbreviations' periods and ellipses intact.
- **Porter stemmer** (`perception/stem.py`).
  - The original 1980 algorithm.
  - It is verified identical to SQLite FTS5's `porter` tokenizer on 42,515 distinct real words, so query terms and index terms always agree.
- **Sentence splitter** (`perception/sentences.py`).
  - Punkt (Kiss & Strunk 2006), unsupervised: it learns abbreviations from the ratio of a word's occurrences with and without a final period.
  - It also learns collocations and frequent sentence starters (Dunning log-likelihood).
  - Extra rules handle quotes, initials and "Jan. 5".
- **Phrases** (`perception/phrases.py`).
  - Bigram and trigram counts, scored with normalised PMI.
  - Phrases above the threshold and frequency floor become single tokens ("new_york").
  - Stopwords are the corpus's own most frequent words, not a list.
- **Aho–Corasick over tokens** (`perception/ahocorasick.py`).
  - Every alias is compiled into one automaton over CRC32 token hashes, stored as numpy CSR arrays (`.npz`).
  - It finds every alias in a document in one pass, at ~1M tokens/s.
- **Entity linking** (`perception/entities.py`).
  - Candidates come from the automaton.
  - Logistic regression over 12 features:
    - prior P(entity | surface) and whether this is the top prior;
    - context tf-idf similarity with the entity's profile;
    - Milne–Witten coherence with the other candidates in the document;
    - keyphraseness (how often the surface is linked when it appears);
    - capitalisation and exact label match;
    - number of candidates and popularity;
    - whether the entity has an article;
    - span length.
  - Training data is the agent's own Wikipedia anchors, with leave-one-out profiles so an anchor never sees itself.
- **Infobox facts** (`perception/infobox.py`).
  - Infobox fields are parsed into typed values.
  - The mapping field → Wikidata property is *learned* by counting agreements on entities known from both sources.
- **Keywords and summaries** (`perception/textrank.py`). TextRank over co-occurrence and sentence-similarity graphs.
- **Relations** (`perception/relations.py`).
  - DIPRE/Snowball bootstrapping: known facts seed the textual patterns between their entity mentions, and patterns are scored by Snowball confidence (positives vs negatives).
  - Patterns with confidence ≥ 0.7 extract new facts with `pattern` provenance.

## Embeddings

- **SGNS** (`perception/embeddings.py`).
  - Skip-gram with negative sampling (Mikolov 2013) in numpy: dynamic windows, frequent-word subsampling (t = 1e-4), and negatives drawn from the unigram distribution to the ¾ power.
  - Negatives are *shared per mini-batch* (K = 4 × negatives, each weighted negatives/K). This turns the update into dense matrix products, about 3× faster than per-pair negatives on the Pi's NEON.
  - The batch is capped at 4 × vocabulary size, which keeps small vocabularies stable.
- **Document vectors.** Smooth inverse frequency (Arora 2017) with the first principal component removed.
- **Vector index** (`memory/vector_index.py`).
  - IVF built with spherical k-means; vectors are stored as int8 with a per-vector scale in memory-mapped generations.
  - A tail file holds recent additions until the next rebuild; nprobe is 32.
  - Measured: 1M vectors at top-10 p50 1.2 ms, recall@10 0.97 on clustered data.
- **Quality checks** are generated from the graph, with no labelled data needed:
  - alias-pair similarity AUC;
  - fact neighbours hit@10 vs random;
  - offset consistency per predicate.

## Reasoning

- **Truth discovery** (`reasoning/reliability.py`).
  - A fact's belief is a noisy-OR over its sources' reliabilities.
  - A source's reliability is the Beta(4, 1)-smoothed mean belief of what it asserts.
  - Ten alternating iterations (TruthFinder-style). For a **functional** predicate — learned: ≥ 90 % of at least 20 subjects have one value — the competing values' confidences are renormalised so they cannot all be near 1.
- **Contradictions** (`reasoning/contradictions.py`).
  - Incompatible values of a functional predicate are compared with tolerance: dates by precision, quantities ±5 % in the same unit, normalised text.
  - When one side leads by a 0.25 margin it wins; otherwise both become `disputed` until more evidence arrives.
- **Rules** (`reasoning/inference.py`).
  - Declared by the properties themselves:
    - transitive (instance of Q18647515);
    - symmetric (Q18647518);
    - inverse (P1696).
  - Learned from statistics:
    - transitivity: closed / open 2-chains ≥ 0.5;
    - symmetry ≥ 0.8;
    - domain and range classes ≥ 0.7 of subjects or objects.
  - **Forward chaining** is semi-naive: only facts newer than a cursor are joined, and each conclusion records the rule and premises in its provenance.
  - Domain and range rules reject type-violating conclusions.
- **Link prediction** (`reasoning/link_prediction.py`). To score candidate objects for `(s, p, ?)`, five experts each give a distribution or abstain:
  1. **graph** — the fact itself is known; a hidden fact counts only through independent evidence (re-derived by a rule, read in text).
  2. **related** — other facts connecting s and o.
  3. **association** — naive Bayes over s's other facts: P(o | p) · Π P(f | p, o), estimated from other subjects, with Laplace smoothing.
  4. **text** — same-sentence co-occurrence with lift (÷ √ frequency of the candidate), plus a bonus when the relation's name appears.
  5. **embedding** — translation: the mean offset v(o) − v(s) over known facts of p, applied to s.

  They combine as a product of experts, Σ wₘ log pₘ(o).

  **The weights are learned**, by maximum likelihood, from quizzes on *visible* facts. Each asked fact is hidden from every method while it is asked (leave-one-out); this is recalibrated daily.

## Reasoning skills (v0.5)

- **Units** (`reasoning/units.py`): Wikidata unit Q-ids and infobox unit text map to a base unit per dimension
  (length, area, mass, time, volume, speed; temperature with its offset). Comparisons happen only within one
  dimension; ratios and differences are shown back in readable units.
- **Comparisons** (`qa/numbers.py`): an adjective selects relations in order of preference (taller → height,
  elevation…; bigger → area, population, mass, length), and the first relation both things have in the same dimension
  decides. "Older" compares the date each began (birth, inception, publication, point in time, start, discovery).
- **Geography** (`qa/places.py`): great-circle distance by the haversine formula (Earth radius 6,371.0088 km);
  east/west compares longitudes the short way round (across the date line); "near" scans a latitude/longitude box
  around the place (50 km) in the `geo` table and sorts by true distance.
- **Time** (`reasoning/temporal.py`, `qa/when.py`): dates compare as (year, month, day) with BC years negative. A fact is
  ended when its end time is before today. Contemporaries: lifespans that overlap by at least a year, among the
  best-known people.
- **Multi-step** (`qa/chains.py`): "the R1 of the R2 of X" is resolved from the inside out, trying every " of " split
  (relations whose names contain "of", like "head of government", work); every hop takes the current, best-supported
  value. "Which C <verb> <phrase>" finds things of kind C (or a subclass) linked to the phrase's thing either way,
  ranked by word overlap of relation and verb and by the phrasings learned from reading.
- **Causes and kinds from text** (`perception/semantic.py`): strong cue phrases between two linked things (causes,
  leads to, results in, caused by, due to, prevents; is a, such as, and other — Hearst patterns). The second thing
  must end its noun phrase, a kind must be a known class and not said of a person, and a claim is used only once two
  sentences agree.
- **Inheritance with exceptions** (`qa/kinds.py`): breadth-first up instance of / subclass of / "is a kind of" (depth
  6). For "can X do V", the nearest kind with a statement about V decides (votes weighted by how often it was read);
  a farther statement that disagrees is reported as the exception.
- **Impossible facts** (`reasoning/sanity.py`): fixed rules (death before birth, lifespan over 122 years, dates in
  the future, a child born before its parent was 10, a part larger than the place it lies in) and learned ranges
  (log scale, median ± 8 × 1.4826 × MAD per relation and unit, from up to 20,000 values; negatives where 99 % are
  positive). The weaker side (lower confidence; text before Wikidata) is disputed; Wikidata against Wikidata is noted.
- **Duplicates** (`memory/merge.py`): a placeholder merges into a Wikidata item on redirect evidence, or when exactly
  one item has its title as name or article title and no shared date disagrees.

## Self-improvement

- **Self-tuning** (`drive/selftune.py`).
  - Paired test: the current value and the candidates answer the *same* 60 hidden-fact questions; the mean
    paired difference d̄ and its standard error SE are computed per candidate, and the candidate with the best
    lower bound d̄ − 2·SE is chosen.
  - Adopted only when d̄ > max(minimum effect, 2·SE).
  - The link-predictor settings are judged on the log-likelihood of the clue they control (association), not
    on the combined answer, because the combination can weight a weak clue to zero and hide any change. They
    hill-climb: each trial tests half and double the current value.
  - `predict.min_confidence`: one pass collects (confidence, right, had a view) per question; every threshold
    is scored from it with the predictions reward (+2 right, −0.8 wrong, 0 abstain).
  - `infer.min_confidence`: from the rule audit's confirmed/refuted counts per 0.05 confidence bucket, the
    lowest threshold whose kept conclusions are ≥ 80 % precise (Laplace-smoothed, ≥ 20 judged).
  - **Safety net:** an adopted value is *watching* until 2 self-tests have run; if their mean accuracy is more
    than 5 points below the mean of the 3 before, the old value is restored.
- **Rule trust** (`reasoning/rule_audit.py`).
  - Per rule (inverse rules are one rule both ways), up to 3,000 recent conclusions are judged:
    *confirmed* when a non-rule source states the same fact; *refuted* when it is disputed, or the relation is
    strictly single-valued (≥ 50 subjects, ≥ 95 % with exactly one sourced, visible value) and a sourced,
    visible fact gives another value. Hidden quiz facts never count.
  - trust = (confirmed + 1) / (judged + 2); refuted conclusions already withdrawn keep counting.
  - Demoted at trust < 0.5 with ≥ 8 judged; promoted again at ≥ 0.65.
- **Specialists** (`drive/specialists.py`). Over 14 days of self-test answers, a relation with ≥ 12 answers is
  weak when its accuracy is < 50 %, or 20 points below overall. Retired when, over ≥ 8 answers since the spawn,
  accuracy ≥ 70 %, or within 5 points of overall and ≥ 10 points better than before; or after 21 days. A
  relation is not re-spawned within 7 days of a retirement.
- **Reading strategy** (`drive/strategy.py`).
  - Value per source over 7 days = good facts (non-rule evidence, not disputed) + 0.5 × documents linked to at
    least one known entity; per CPU-minute of its ingest jobs plus its document share of reading.
  - Weight = √(rate / median of the positive rates), clamped to 0.5–2 (sources with < 30 CPU-seconds or < 20
    documents keep 1).
  - Topic factor = √(facts per CPU-second of targeted reading / median), clamped to 0.5–1.5.
- **Learned phrasing** (`perception/phrasing.py`). From the relation-pattern table (DIPRE/Snowball), per
  relation, the best subject-first middle with ≥ 3 supporting sentences and confidence ≥ 0.7 that reads as a
  clause: 1–5 lowercase words, a verb (auxiliary first, a participle, or an -ed word), no pronouns, articles,
  numbers or archaic forms, not ending in an auxiliary.

## Drive (curiosity)

- **Importance.** PageRank over the entity graph (sparse power iteration; dangling mass spread uniformly), using fact links and document co-mentions.
- **Priority** of a topic = gap × importance × novelty × (1 − recent effort) × (3 if you asked about it, for 14 days):
  - *gap* = 1 − Laplace-smoothed quiz accuracy on the topic, or, before any quiz, the share of its linked entities whose article is still unread;
  - *importance* = summed PageRank of its entities, scaled to the top topic;
  - *novelty* = 1 / (1 + pursuits), with a 7-day half-life;
  - *effort* = the share of the last 24 h of targeted CPU time.

  Every factor is stored, so `polymath why <topic>` can show the arithmetic.
- **Targeted reading.** The best topics' most important unread entities are fetched straight from the Wikipedia dump through the title index.
- **The bandit** (`drive/bandit.py`):
  - Contextual Thompson sampling over action groups.
  - Reward = log(1 + value per CPU second), with Welford mean and variance per (context, arm).
  - Unseen arms get an optimistic prior.
  - Every decision is logged with its sampled scores and later its reward.

## Evaluation

- **Hold-out.** A deterministic 5 % of Wikidata entity facts, chosen by a hash of the triple id, is hidden from reasoning, PageRank, answering and calibration.
- **Quiz.**
  - "What is the ⟨p⟩ of ⟨s⟩?" with the true object and three **sibling distractors**: other objects of p, preferably of the same `instance of` class, never ones that are also true.
  - Only named subjects and answers are asked. Chance is 25 %.
- **Nightly report.** Markdown and JSON covering:
  - reading by source;
  - graph growth and evidence kinds;
  - quiz trend and per-evidence accuracy;
  - linker validation and embedding checks;
  - curiosity and effort;
  - learned source reliability;
  - problems.

## The agent society

You can create any number of agents, each with a plain-English **directive**. The directive is parsed by
patterns, not a language model, into one of five kinds and a **scope**: the entities, topics, relations and
keywords it is responsible for.

- **Scope.** It is materialised from:
  - the seed entity and its neighbours;
  - the entities linked in documents about the subject or in its topics;
  - for relation-centred agents, the relation's subjects plus every entity of the relation's dominant type.
    "predict continents" thus also covers countries that have no continent yet.
- **Refresh.** The scope is rebuilt every 6 hours as knowledge grows.

Each kind has a repertoire of actions. Every action produces **tasks whose correctness is checked without
trusting the agent**:

| action | kinds | verified by | reward (✓ / ✗) |
|---|---|---|---|
| quiz: a 4-option question on a hidden fact in scope, each fact once | research, predict, verify, answer | the hidden fact, immediately | +1.0 / −0.4 |
| predict: guess a missing fact, or abstain below `min_conf` | research, predict | the fact arriving later in the dumps (expires after 60 days) | +2.0 / −0.8 |
| dispute: judge a disputed fact in scope | verify | a hidden copy, or the evidence settling the dispute | +1.5 / −0.6 |
| read: fetch important unread articles in scope | research, watch, answer | the articles arriving | +0.1 each |
| scan: report new documents about the subject | watch | the entity linker finding an in-scope entity in them | +0.15 each |
| calibrate: re-learn its own evidence weights on its scope | research, predict, verify | held-out-style log-likelihood gain over its current weights | up to +1 |
| answer: your question | all | your 👍 / 👎 | +2.0 / −1.0 |

The penalties are set so that guessing does not pay. A random 4-way quiz answer has expected reward
0.25 × 1 − 0.75 × 0.4 < 0.

- **Learning to act.**
  - Each agent keeps its own Thompson-sampling arms over its actions, with Welford mean and variance of reward.
  - Arms are updated by every settlement, including verdicts that arrive weeks later: the reward is credited
    to the action that produced the task.
  - An action that finds nothing to do gets a small negative experience.
  - Agents doing calibration also learn *their own* link-prediction weights for their scope (specialisation).
- **Growth.**
  - XP is the sum of positive rewards. Level = 1 + ⌊log₂(1 + XP/5)⌋.
  - Each society slice gives turns by Thompson sampling on each agent's verified reward per step, with an
    optimistic prior for newcomers and a small level bonus. An agent with your questions waiting goes first.
- **Evolution** (daily):
  - **Forking.** An agent with enough verified outcomes (30 by default) and positive reward forks a child.
    The child has mutated heritable parameters:
    - confidence threshold;
    - batch size;
    - scope breadth;
    - exploration width;
    - evidence weights.
  - **Judging the contest.** Parent and child are compared on reward per step with a two-standard-error margin:
    - A worse child is retired.
    - A better child passes its parameters to a user-made parent, which keeps its name and history, or
      replaces an evolved parent.
  - **Your agents are never retired automatically.** One with persistently negative reward is reported as
    "struggling".
  - **Population cap:** `agents.max_agents`.

## Answering

- **Pattern parse.** Recognises "what is the X of Y", "when was Y born", "where is Y", "who is Y", "Y's X" and "how many".
- **Grounding:**
  - the subject is resolved by alias candidates, then the entity linker, then the longest n-gram;
  - the relation is matched against predicate labels and the property aliases learned from Wikidata ("capital city" → P36).
- **Facts first:** sourced, then inferred; disputed facts are shown as disputed; held-out facts never.
- **Passages otherwise.** A passage must contain a *specific* query word (document frequency ≤ 25 %), the relation's word and the entity's own name.
- **Citations.** Every statement carries its citations: document title, URL and license, or the dump it came from. With no grounded answer the engine says so.
