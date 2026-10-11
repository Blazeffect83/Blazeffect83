""" "Tell me about X": paragraphs written from facts (interface.tell), the answerer and the CLI using them."""

from __future__ import annotations

import json

import pytest

from polymath.interface import cli
from polymath.interface.answer import Answerer, render_value
from polymath.interface.tell import Fact, Story, Teller, article, clean_description, format_date, join, round_number
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from tests.fixtures.kb import build


def W(g: KnowledgeGraph, s: int, key: str, label: str, **kw):
    return g.add_triple(s, g.predicate(key, label), kind="wikidata", source="wikidata", confidence=0.9, **kw)[0]


@pytest.fixture()
def world(db):
    kb = build(db, n=4)
    g = kb.graph
    human = g.upsert_entity("Q5", "Q5")  # the class "human" itself not read yet: still recognised
    poland = g.upsert_entity("Q36", "Poland")
    warsaw = g.upsert_entity("Q270", "Warsaw")
    passy = g.upsert_entity("Q209", "Passy")
    physicist = g.upsert_entity("Q169470", "physicist")
    chemist = g.upsert_entity("Q593644", "chemist")
    nobel_p = g.upsert_entity("Q38104", "Nobel Prize in Physics")
    nobel_c = g.upsert_entity("Q44585", "Nobel Prize in Chemistry")
    pierre = g.upsert_entity("Q37463", "Pierre Curie")
    store = DocumentStore(db)
    did, _ = store.add(Document("wikipedia", "en:Marie Curie", "Marie Curie",
                                "Marie Curie was a Polish and naturalised-French physicist and chemist. She was the first "
                                "woman to win a Nobel Prize. Later life was spent in Paris.", "CC BY-SA 4.0",
                                url="https://en.wikipedia.org/wiki/Marie_Curie"))  # fmt: skip
    curie = g.upsert_entity("Q7186", "Marie Curie", description="Polish-French physicist and chemist (1867–1934)",
                            doc_id=did)  # fmt: skip
    g.add_alias("Marie Curie", curie, "title", 5)
    W(g, curie, "P31", "instance of", o=human)
    W(g, curie, "P569", "date of birth", value={"time": "1867-11-07"})
    W(g, curie, "P570", "date of death", value={"time": "1934-07-04"})
    W(g, curie, "P19", "place of birth", o=warsaw)
    W(g, curie, "P20", "place of death", o=passy)
    for job in (physicist, chemist):
        W(g, curie, "P106", "occupation", o=job)
    for prize in (nobel_p, nobel_c):
        W(g, curie, "P166", "award received", o=prize)
    W(g, curie, "P26", "spouse", o=pierre)
    W(g, curie, "P27", "country of citizenship", o=poland)
    W(g, curie, "P213", "ISNI", value={"text": "0000 0001 2133 4016"})  # an identifier: never mentioned
    W(g, warsaw, "P17", "country", o=poland)
    W(g, warsaw, "P31", "instance of", o=kb.preds["capital"] and g.upsert_entity("Q515", "city"))
    W(g, poland, "P36", "capital", o=warsaw)
    W(g, poland, "P1082", "population", value={"amount": 37_840_001, "unit": None})
    W(g, poland, "P2046", "area", value={"amount": 312_696.4, "unit": "Q712226"})
    W(g, poland, "P1451", "motto text", value={"text": "Bóg, Honor, Ojczyzna"})
    reason = g.predicate("P37", "official language")
    polish = g.upsert_entity("Q809", "Polish")
    g.add_triple(poland, reason, o=polish, status="inferred", kind="rule", source="rule", detail="language rule",
                 confidence=0.7)  # fmt: skip
    return {"curie": curie, "poland": poland, "warsaw": warsaw, "kb": kb}


def test_a_person_reads_like_an_encyclopedia_opening(db, world):
    st = Teller(db).tell("Marie Curie")
    p = st.paragraph
    assert p.startswith("Marie Curie was a Polish-French physicist and chemist.")  # dates stripped: facts say them
    assert "Curie was born in Warsaw on 7 November 1867 and died in Passy on 4 July 1934." in p
    assert "Curie worked as a physicist and chemist." in p
    assert "Awards include Nobel Prize in Physics and Nobel Prize in Chemistry." in p
    assert "Curie was married to Pierre Curie." in p
    assert " she " not in f" {p.lower()} " and " her " not in f" {p.lower()} "  # never a guessed pronoun
    assert "0000 0001" not in p and "[1]" in p
    assert st.citations[0].title == "Wikidata" and st.facts_used >= 8 and st.confidence > 0.8
    assert st.article.startswith("Marie Curie was a Polish and naturalised-French physicist and chemist.")
    out = st.render()
    assert "From the article:" in out and "Sources:" in out and "en.wikipedia.org/wiki/Marie_Curie" in out
    d = st.to_dict()
    assert d["subject"] == "Marie Curie" and d["inferred"] == 0 and d["citations"]


def test_a_place_and_what_points_to_it(db, world):
    p = Teller(db).tell("Poland").paragraph
    assert "Its capital is Warsaw." in p and "It has a population of about 37.8 million." in p
    assert "It covers 312,696 km²." in p and "Its motto is “Bóg, Honor, Ojczyzna”." in p
    assert "Its official language is Polish (worked out by reasoning)." in p
    w = Teller(db).tell("Warsaw").paragraph
    assert "It is the capital of Poland." in w and "People born here include Marie Curie." in w


def test_disputes_unknowns_and_fallbacks(db, world):
    g: KnowledgeGraph = world["kb"].graph
    pid = g.predicate("P36", "capital")
    krakow = g.upsert_entity("Q31487", "Kraków")
    db.execute("UPDATE triples SET status='disputed' WHERE s=? AND p=?", (world["poland"], pid))
    g.add_triple(world["poland"], pid, o=krakow, status="disputed", kind="wikidata", source="x", confidence=0.4)
    assert "Its capital is Warsaw and Kraków (sources disagree)." in Teller(db).tell("Poland").paragraph
    nothing = Teller(db).tell("Atlantis Unknown")
    assert nothing.paragraph == "" and "don't know enough about Atlantis Unknown" in nothing.render()
    stub = g.upsert_entity("Q999", "Q999")
    thing = g.upsert_entity("Q77", "Gizmo", description="")
    W(g, thing, "P31", "instance of", o=stub)  # its class unread: nothing to say
    W(g, thing, "P279", "subclass of", o=g.upsert_entity("Q78", "tool"))
    W(g, thing, "P1086", "atomic number", value={"amount": 26, "unit": None})
    W(g, thing, "infobox:symbol_type", "symbol type", value={"text": "Emblem"})
    W(g, thing, "infobox:nickname", "nickname", value={"text": "The Gadget"})
    W(g, thing, "infobox:released", "released", value={"text": "1999"})
    p = Teller(db).tell("Gizmo").paragraph
    assert "It is a kind of tool." in p and "Its atomic number is 26." in p and "nickname is The Gadget" in p
    assert "Emblem" not in p and "1999" not in p  # junk labels and bare numbers from infoboxes are skipped


def test_helpers():
    assert join(["a"]) == "a" and join(["a", "b"]) == "a and b" and join(["a", "b", "c"]) == "a, b and c"
    assert join(["a", "b"], 3) == "a, b and 3 more"
    assert article("capital and largest city of France") == "the"
    assert article("country on the west coast of Southern Africa") == "a"
    assert article("largest planet in the Solar System") == "the" and article("English musician") == "an"
    assert article("university in Ohio") == "a" and article("the band") == ""
    assert clean_description("Smallest unit of a chemical element") == "smallest unit of a chemical element"
    assert clean_description("English musician (1947–2016)") == "English musician"
    assert format_date("1867-11-07") == ("on", "7 November 1867")
    assert format_date("1867-11") == ("in", "November 1867") and format_date("1867") == ("in", "1867")
    assert format_date("-0428") == ("in", "428 BC") and format_date("1867-00-00") == ("in", "1867")
    assert format_date("circa 1500") == ("in", "circa 1500")
    assert (
        round_number("1,412,000,000") == "about 1.4 billion"
        and round_number("2,000,000 people") == "about 2 million people"
    )
    assert round_number("950") == "950" and round_number("n/a") == "n/a"
    f = Fact(1, "P569", "date of birth", "1867", "sourced", 0.9, 0, "not json")
    assert f.raw_date() == "1867"
    assert Story("x", "").render().startswith("I don't know enough")


def test_units_render_without_ids(db):
    assert render_value(db, 0, json.dumps({"amount": 357587.77, "unit": "Q712226"})) == "357,588 km²"
    assert render_value(db, 0, json.dumps({"amount": 2.5, "unit": "Q99999999"})) == "2.5"  # unknown unit: left out
    assert render_value(db, 0, json.dumps({"amount": 0.508, "unit": None})) == "0.508"


def test_the_answerer_tells_a_story_for_describe_questions(db, world):
    ans = Answerer(db).ask("Tell me about Marie Curie")
    assert ans.statements[0].kind == "story" and "Curie was born in Warsaw" in ans.statements[0].text
    assert "[" not in ans.statements[0].text  # the answer numbers its own citations
    assert ans.statements[1].kind == "passage" and "physicist and chemist" in ans.statements[1].text
    assert "Curie worked as" in Answerer(db).ask("Who was Marie Curie?").render()
    country = Answerer(db).ask("What is Country01?")  # no story-worthy facts beyond the description: as before
    assert country.statements


def test_cli_tell(db, world, tmp_path, capsys):
    db.conn.commit()
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "tell", "Marie", "Curie"]) == 0
    out = capsys.readouterr().out
    assert "Curie was born in Warsaw" in out and "Sources:" in out
    assert cli.main(["--config", path, "tell", "Poland", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"] == "Poland"
    assert cli.main(["--config", path, "tell", "Nowhere", "Land"]) == 1
