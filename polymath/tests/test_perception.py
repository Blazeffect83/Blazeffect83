"""Phase 4: tokenizer, stemmer, sentence splitting, phrases, Aho–Corasick, entity linking, infobox, TextRank,
relation bootstrapping and the perception jobs."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time

import numpy as np
import pytest

from polymath.body.systemd_notify import Notifier
from polymath.core.app import build_components
from polymath.core.jobs import JobContext
from polymath.core.loop import Agent
from polymath.core.scheduler import Job, Scheduler
from polymath.memory import jobs as mj
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.perception import jobs as pj
from polymath.perception import phrases as ph
from polymath.perception.ahocorasick import TokenAutomaton, longest_non_overlapping
from polymath.perception.entities import (
    AliasIndex,
    EntityLinker,
    LinkerModel,
    context_vector,
    match_tokens,
    sigmoid,
    tfidf_profile,
    train_from_anchors,
    train_logistic,
)
from polymath.perception.infobox import aligned_property, extract_infobox_triples, param_key, parse_value
from polymath.perception.relations import functional_predicates, learn_and_extract, normalize_middle
from polymath.perception.sentences import PunktModel, PunktTrainer, SentenceSplitter, dunning_ll, train_model
from polymath.perception.stem import stem
from polymath.perception.textrank import keywords, pagerank, summarize
from polymath.perception.tokenize import is_wordish, normalize_token, split_period, tokenize, words


def ctx_for(config, db, kind, services, payload=None):
    s = Scheduler(db)
    key = f"{kind}:{time.time_ns()}"
    jid, _ = s.enqueue(kind, payload or {}, key=key)
    return JobContext(
        config=config,
        db=db,
        scheduler=s,
        job=Job(jid, kind, key, payload or {}, None, 0, 0, 0, 5, 0),
        deadline=time.monotonic() + 60,
        stop_event=threading.Event(),
        services=services,
    )


# ---------------------------------------------------------------- tokenizer


def test_tokenizer_kinds_offsets_and_helpers():
    text = "Dr. Smith (U.S.) paid $3,400.50 on 12.5% at https://x.org/a?b=1 — e.g. state-of-the-art can't; a@b.co.uk."
    toks = tokenize(text)
    kinds = {t.text: t.kind for t in toks}
    assert kinds["Dr."] == "word" and kinds["U.S."] == "word" and kinds["3,400.50"] == "num"
    assert kinds["12.5%"] == "num" and kinds["https://x.org/a?b=1"] == "url" and kinds["a@b.co.uk"] == "email"
    assert kinds["state-of-the-art"] == "word" and kinds["can't"] == "word"
    assert all(text[t.start : t.end] == t.text for t in toks)
    sp = split_period(tokenize("Dr. Who"))
    assert [t.text for t in sp] == ["Dr", ".", "Who"]
    assert words("The Cat sat, 3 times.") == ["the", "cat", "sat", "3", "times"]
    assert normalize_token("Can’t.") == "can't" and is_wordish("a1") and not is_wordish("--")
    assert tokenize("x")[0].is_word and tokenize("X")[0].lower == "x"


# ------------------------------------------------------------------ stemmer


def test_porter_matches_classic_examples_and_fts5():
    pairs = {
        "caresses": "caress",
        "ponies": "poni",
        "relational": "relat",
        "conditional": "condit",
        "generalizations": "gener",
        "hopping": "hop",
        "filing": "file",
        "happy": "happi",
        "goodness": "good",
        "oscillators": "oscil",
        "temperatures": "temperatur",
        "running": "run",
        "sky": "sky",
        "a": "a",
    }
    for w, s in pairs.items():
        assert stem(w) == s, w
    rng = random.Random(5)
    letters = "abcdefghijklmnoprstuvwyeeaaioou"
    vocab = sorted(
        {
            "".join(rng.choice(letters) for _ in range(rng.randint(3, 12)))
            + rng.choice(["", "s", "ing", "ed", "ation", "ness", "ly", "ies", "ational", "izer", "ful", "ement"])
            for _ in range(3000)
        }
    )
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE f USING fts5(x, tokenize='porter unicode61 remove_diacritics 2')")
    con.executemany("INSERT INTO f(rowid, x) VALUES(?,?)", list(enumerate(vocab)))
    con.execute("CREATE VIRTUAL TABLE v USING fts5vocab(f, instance)")
    fts = {vocab[doc]: term for term, doc in con.execute("SELECT term, doc FROM v")}
    assert [w for w in vocab if stem(w) != fts[w]] == []


# ---------------------------------------------------------------- sentences

CORPUS = (
    "Dr. Adams met Dr. Brown at the lab. Mr. Clark arrived later. The study, led by Prof. Doyle, ended in Jan. "
    "Results appear in Fig. 2 and Fig. 3 of the report. The team used approx. ten samples. "
    "However, the data was incomplete. Therefore the work continued. They wrote a paper about it. "
    "The lab results came later that day and it was clear that the lab was right about it all. "
)


def test_punkt_learns_abbreviations_and_splits():
    model = train_model([CORPUS * 30, "The end. It was over. Nothing else happened."])
    for ab in ("dr", "mr", "prof", "fig"):
        assert ab in model.abbreviations, ab
    assert "lab" not in model.abbreviations and "however" in model.starters
    sp = SentenceSplitter(model)
    assert sp.split("Dr. Adams met Mr. Clark. However, it rained.") == [
        "Dr. Adams met Mr. Clark.",
        "However, it rained.",
    ]
    assert sp.split('He said "Stop." Then he left. Is it over? Yes!') == [
        'He said "Stop."',
        "Then he left.",
        "Is it over?",
        "Yes!",
    ]
    assert sp.split("Values rose to 3.5 percent. See Jan. 5 notes.") == [
        "Values rose to 3.5 percent.",
        "See Jan. 5 notes.",
    ]
    assert sp.split("It was described by J. R. Tolkien. The book sold.") == [
        "It was described by J. R. Tolkien.",
        "The book sold.",
    ]
    assert sp.split("First line\nSecond line without period") == ["First line", "Second line without period"]
    assert sp.split("Wait... What happened? nothing.") == ["Wait... What happened? nothing."]
    assert sp.split("") == []
    restored = PunktModel.from_json(model.to_json())
    assert restored.abbreviations == model.abbreviations and PunktModel.from_json(None).abbreviations == set()
    assert dunning_ll(100, 1000, 99, 100_000) > dunning_ll(100, 1000, 1, 100_000)
    tr = PunktTrainer()
    tr.add_text("")
    assert tr.train().abbreviations == set()


# ------------------------------------------------------------------ phrases


def test_phrase_learning_npmi_and_merge(db):
    learner = ph.PhraseLearner(db)
    rng = random.Random(2)
    filler = ["the", "a", "of", "and", "to", "in", "is", "was", "it", "for"]
    content = ["river", "stone", "bright", "music", "garden", "winter", "silver", "forest", "engine", "planet"]
    for _ in range(400):
        sent = [rng.choice(filler) for _ in range(6)] + [rng.choice(content) for _ in range(3)]
        sent[rng.randrange(len(sent))] = "new york"
        learner.add_sentence([*" ".join(sent).split(), "bank", "of", "america"])
    assert learner.flush() > 0 and learner.flush() == 0
    assert db.kv_get("ngram_tokens") > 1000
    n = ph.recompute_phrases(db, min_count=5, threshold=0.4)
    phrases = ph.load_phrases(db)
    assert n == len(phrases) and "new york" in phrases and "bank of america" in phrases
    assert not any(p.split()[0] in filler and p.split()[-1] in filler for p in phrases)
    assert ph.merge_phrases(["i", "love", "new", "york", "bank", "of", "america"], phrases) == [
        "i",
        "love",
        "new_york",
        "bank_of_america",
    ]
    assert ph.npmi(0, 1, 1, 10) == -1.0 and ph.npmi(10, 10, 10, 10) == 1.0
    assert ph.stopwords(db, rank=5) == set()  # 26-word vocabulary: too small to call anything a stop-word


# ------------------------------------------------------------ Aho–Corasick


def test_aho_corasick_against_brute_force_and_persistence(tmp_path):
    rng = random.Random(9)
    vocab = [f"t{i}" for i in range(25)]
    patterns = {tuple(rng.choice(vocab) for _ in range(rng.randint(1, 4))) for _ in range(400)}
    auto = TokenAutomaton()
    for p in patterns:
        auto.add(p)
    auto.add([])
    auto.build()
    text = [rng.choice(vocab) for _ in range(3000)]
    brute = sorted(
        (i, i + n)
        for i in range(len(text))
        for n in range(1, 5)
        if i + n <= len(text) and tuple(text[i : i + n]) in patterns
    )
    assert sorted(auto.finditer(text)) == brute and auto.n_patterns == len(patterns)
    path = tmp_path / "a.npz"
    auto.save(path)
    loaded = TokenAutomaton.load(path)
    assert sorted(loaded.finditer(text)) == brute and loaded.n_states == auto.n_states
    with pytest.raises(RuntimeError):
        auto.add(["x"])
    fresh = TokenAutomaton()
    with pytest.raises(RuntimeError):
        list(fresh.finditer(["a"]))
    with pytest.raises(RuntimeError):
        fresh.save(path)
    assert longest_non_overlapping([(0, 2), (1, 3), (0, 3), (4, 5)]) == [(0, 3), (4, 5)]


# ------------------------------------------------------------- entity linking

TEXTS = {
    "Paris": "Paris is the capital and largest city of France, on the river Seine, known for art and museums. "
    "The French government and parliament sit in Paris.",
    "Paris (mythology)": "In Greek mythology Paris was a prince of Troy, son of Priam, who abducted Helen and caused "
    "the Trojan War described by Homer.",
    "France": "France is a country in Western Europe with Paris as its capital and a French government.",
    "Troy": "Troy was an ancient city in Anatolia, the setting of the Trojan War in Greek mythology and Homer.",
}


def build_kb(db, store):
    g = KnowledgeGraph(db)
    ids = {}
    for title, text in TEXTS.items():
        did, _ = store.add(Document("wikipedia", title, title, text * 3, "CC BY-SA 4.0"))
        ids[title] = g.upsert_entity(f"wiki:{title}", title, wiki_title=title, doc_id=did, kind="item")
        db.execute(
            "INSERT OR REPLACE INTO entity_profiles(entity_id, terms) VALUES(?,?)",
            (ids[title], json.dumps(context_vector(text))),
        )
    g.add_alias("Paris", ids["Paris"], "anchor", 30)
    g.add_alias("Paris", ids["Paris (mythology)"], "anchor", 4)
    g.add_alias("France", ids["France"], "title", 1)
    g.add_alias("Troy", ids["Troy"], "title", 1)
    g.add_alias("the", ids["France"], "anchor", 1)  # junk alias: low keyphraseness keeps it out
    for alias, linked, seen in (("paris", 40, 60), ("france", 30, 40), ("troy", 10, 12), ("the", 1, 5000)):
        db.execute("INSERT INTO surface_stats(alias, linked, seen) VALUES(?,?,?)", (alias, linked, seen))
    for d in range(1, 6):  # co-linking documents: Paris~France, mythology~Troy
        db.execute(
            "INSERT INTO documents(source, external_id, title, license, fetched, content_hash, nchars) "
            "VALUES('x', ?, 't', 'x', 0, ?, 1)",
            (f"co{d}", f"h{d}"),
        )
        doc = int(db.scalar("SELECT MAX(id) FROM documents"))
        pair = ("Paris", "France") if d <= 3 else ("Paris (mythology)", "Troy")
        for t in pair:
            db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?,?,1,1)", (doc, ids[t]))
    return ids


def test_linker_resolves_ambiguity_with_context_and_coherence(config, db):
    store = DocumentStore(db)
    ids = build_kb(db, store)
    idx = AliasIndex(db, config.paths.index_dir)
    assert idx.stale()
    info = idx.rebuild()
    assert info["patterns"] == 4 and not idx.stale() and idx.load() is not None
    linker = EntityLinker(db, idx.automaton, LinkerModel(threshold=0.3))
    city = linker.link("The Louvre in Paris is the most visited museum in France, near the Seine.")
    assert {(lk.label) for lk in city} == {"Paris", "France"}
    myth = linker.link("Homer tells how Paris, prince of Troy, took Helen and began the Trojan War.")
    by_label = {lk.label for lk in myth}
    assert "Paris (mythology)" in by_label and "Troy" in by_label
    assert all(lk.label != "France" for lk in linker.link("This is the end."))  # 'the' never linked
    assert linker.keyphraseness("paris") > 0.6 and linker.keyphraseness("unknown") == 0.0
    loo = linker.candidates("paris", exclude_anchor=(ids["Paris"], 30))
    assert [c[0] for c in loo] == [ids["Paris (mythology)"]]
    assert linker.relatedness(ids["Paris"], ids["France"]) > linker.relatedness(ids["Paris"], ids["Troy"])
    assert linker.relatedness(ids["Troy"], ids["Troy"]) == 1.0
    assert [m.norm for m in match_tokens("São-Paulo’s")] == ["são", "paulo", "s"]
    assert EntityLinker(db, None).link("Paris") == []
    prof = tfidf_profile(db, TEXTS["Troy"])
    assert prof and abs(sum(v * v for v in prof.values()) - 1) < 0.01 and tfidf_profile(db, "") == {}


def test_logistic_training_and_anchor_trainer(config, db):
    rng = np.random.default_rng(0)
    x = np.hstack([np.ones((400, 1)), rng.normal(size=(400, 2))])
    y = (x[:, 1] + 0.5 * x[:, 2] > 0).astype(float)
    w = train_logistic(x, y, epochs=300)
    assert np.mean((sigmoid(x @ w) > 0.5) == (y > 0.5)) > 0.95
    store = DocumentStore(db)
    ids = build_kb(db, store)
    idx = AliasIndex(db, config.paths.index_dir)
    idx.rebuild()
    linker = EntityLinker(db, idx.automaton)
    docs = []
    for k in range(20):
        if k % 2:
            text = "Paris, prince of Troy, loved Helen in Greek mythology."
            docs.append((k, text, [(0, 5, ids["Paris (mythology)"]), (17, 21, ids["Troy"])]))
        else:
            text = "Paris is the capital of France and its museums."
            docs.append((k, text, [(0, 5, ids["Paris"]), (24, 30, ids["France"])]))
    model = train_from_anchors(linker, docs)
    assert model.trained_on == 40 and model.validation["validation_mentions"] > 0
    assert model.validation["top1_accuracy"] >= 0.75
    assert LinkerModel.from_dict(model.to_dict()).weights == model.weights
    assert LinkerModel.from_dict({"weights": [1.0]}).weights != [1.0]
    assert train_from_anchors(linker, []) is linker.model


# ------------------------------------------------------------------ infobox


def test_infobox_values_and_alignment(db):
    assert param_key("Birth_Place2 ") == "birth_place"
    assert parse_value("[[Paris]] and [[Lyon|the city]]") == [("entity", "Paris"), ("entity", "Lyon")]
    assert parse_value("3 May 1950") == [("value", {"time": "1950-05-03"})]
    assert parse_value("May 3, 1950") == [("value", {"time": "1950-05-03"})]
    assert parse_value("44 BC") == [("value", {"time": "-44"})]
    assert parse_value("2,148,271 km") == [("value", {"amount": 2148271.0, "unit": "km"})]
    assert parse_value("Republic") == [("value", {"text": "Republic"})]
    assert parse_value("") == [] and parse_value("x" * 200) == []
    g = KnowledgeGraph(db)
    fr, pa = g.upsert_entity("Q142", "France", wiki_title="France"), g.upsert_entity("Q90", "Paris", wiki_title="Paris")
    for i in range(3):
        other = g.upsert_entity(f"Q{i}", f"C{i}", wiki_title=f"C{i}")
        cap = g.upsert_entity(f"Q{i + 100}", f"K{i}", wiki_title=f"K{i}")
        g.add_triple(other, g.predicate("P36", "capital"), o=cap, kind="wikidata", source="wikidata")
        extract_infobox_triples(
            db,
            g,
            other,
            1,
            [{"name": "Infobox country", "params": {"capital": f"[[K{i}]]"}}],
            lambda t: g.by_wiki_title(t).id if g.by_wiki_title(t) else None,
        )
    n = extract_infobox_triples(
        db,
        g,
        fr,
        1,
        [
            {
                "name": "Infobox country",
                "params": {
                    "capital": "[[Paris]]",
                    "image_flag": "x.svg",
                    "population_total": "68,000,000",
                    "": "x",
                    "motto": " ",
                },
            }
        ],
        lambda t: pa if t == "Paris" else None,
    )
    assert n == 2
    assert aligned_property(db, "capital") == "P36" and aligned_property(db, "motto") is None


# ----------------------------------------------------------------- TextRank


def test_pagerank_keywords_and_summary():
    adj = np.array([[0, 1, 1], [1, 0, 0], [1, 0, 0]], dtype=float)
    r = pagerank(adj)
    assert abs(r.sum() - 1) < 1e-6 and r[0] > r[1] and pagerank(np.zeros((0, 0))).size == 0
    assert abs(pagerank(np.zeros((3, 3))).sum() - 1) < 1e-6
    text = (
        "Compatibility of systems of linear constraints over the set of natural numbers. Criteria of "
        "compatibility of a system of linear Diophantine equations, strict inequations, and nonstrict "
        "inequations are considered. Upper bounds for components of a minimal set of solutions and algorithms "
        "of construction of minimal generating sets of solutions for all types of systems are given."
    )
    kws = keywords(text, {"of", "the", "and", "for", "are", "a", "all", "over"}, top=5)
    assert any("linear" in k for k in kws) and any("systems" in k or "system" in k for k in kws)
    sents = ["Cats are small mammals.", "Cats hunt mice and birds.", "The weather was nice.", "Small cats hunt birds."]
    assert summarize(sents, set(), k=2) != [2, 3] and len(summarize(sents, set(), k=2)) == 2
    assert summarize(sents[:2], set(), k=3) == [0, 1] and keywords("", set()) == []


# ---------------------------------------------------------------- relations


def test_middle_normalisation():
    assert normalize_middle(" is the capital (since 1944) of ") == "is <det> capital of"
    assert normalize_middle(" was born in 1950 in ") == "was born in <num> in"
    assert normalize_middle("") == "" and normalize_middle(" a b c d e f g h i j ") == ""


def test_bootstrapped_relation_learning(db):
    g = KnowledgeGraph(db)
    cap = g.predicate("P36", "capital")
    countries = [g.upsert_entity(f"Q{i}", f"Country{i}") for i in range(30)]
    cities = [g.upsert_entity(f"Q{100 + i}", f"City{i}") for i in range(30)]
    other = g.upsert_entity("Q999", "Elsewhere")
    for i in range(20):  # seeds: known capitals
        g.add_triple(countries[i], cap, o=cities[i], kind="wikidata", source="wikidata")
    g.add_triple(countries[25], cap, o=other, kind="wikidata", source="wikidata")
    now = time.time()
    rows = []
    for i in range(30):  # "<city> is the capital of <country>" : object first → order 1
        rows.append((cities[i], countries[i], 1, "x", normalize_middle(" is the capital of "), "y", "s", now))
    for i in range(10):  # noise pattern
        rows.append((cities[i], countries[(i + 3) % 30], 1, "x", normalize_middle(" is far from "), "y", "s", now))
    db.executemany(
        "INSERT INTO pair_contexts(e1, e2, doc_id, left_ctx, middle, right_ctx, sentence, created) "
        "VALUES(?,?,?,?,?,?,?,?)",
        rows,
    )
    assert cap in functional_predicates(db, min_subjects=5)
    res = learn_and_extract(db)
    assert res["confident_patterns"] >= 1
    new = {
        (int(r["s"]), int(r["o"]))
        for r in db.query("SELECT t.s, t.o FROM triples t JOIN provenance v ON v.triple_id=t.id WHERE v.kind='pattern'")
    }
    assert (countries[22], cities[22]) in new and (countries[25], cities[25]) in new
    assert not any(o == countries[(s % 30)] for s, o in new)
    pat = db.one("SELECT * FROM patterns WHERE middle='is <det> capital of'")
    assert pat["positive"] == 20 and pat["negative"] == 1 and pat["confidence"] > 0.7
    assert learn_and_extract(db)["extracted"] == 0  # idempotent: nothing new on a second pass
    db.execute("DELETE FROM pair_contexts")
    assert learn_and_extract(db) == {"contexts": 0, "patterns": 0, "extracted": 0}


# --------------------------------------------------------------------- jobs

WIKI_TEXT = (
    "Mars is the fourth planet from the Sun. It is named after the Roman god of war. Mars has two moons "
    "called Phobos and Deimos. The Sun is a star at the center of the Solar System."
)


def test_perception_pipeline_jobs(config, db):
    store = DocumentStore(db)
    s1 = WIKI_TEXT.index("Sun")
    links = [
        (s1, s1 + 3, "Sun"),
        (WIKI_TEXT.index("Phobos"), WIKI_TEXT.index("Phobos") + 6, "Phobos (moon)"),
        (WIKI_TEXT.index("Deimos"), WIKI_TEXT.index("Deimos") + 6, "Deimos (moon)"),
    ]
    store.add(
        Document(
            "wikipedia",
            "en:1",
            "Mars",
            WIKI_TEXT,
            "CC BY-SA 4.0",
            links=links,
            extra={"infoboxes": [{"name": "Infobox planet", "params": {"satellites": "[[Phobos (moon)|Phobos]]"}}]},
        )
    )
    store.add(
        Document(
            "wikipedia", "en:2", "Sun", "The Sun is the star at the centre of the Solar System. " * 4, "CC BY-SA 4.0"
        )
    )
    store.add(
        Document("web", "w1", "News", "Astronomers watched Phobos and Deimos orbit Mars near the Sun. " * 3, "unknown")
    )
    db.execute("INSERT INTO wiki_redirects(lang, title, target) VALUES('en', 'Phobos (moon)', 'Phobos')")
    services = {"docs": store}
    mj.index_documents(ctx_for(config, db, "memory.index", services))
    out = pj.anchor_job(ctx_for(config, db, "perception.anchors", services))
    assert out.done and out.result["docs"] == 2 and out.result["infobox_facts"] == 1
    g = KnowledgeGraph(db)
    assert g.by_wiki_title("Phobos") is not None and g.candidates("deimos")
    assert db.scalar("SELECT linked FROM surface_stats WHERE alias='phobos'") == 1
    assert db.scalar("SELECT COUNT(*) FROM entity_profiles") == 2
    assert pj.train_sentences_job(ctx_for(config, db, "x", services)).done
    assert db.kv_get("punkt_model")["trained_tokens"] > 0
    res = pj.automaton_job(ctx_for(config, db, "x", services))
    assert res.result["rebuilt"] and not pj.automaton_job(ctx_for(config, db, "x", services)).result["rebuilt"]
    read = pj.read_job(ctx_for(config, db, "perception.read", services))
    assert read.done and read.result["docs"] == 3 and read.result["contexts"] >= 1
    web_doc = db.one("SELECT id, meta FROM documents WHERE source='web'")
    meta = json.loads(web_doc["meta"])
    assert meta["keywords"] and meta["sentences"] == 3 and meta["summary"]
    assert db.scalar("SELECT COUNT(*) FROM doc_entities WHERE doc_id=?", (web_doc["id"],)) >= 2
    assert pj.train_phrases_job(ctx_for(config, db, "x", services)).done
    model = pj.train_linker_job(ctx_for(config, db, "x", services, {"docs": 10}))
    assert model.done and db.kv_get("linker_model")
    assert pj.relations_job(ctx_for(config, db, "x", services)).done


def test_perception_planner_bootstrap_order_and_reread(config, db):
    comps = build_components(config, db)
    assert {"perception.read", "perception.anchors", "perception.automaton"} <= set(comps.registry.kinds())
    agent = Agent(config, db, comps.registry, services=comps.services, notifier=Notifier({}))
    store = DocumentStore(db)
    for i in range(3):
        store.add(Document("wikipedia", f"en:{i}", f"T{i}", WIKI_TEXT + str(i), "CC", links=[(0, 4, "Mars")]))
    mj.index_documents(ctx_for(config, db, "memory.index", {"docs": store}))
    pj.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs")}
    assert "perception.anchors" in kinds and "perception.read" not in kinds  # anchors first
    db.kv_set("alias_automaton", {"patterns": 100, "aliases_total": 1, "built": time.time()})
    assert pj.maybe_reread(db) == 0 and db.kv_get("read_epoch_patterns") == 100
    db.execute("UPDATE documents SET stage=2")
    db.kv_set("alias_automaton", {"patterns": 150, "aliases_total": 1, "built": time.time()})
    assert pj.maybe_reread(db) == 0
    db.kv_set("alias_automaton", {"patterns": 250, "aliases_total": 1, "built": time.time()})
    assert pj.maybe_reread(db) == 3 and db.scalar("SELECT MAX(stage) FROM documents") == 1
