"""Link prediction: each evidence source, the product of experts, and weights learned leave-one-out."""

from __future__ import annotations

import random

import numpy as np

from polymath.memory.graph import KnowledgeGraph
from polymath.reasoning.link_prediction import (
    DEFAULT_WEIGHTS,
    METHODS,
    LinkPredictor,
    WordVectors,
    calibrate,
    fit_weights,
    named,
)
from tests.fixtures.kb import build
from tests.fixtures.typing import some


def test_named():
    assert named("Paris") and named("Q-ship") and named("P2P") and named("Quebec")
    assert not named("Q42") and not named("P31") and not named("")


def test_graph_evidence_respects_holdout_and_independent_provenance(db):
    kb = build(db)
    pred = LinkPredictor(db)
    opts = [kb.cities[0], kb.cities[1], kb.cities[2], kb.cities[3]]
    s, cap = kb.countries[2], kb.preds["capital"]
    dist = pred.graph(s, cap, opts, None)
    assert dist == {kb.cities[0]: 0.0, kb.cities[1]: 0.0, kb.cities[2]: 1.0, kb.cities[3]: 0.0}
    tid = int(db.scalar("SELECT id FROM triples WHERE s=? AND p=? AND o=?", (s, cap, kb.cities[2])))
    assert pred.graph(s, cap, opts, tid) is None  # the asked fact is hidden (leave-one-out)
    db.execute("UPDATE triples SET holdout=1 WHERE id=?", (tid,))
    assert pred.graph(s, cap, opts, None) is None
    kb.graph.add_triple(s, cap, o=kb.cities[2], kind="pattern", source="text", doc_id=kb.docs["Country02"], weight=0.5)
    again = pred.graph(s, cap, opts, None)  # re-read in text: independent of the hidden Wikidata record
    assert again is not None and again[kb.cities[2]] == 1.0


def test_related_counts_other_connections(db):
    kb = build(db)
    pred = LinkPredictor(db)
    opts = [kb.cities[4], kb.cities[5]]
    dist = pred.related(kb.countries[4], kb.preds["capital"], opts, None)
    assert dist == {kb.cities[4]: 1.0, kb.cities[5]: 0.0}  # Capitol04 —country→ Country04, not the asked fact
    db.execute("DELETE FROM triples WHERE p=?", (kb.preds["country"],))
    assert pred.related(kb.countries[4], kb.preds["capital"], opts, None) is None


def test_association_learns_from_similar_subjects(db):
    g = KnowledgeGraph(db)
    cont, cur = g.predicate("P30", "continent"), g.predicate("P38", "currency")
    europe, america = g.upsert_entity("Q46", "Europe"), g.upsert_entity("Q828", "Americas")
    euro, dollar, yen, peso = (
        g.upsert_entity(f"Q{900 + i}", n) for i, n in enumerate(["euro", "dollar", "yen", "peso"])
    )
    for i in range(20):
        x = g.upsert_entity(f"Q{2000 + i}", f"EuroLand{i}")
        g.add_triple(x, cont, o=europe, kind="wikidata", source="wikidata")
        g.add_triple(x, cur, o=euro, kind="wikidata", source="wikidata")
    for i in range(40):  # the dollar is more popular overall …
        x = g.upsert_entity(f"Q{3000 + i}", f"AmeriLand{i}")
        g.add_triple(x, cont, o=america, kind="wikidata", source="wikidata")
        g.add_triple(x, cur, o=dollar, kind="wikidata", source="wikidata")
    for i, money in enumerate((yen, peso)):
        x = g.upsert_entity(f"Q{4000 + i}", f"Other{i}")
        g.add_triple(x, cur, o=money, kind="wikidata", source="wikidata")
    new = g.upsert_entity("Q5000", "NewLand")
    g.add_triple(new, cont, o=europe, kind="wikidata", source="wikidata")
    dist = some(LinkPredictor(db).association(new, cur, [dollar, yen, euro, peso], None))
    assert max(dist, key=dist.__getitem__) == euro and dist[euro] > 0.9  # … but European subjects use the euro
    bare = g.upsert_entity("Q5001", "Featureless")
    prior = some(LinkPredictor(db).association(bare, cur, [dollar, yen, euro, peso], None))
    assert max(prior, key=prior.__getitem__) == dollar  # no features: the popularity prior decides
    lonely = g.predicate("P9999", "unused")
    assert LinkPredictor(db).association(new, lonely, [dollar, euro], None) is None


def test_text_evidence_uses_same_sentence_cooccurrence(db):
    kb = build(db)
    pred = LinkPredictor(db)
    opts = [kb.cities[6], kb.cities[7], kb.cities[8], kb.cities[9]]
    dist = some(pred.text_evidence(kb.countries[7], kb.preds["capital"], opts))
    assert max(dist, key=dist.__getitem__) == kb.cities[7] and dist[kb.cities[7]] == 1.0
    stub = kb.graph.stub("Q999999")
    assert pred.text_evidence(stub, kb.preds["capital"], opts) is None  # unnamed subject: nothing to read
    assert pred.text_evidence(kb.countries[7], kb.preds["capital"], [stub, kb.cities[1]]) is None


def test_embedding_translation(db):
    g = KnowledgeGraph(db)
    p = g.predicate("P1", "pairs with")
    rng = np.random.default_rng(3)
    dim = 16
    r = rng.normal(size=dim)
    words: dict[str, int] = {}
    vecs: list[np.ndarray] = []
    for i in range(12):
        base = rng.normal(size=dim)
        for name, v in ((f"a{i}", base), (f"b{i}", base + r)):
            words[name] = len(vecs)
            vecs.append(v / np.linalg.norm(v))
    wv = WordVectors(words, np.array(vecs))
    ids = {}
    for i in range(12):
        ids[f"a{i}"] = g.upsert_entity(f"Q{10 + i}", f"a{i}")
        ids[f"b{i}"] = g.upsert_entity(f"Q{50 + i}", f"b{i}")
    for i in range(1, 12):
        g.add_triple(ids[f"a{i}"], p, o=ids[f"b{i}"], kind="wikidata", source="wikidata")
    pred = LinkPredictor(db, vectors=wv)
    opts = [ids["b5"], ids["b0"], ids["b7"], ids["a3"]]
    dist = some(pred.embedding(ids["a0"], p, opts, None))
    assert max(dist, key=dist.__getitem__) == ids["b0"]
    assert wv.label("A0 unknownword") is not None and wv.label("nothing here") is None
    assert LinkPredictor(db).embedding(ids["a0"], p, opts, None) is None  # no vectors yet


def test_combine_and_abstention(db):
    kb = build(db)
    pred = LinkPredictor(db)
    opts = [kb.people[0], kb.people[1]]
    empty = pred.combine(opts, {})
    assert empty.method == "none" and empty.confidence == 0.5 and empty.best == opts[0]
    mixed = pred.combine(opts, {"text": {opts[0]: 0.2, opts[1]: 0.8}, "related": {opts[0]: 0.5, opts[1]: 0.5}})
    assert mixed.best == opts[1] and mixed.method == "text" and 0.5 < mixed.confidence < 1.0


def test_fit_weights_rewards_informative_evidence():
    rng = random.Random(0)
    samples = []
    for _ in range(150):
        opts = [1, 2, 3, 4]
        ans = rng.choice(opts)
        good = {o: (0.7 if o == ans else 0.1) for o in opts}
        raw = {o: rng.random() for o in opts}
        noise = {o: v / sum(raw.values()) for o, v in raw.items()}
        misleading = {o: (0.1 if o == ans else 0.3) for o in opts}
        samples.append((opts, ans, {"related": good, "text": noise, "embedding": misleading}))
    fit = fit_weights(samples)
    w = fit["weights"]
    assert w["related"] > 1.0 > w["text"] and w["embedding"] == 0.0
    assert w["graph"] == DEFAULT_WEIGHTS["graph"]  # never observed: keeps its prior
    assert fit["train_accuracy"] > 0.95 and fit["loglik"] > fit["chance_loglik"] and fit["n"] == 150
    assert fit_weights([])["n"] == 0 and set(fit_weights([])["weights"]) == set(METHODS)


def test_calibrate_on_visible_facts(db):
    build(db)
    pred = LinkPredictor(db)
    ticks = []
    fit = calibrate(db, pred, size=30, seed=1, tick=lambda: ticks.append(1))
    assert fit["n"] >= 20 and len(ticks) == fit["n"] and db.kv_get("link_weights")["n"] == fit["n"]
    assert fit["train_accuracy"] > 0.8 and pred.weights == fit["weights"]
    assert LinkPredictor(db).weights == fit["weights"]  # persisted for the next quiz
