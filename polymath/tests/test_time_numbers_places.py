"""v0.5: time awareness, number sense and geography."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from polymath.interface.answer import Answer, Answerer
from polymath.interface.tell import Teller
from polymath.memory.jobs import _claims_to_triples
from polymath.qa import places
from polymath.reasoning import contradictions, inference, temporal, units
from polymath.senses.wikidata import parse_entity
from tests.world import World, geo_world, q, t


def text(answer: Answer) -> str:
    return " ".join(s.text for s in answer.statements) + " " + answer.note


# ------------------------------------------------------------------ time
def _snak(pid: str, value: dict, dtype: str = "time") -> dict:
    return {"snaktype": "value", "property": pid, "datatype": dtype, "datavalue": {"type": dtype, "value": value}}


def _time(y: str) -> dict:
    return {"time": f"+{y}-00-00T00:00:00Z", "precision": 9}


def test_wikidata_keeps_when_a_statement_held_and_earlier_values():
    """Berlin is the preferred (current) capital; Bonn, which ended in 1990, is kept with its end."""
    item: dict[str, Any] = {"type": "item", "id": "Q183", "labels": {"en": {"value": "Germany"}},
            "sitelinks": {"enwiki": {"title": "Germany"}, "eswiki": {"title": "Alemania"}},
            "claims": {"P36": [
                {"rank": "preferred", "mainsnak": _snak("P36", {"id": "Q64"}, "wikibase-entityid"),
                 "qualifiers": {"P580": [_snak("P580", _time("1990"))]}},
                {"rank": "normal", "mainsnak": _snak("P36", {"id": "Q350"}, "wikibase-entityid"),
                 "qualifiers": {"P580": [_snak("P580", _time("1949"))], "P582": [_snak("P582", _time("1990"))]}},
                {"rank": "normal", "mainsnak": _snak("P36", {"id": "Q1"}, "wikibase-entityid")},  # no end: dropped
            ]}}  # fmt: skip
    for st in item["claims"]["P36"]:
        st["mainsnak"]["datavalue"]["value"] = {"entity-type": "item", "id": st["mainsnak"]["datavalue"]["value"]["id"]}
        st["mainsnak"]["datatype"] = "wikibase-item"
    ent = parse_entity(json.dumps(item).encode())
    assert ent is not None
    assert ent["claims"]["P36"] == ["Q64", "Q350"]
    assert ent["claims"]["_when"]["P36"] == [[0, "1990", None, None], [1, "1949", "1990", None]]
    assert ent["claims"]["_sitelinks"] == {"es": "Alemania"}


def test_validity_is_stored_inherited_and_shown(db):
    w = geo_world(db)
    claims = {"P36": ["Q64", "Q350"], "_when": {"P36": [[1, "1949", "1990", None]]}}
    _claims_to_triples(w.g, w.e["Germany"], claims)
    bonn = db.scalar("SELECT t.id FROM triples t JOIN entities e ON e.id = t.o WHERE e.label = 'Bonn'")
    v = temporal.of(db, [bonn])[bonn]
    assert v == temporal.Validity("1949", "1990", None) and v.ended() and v.suffix() == " (1949–1990)"
    # the answer shows today's capital first, and Bonn as past
    ans = Answerer(db).ask("What is the capital of Germany?")
    lines = [s.text for s in ans.statements]
    assert lines[0] == "The capital of Germany is Berlin." and "The capital of Germany was Bonn (1949–1990)." in lines
    # tell leaves the past value out when the current one is known
    story = Teller(db).tell("Germany")
    assert "Bonn" not in story.paragraph
    # a single-valued relation: the past value does not compete with today's
    stats = contradictions.detect(db, {w.pred("P36", "capital")})
    assert stats["disputed"] == 0
    # a conclusion drawn from the past fact ends with it
    cap_of = w.pred("P1376", "capital of")
    prem, _ = w.g.add_triple(w.e["Bonn"], cap_of, o=w.e["Germany"], kind="wikidata", source="wikidata", confidence=0.9)
    temporal.record(db, prem, "1949", "1990", None)
    db.execute("INSERT INTO rules(kind, p, q, confidence, support, updated) VALUES('inverse', ?, ?, 0.9, 50, 0)",
               (cap_of, w.pred("P36", "capital")))  # fmt: skip
    db.execute("DELETE FROM triples WHERE id = ?", (bonn,))
    db.kv_set("inference_cursor", 0)
    inference.forward_chain(db)
    derived = db.scalar(
        "SELECT id FROM triples WHERE s = ? AND o = ? AND status = 'inferred'", (w.e["Germany"], w.e["Bonn"])
    )
    assert temporal.of(db, [derived])[derived].valid_to == "1990"


def test_dates_compare_and_read_naturally():
    assert temporal.date_key("-44") < temporal.date_key("1") < temporal.date_key("1990-10-03")  # type: ignore[operator]
    assert temporal.date_key("not a date") is None and temporal.date_key(None) is None
    assert temporal.human_date("1990-10-03") == "3 October 1990" and temporal.human_date("1990-10") == "October 1990"
    assert temporal.human_date("-44") == "44 BC" and temporal.human_date("x") == "x"
    assert temporal.Validity(None, None, "2004").suffix() == " (in 2004)"
    assert (
        temporal.Validity("2005").suffix() == " (since 2005)"
        and temporal.Validity(valid_to="2999").suffix() == " (until 2999)"
    )
    assert temporal.Validity().suffix() == "" and not temporal.Validity(valid_to="2999").ended()


def test_time_questions(db):
    geo_world(db)
    a = Answerer(db)
    assert "Mozart died at about 35" in text(a.ask("How old was Mozart when he died?"))
    assert "years old (inception 843)" in text(a.ask("How old is France?"))
    first = text(a.ask("Which came first, France or Germany?"))
    assert first.startswith("France came first") and "about 1,028 years before Germany" in first
    alive = text(a.ask("Who was alive at the same time as Mozart?"))
    assert "Joseph Haydn (1732–1809)" in alive and "Ludwig van Beethoven" in alive and "Einstein" not in alive
    assert "Moon landing" in text(a.ask("What happened in 1969?"))
    assert "Albert Einstein" in text(a.ask("Who was born in 1879?"))
    assert "Isaac Newton" in text(a.ask("Who died in 1727?"))
    assert "could not identify" in text(a.ask("How old is Narnia?"))
    assert "nothing dated 1066" in text(a.ask("What happened in 1066?"))


# ------------------------------------------------------------------ numbers
def test_units_put_quantities_on_one_scale():
    assert units.to_base(28251, "Q3710") == pytest.approx((8610.9, "length"), rel=1e-4)
    assert units.to_base(1, "km²") == (1e6, "area") and units.to_base(212, "Q42289") == pytest.approx(
        (100.0, "temperature")
    )
    assert units.to_base(5, None) == (5.0, "count") and units.to_base(3, "Q4917") is None  # a currency
    assert units.show(8849, "length") == "8.85 km" or units.show(8849, "length") == "8,849 m"
    assert units.show(643801e6, "area") == "643,801 km²" and units.show(0.5, "mass") == "500 g"
    assert units.show(3, "count") == "3" and units.show(0, "length") == "0 m"


def test_comparisons_ratios_and_density(db):
    geo_world(db)
    a = Answerer(db)
    yes = a.ask("Is Mount Everest taller than K2?")
    assert text(yes).startswith("Yes. Mount Everest") and "compared by elevation above sea level" in text(yes)
    assert yes.statements[0].citations  # both facts cited
    assert text(a.ask("Is K2 higher than Everest?")).startswith("No. Mount Everest")
    assert text(a.ask("Which is bigger, France or Spain?")).startswith("France (643,801 km²) is bigger than Spain")
    assert "about 1.3 times the area of Spain" in text(a.ask("How many times bigger is France than Spain?"))
    assert "France is larger by 137,811 km²" in text(a.ask("How much larger is France than Spain?"))
    assert text(a.ask("Is France more populous than Spain?")).startswith("Yes.")
    older = text(a.ask("Who is older, Newton or Leibniz?"))
    assert older.startswith("Isaac Newton is older") and "about 3 years apart" in older
    assert "About 106 people per km²" in text(a.ask("What is the population density of France?"))
    assert "do not know the height" in text(a.ask("Is Paris taller than Berlin?"))
    assert "could not identify" in text(a.ask("Is Atlantis bigger than Spain?"))
    assert "need both the population and the area of Paris" in text(a.ask("What is the population density of Paris?"))


# ------------------------------------------------------------------ places
def test_distances_directions_and_what_is_near(db):
    geo_world(db)
    a = Answerer(db)
    d = text(a.ask("How far is Paris from Berlin?"))
    assert "about 877 km (545 miles) apart" in d
    assert "about 877 km" in text(a.ask("What is the distance between Berlin and Paris?"))
    assert places.haversine(0, 0, 0, 180) == pytest.approx(math.pi * places.EARTH_KM)
    north = text(a.ask("Which is further north, Paris or Seattle?"))
    assert north.startswith("Paris is further north than Seattle") and "48.86° N" in north
    assert text(a.ask("Is Tokyo east of Berlin?")).startswith("Yes. Tokyo is further east")
    assert text(a.ask("Is Seattle east of Tokyo?")).startswith("Yes. Seattle is further east")  # across the date line
    assert places.index_coordinates(db)["indexed"] == 7
    near = text(a.ask("What is near the Eiffel Tower?"))
    assert "Paris (4 km)" in near and "Versailles (14 km)" in near and "Berlin" not in near
    assert "know of nothing else within 50 km of Tokyo" in text(a.ask("What is near Tokyo?"))
    assert "do not know where France is" in text(a.ask("How far is France from Spain?"))


def test_geo_job_and_planner(db):
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    places.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'memory.geo'") == 0
    geo_world(db)
    places.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'memory.geo'") == 1
    ctx = type("C", (), {"db": db, "tick": staticmethod(lambda: None), "should_stop": staticmethod(lambda: False)})()
    out = places.geo_job(ctx)
    assert out.done and out.result["indexed"] == 7 and db.scalar("SELECT COUNT(*) FROM geo") == 7
    assert places.geo_job(ctx).result["indexed"] == 0  # nothing new


def test_world_helper_values():
    assert q(5, "Q11573") == {"amount": 5, "unit": "Q11573"} and t("1990") == {"time": "1990"}
    assert World.__name__ == "World"


def test_ages_of_the_living_the_long_dead_and_things_that_ended(db):
    w = geo_world(db)
    w.ent("Q9", "Young Person")
    w.fact("Young Person", "P569", "date of birth", value=t("2000-06-01"))
    w.ent("Q8", "Ancient Sage")
    w.fact("Ancient Sage", "P569", "date of birth", value=t("1500"))
    w.ent("Q7", "Old Empire")
    w.fact("Old Empire", "P571", "inception", value=t("1200"))
    w.fact("Old Empire", "P576", "dissolved, abolished or demolished date", value=t("1500"))
    w.ent("Q6", "Mystery")
    a = Answerer(db)
    assert "Young Person is about 2" in text(a.ask("How old is Young Person?"))
    assert "I have no date of death for Young Person" in text(a.ask("How old was Young Person when she died?"))
    assert "do not know when they died" in text(a.ask("How old is Ancient Sage?"))
    assert "Old Empire lasted about 300 years" in text(a.ask("How old is Old Empire?"))
    assert "do not know when Mystery began" in text(a.ask("How old is Mystery?"))
    assert "do not know when Mystery began" in text(a.ask("Which came first, Mystery or France?"))
    assert "could not identify" in text(a.ask("Which came first, Atlantis or France?"))
    assert "do not know when Mystery lived" in text(a.ask("Who lived at the same time as Mystery?"))
    assert "could not identify" in text(a.ask("Who were the contemporaries of Nobody Atall?"))
    assert "Born in 44 BC" not in text(a.ask("Who was born in 44 BC?"))
    same = text(a.ask("Which came first, Mozart or Mozart?"))
    assert same
