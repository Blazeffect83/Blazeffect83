""" "Did you know?": facts that surprised its model (evaluation.surprise)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from polymath.evaluation import surprise as S
from polymath.interface.face import Face
from polymath.interface.feed import render_event
from polymath.memory.graph import KnowledgeGraph
from polymath.reasoning.link_prediction import LinkPredictor, Prediction


class _Ctx:
    def __init__(self, db):
        self.db = db

    def tick(self):
        pass


def Ctx(db) -> Any:
    return _Ctx(db)


def text_of(segs):
    return "".join(t for t, _ in segs)


@pytest.fixture()
def world(db):
    g = KnowledgeGraph(db)
    p31 = g.predicate("P31", "instance of")
    pop = g.predicate("P1082", "population", datatype="quantity")
    ids = g.predicate("P213", "ISNI identifier")
    country = g.upsert_entity("Q6256", "country")
    lands = []
    for i in range(50):
        c = g.upsert_entity(f"Q{900 + i}", f"Land{i}")
        g.add_triple(c, p31, o=country, kind="wikidata", source="wikidata", confidence=0.9)
        g.add_triple(c, pop, value={"amount": 1_000_000 + 10_000 * i, "unit": None}, kind="wikidata",
                     source="wikidata", confidence=0.9)  # fmt: skip
        lands.append(c)
    giant = g.upsert_entity("Q999", "Giantland")
    g.add_triple(giant, p31, o=country, kind="wikidata", source="wikidata", confidence=0.9)
    big = g.add_triple(giant, pop, value={"amount": 900_000_000, "unit": None}, kind="wikidata", source="wikidata",
                       confidence=0.9)[0]  # fmt: skip
    g.add_triple(giant, ids, value={"amount": 1, "unit": None}, kind="wikidata", source="wikidata")
    return {"g": g, "giant": giant, "big": big, "lands": lands, "country": country, "pop": pop}


def test_extremes_are_found_and_shared_once(db, world):
    out = S.surprise_job(Ctx(db), predictor=LinkPredictor(db))
    assert out.result["shared"] >= 1
    texts = [r["text"] for r in db.query("SELECT text FROM events WHERE kind = 'didyouknow'")]
    assert "Did you know? Giantland has the largest population of the 51 countries it knows: 900,000,000." in texts
    assert not any("Land1'" in t or "Land2'" in t for t in texts)  # ordinary values are not surprising
    again = S.surprise_job(Ctx(db), predictor=LinkPredictor(db))
    assert again.result["examined"] == 0  # the cursor moved on
    assert any(x.startswith("Did you know? Giantland") for x in S.recent(db, time.time() - 60))
    assert (
        S.plural("city") == "cities"
        and S.plural("bus") == "buses"
        and S.plural("sovereign state") == "sovereign states"
    )


def test_near_extreme_wording_and_too_few_peers(db, world):
    g = world["g"]
    for i in range(60):  # enough peers for "less than 98%" to mean something
        extra = g.upsert_entity(f"Q{3000 + i}", f"Midland{i}")
        g.add_triple(extra, g.predicate("P31", "instance of"), o=world["country"], kind="wikidata", source="wikidata")
        g.add_triple(extra, world["pop"], value={"amount": 2_000_000 + i, "unit": None}, kind="wikidata", source="w")
    small = g.upsert_entity("Q998", "Tinyland")
    g.add_triple(small, g.predicate("P31", "instance of"), o=world["country"], kind="wikidata", source="wikidata")
    tid = g.add_triple(small, world["pop"], value={"amount": 1_005_000, "unit": None}, kind="wikidata",
                       source="wikidata")[0]  # fmt: skip
    t = db.one("SELECT t.*, p.label AS pl FROM triples t JOIN predicates p ON p.id = t.p WHERE t.id = ?", (tid,))
    why = S.extreme(db, t)
    assert why is not None and why["rank"] == "smallest" and not why["most"]
    assert "less than 98% of the" in S.sentence(db, t, why)
    lonely = g.upsert_entity("Q997", "Lonely")
    tid2 = g.add_triple(lonely, world["pop"], value={"amount": 5, "unit": None}, kind="wikidata", source="w")[0]
    t2 = db.one("SELECT t.*, p.label AS pl FROM triples t JOIN predicates p ON p.id = t.p WHERE t.id = ?", (tid2,))
    assert S.extreme(db, t2) is None  # no class: no peers to compare with
    t3 = dict(t2) | {"value": '{"text": "x"}'}
    assert S.extreme(db, t3) is None and S._amount("not json") is None


def test_unexpected_facts(db, world):
    g = world["g"]
    cap = g.predicate("P36", "capital")
    sydney, canberra, perth = (g.upsert_entity(f"Q{50 + i}", n) for i, n in enumerate(["Sydney", "Canberra", "Perth"]))
    australia = g.upsert_entity("Q408", "Australia")
    for land in world["lands"][:10]:
        g.add_triple(land, cap, o=sydney, kind="wikidata", source="wikidata")
    g.add_triple(world["lands"][10], cap, o=perth, kind="wikidata", source="wikidata")
    tid = g.add_triple(australia, cap, o=canberra, kind="wikidata", source="wikidata")[0]
    g.add_triple(australia, g.predicate("P150", "contains"), o=sydney, kind="wikidata", source="wikidata")
    t = db.one("SELECT t.*, p.label AS pl FROM triples t JOIN predicates p ON p.id = t.p WHERE t.id = ?", (tid,))

    class Sure:
        def predict(self, s, p, options, exclude=None):
            return Prediction(sydney, 0.9, "related", {o: (0.9 if o == sydney else 0.02) for o in options})

    why = S.unexpected(db, t, Sure())
    assert why is not None and why["expected"] == sydney
    assert (
        S.sentence(db, t, why) == "Australia's capital is Canberra, although everything else it knew pointed to Sydney"
    )

    class Right(Sure):
        def predict(self, s, p, options, exclude=None):
            return Prediction(canberra, 0.9, "related", {o: (0.9 if o == canberra else 0.02) for o in options})

    assert S.unexpected(db, t, Right()) is None  # it expected the truth: no surprise
    assert S.unexpected(db, dict(t) | {"o": 0}, Sure()) is None


def test_job_uses_the_default_predictor_and_skips_dull_relations(db, world, monkeypatch):
    from polymath.evaluation import jobs as vjobs

    g = world["g"]
    cap = g.predicate("P36", "capital")
    city = g.upsert_entity("Q60", "Metro")
    for land in world["lands"][:3]:
        g.add_triple(land, cap, o=city, kind="wikidata", source="wikidata")
    called = []

    def loader(ctx: Any) -> LinkPredictor:
        called.append(1)
        return LinkPredictor(ctx.db)

    monkeypatch.setattr(vjobs, "predictor_for", loader)
    db.kv_set("surprise_cursor", world["big"])  # only the newer facts
    S.surprise_job(Ctx(db))
    assert called  # loaded once, lazily, for entity-valued facts
    assert not db.scalar("SELECT COUNT(*) FROM events WHERE text LIKE '%ISNI%'")


def test_planner_feed_and_face(db, world):
    from types import SimpleNamespace

    from polymath.core.scheduler import Scheduler

    a = SimpleNamespace(db=db, scheduler=Scheduler(db))
    S.planner(a)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'eval.surprise'") == 1
    ev = {"kind": "note", "at": time.time(), "what": "didyouknow", "text": "Did you know? X."}
    assert "fun fact" in text_of(render_event(ev))
    f = Face()
    f.update({"online": True, "state": "running", "mode": "normal", "action": "wikipedia.part"})
    f.see(ev, 3.0)
    assert f.mood(3.0) == "aha"


def test_units_series_and_many_valued_relations_are_not_surprises(db, world):
    g = world["g"]
    area = g.predicate("P2046", "area", datatype="quantity")
    for i, land in enumerate(world["lands"]):
        g.add_triple(land, area, value={"amount": 100 + i, "unit": "Q712226"}, kind="wikidata", source="wikidata")
    odd = g.add_triple(
        world["giant"], area, value={"amount": 5_000_000, "unit": "Q25343"}, kind="wikidata", source="wikidata"
    )[0]  # fmt: skip — square metres among square kilometres
    t = db.one("SELECT t.*, p.label AS pl FROM triples t JOIN predicates p ON p.id = t.p WHERE t.id = ?", (odd,))
    assert S.extreme(db, t) is None  # no peers in the same unit
    g.add_triple(
        world["giant"], world["pop"], value={"amount": 800_000_000, "unit": None}, kind="wikidata", source="wikidata"
    )  # fmt: skip — a second value: a series
    big = db.one("SELECT t.*, p.label AS pl FROM triples t JOIN predicates p ON p.id = t.p WHERE t.id = ?",
                 (world["big"],))  # fmt: skip
    assert S.extreme(db, big) is None
    borders = g.predicate("P47", "shares border with")
    a, b = g.upsert_entity("Q71", "Aland"), g.upsert_entity("Q72", "Bland")
    tid = g.add_triple(world["lands"][0], borders, o=a, kind="wikidata", source="wikidata")[0]
    g.add_triple(world["lands"][0], borders, o=b, kind="wikidata", source="wikidata")
    t = db.one("SELECT t.*, p.label AS pl FROM triples t JOIN predicates p ON p.id = t.p WHERE t.id = ?", (tid,))

    class Sure:
        def predict(self, s, p, options, exclude=None):
            return Prediction(options[0], 0.95, "related", {o: (0.95 if o == options[0] else 0.01) for o in options})

    assert S.unexpected(db, t, Sure(), single=set()) is None  # not a single-valued relation


def test_digest_shares_the_days_surprises(db, world):
    from polymath.evaluation import digest

    S.surprise_job(Ctx(db), predictor=LinkPredictor(db))
    lines = digest.lines(digest.collect(db, time.time() - 86400))
    assert any(line.startswith("Did you know? Giantland") for line in lines)
