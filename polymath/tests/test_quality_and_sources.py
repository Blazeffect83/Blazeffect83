"""v0.5: impossible facts, tables, a second language, merged duplicates, big-brain mode."""

from __future__ import annotations

import json
from typing import Any

import pytest

from polymath.core.config import BIG_BRAIN_GB, ConfigError, apply_capacity, brain_tier, load_config
from polymath.core.scheduler import Scheduler
from polymath.drive import capacity
from polymath.memory import merge
from polymath.memory.graph import KnowledgeGraph
from polymath.perception.infobox import extract_infobox_triples
from polymath.perception.jobs import resolve_title
from polymath.perception.tables import header_key, table_facts
from polymath.reasoning import sanity
from polymath.senses.wikitext import clean_wikitext, parse_table
from tests.conftest import make_config
from tests.world import World, q, t


def agent_for(db, config=None):  # type: ignore[no-untyped-def]
    return type("A", (), {"db": db, "scheduler": Scheduler(db), "config": config})()


# ------------------------------------------------------------------ impossible facts
def test_impossible_facts_are_caught_and_the_weaker_side_set_aside(db):
    w = World(db)
    for key, label in (("Q1", "Ada"), ("Q2", "Bob"), ("Q3", "Cy"), ("Q4", "Dee"), ("Q5", "Eve"),
                       ("Q10", "Smallland"), ("Q11", "Bigtown")):  # fmt: skip
        w.ent(key, label)
    w.fact("Ada", "P569", "date of birth", value=t("1890-01-01"))
    bad_death = w.fact(
        "Ada", "P570", "date of death", value=t("1820-01-01"), conf=0.6, kind="infobox", source="wikipedia"
    )
    w.fact("Bob", "P569", "date of birth", value=t("1700"))
    old = w.fact("Bob", "P570", "date of death", value=t("1950"), conf=0.5, kind="pattern", source="text")
    future = w.fact("Cy", "P569", "date of birth", value=t("2999"), kind="infobox", source="wikipedia")
    w.fact("Dee", "P569", "date of birth", value=t("1950"))
    w.fact("Eve", "P569", "date of birth", value=t("1945"))
    child = w.fact("Dee", "P40", "child", o="Eve", kind="pattern", source="text")  # the child is older
    w.fact("Bigtown", "P131", "located in the administrative territorial entity", o="Smallland")
    w.fact("Smallland", "P2046", "area", value=q(100, "Q712226"))
    big = w.fact("Bigtown", "P2046", "area", value=q(5000, "Q712226"), conf=0.5, kind="infobox", source="wikipedia")
    res = sanity.check(db)
    assert res["caught"] == 5, res
    statuses = {
        tid: db.scalar("SELECT status FROM triples WHERE id = ?", (tid,))
        for tid in (bad_death, old, future, child, big)
    }
    assert set(statuses.values()) == {"disputed"}
    rules = {str(r["rule"]) for r in db.query("SELECT rule FROM sanity")}
    assert rules == {
        "died before born",
        "too old",
        "from the future",
        "child older than parent",
        "part bigger than whole",
    }
    summary = db.scalar("SELECT summary FROM self_changes WHERE action = 'corrected'")
    assert summary.startswith("Caught 5 facts that cannot be true") and "Ada: died 1 January 1820" in summary
    assert sanity.check(db)["caught"] == 0  # each case once


def test_two_wikidata_facts_that_clash_are_only_noted(db):
    w = World(db)
    w.ent("Q1", "Ada")
    w.fact("Ada", "P569", "date of birth", value=t("1890"))
    death = w.fact("Ada", "P570", "date of death", value=t("1820"))
    sanity.check(db)
    assert db.scalar("SELECT action FROM sanity") == "noted"
    assert db.scalar("SELECT status FROM triples WHERE id = ?", (death,)) == "sourced"
    assert sanity.weaker(db, death, death) == death


def test_a_learned_range_flags_values_far_outside_it(db):
    w = World(db)
    pop = w.pred("P1082", "population", datatype="quantity")
    for i in range(120):
        e = w.ent(f"Q{100 + i}", f"Town{i}")
        w.g.add_triple(e, pop, value=q(10_000 + 137 * i), kind="wikidata", source="wikidata", confidence=0.9)
    tiny = w.ent("Q999", "Oddville")
    bad, _ = w.g.add_triple(tiny, pop, value=q(1e15), kind="infobox", source="wikipedia", confidence=0.7)
    neg, _ = w.g.add_triple(w.ent("Q998", "Minus"), pop, value=q(-5), kind="table", source="wikipedia", confidence=0.6)
    res = sanity.check(db)
    assert res["caught"] == 2
    assert {db.scalar("SELECT status FROM triples WHERE id = ?", (x,)) for x in (bad, neg)} == {"disputed"}
    r = db.one("SELECT * FROM value_ranges WHERE p = ?", (pop,))
    assert r["n"] == 122 and r["positive"] == 1 and r["lo"] < 10_000 < 27_000 < r["hi"] < 1e15


def test_sanity_job_and_planner(db):
    agent = agent_for(db)
    sanity.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'reason.sanity'") == 0
    World(db).ent("Q1", "Ada")
    w = World(db)
    w.e["Ada"] = int(db.scalar("SELECT id FROM entities WHERE key = 'Q1'"))
    w.fact("Ada", "P569", "date of birth", value=t("1890"))
    sanity.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'reason.sanity'") == 1
    assert sanity.sanity_job(type("C", (), {"db": db})()).done


# ------------------------------------------------------------------ tables
TABLE = """{| class="wikitable sortable"
|+ Tallest mountains
! Mountain !! Height (m) !! Country !! Notes
|-
| [[Mount Everest]] || 8,849 m || [[Nepal]] || highest
|-
| [[K2]] || 8,611 m || [[Pakistan]] || second
|-
| style="background:#eee" | [[Kangchenjunga]] || 8,586 m || [[Nepal]] || third
|-
| [[Lhotse]] || 8,516 m || [[Nepal]]
|}"""


def test_wikitables_are_parsed_and_layout_tables_skipped():
    tab = parse_table(TABLE)
    assert tab is not None and tab["caption"] == "Tallest mountains"
    assert tab["headers"] == ["Mountain", "Height (m)", "Country", "Notes"]
    assert len(tab["rows"]) == 3  # the short last row does not line up and is dropped
    assert tab["rows"][2][0] == {"text": "Kangchenjunga", "link": "Kangchenjunga"}
    assert parse_table("{| style='width:100%'\n! a !! b\n|-\n| x || y\n|}") is None  # no "wikitable"
    assert parse_table('{| class="wikitable"\n|-\n| x || y\n|}') is None  # no headers
    page = clean_wikitext("Some prose about mountains that matters.\n" + TABLE + "\nMore prose here.")
    assert page.tables and "Everest" not in page.text and "More prose here." in page.text
    assert header_key("Height (m)[1]") == "height" and header_key("Date of birth") == "date_of_birth"


def test_tables_become_facts_aligned_like_infoboxes(db):
    w = World(db)
    for key, label in (("Q513", "Mount Everest"), ("Q43512", "K2"), ("Q159", "Kangchenjunga"), ("Q837", "Nepal"),
                       ("Q843", "Pakistan")):  # fmt: skip
        w.ent(key, label)
        db.execute("UPDATE entities SET wiki_title = ? WHERE id = ?", (label, w.e[label]))
    w.fact("Mount Everest", "P17", "country", o="Nepal")  # Wikidata agrees with the table
    tab = parse_table(TABLE)
    assert tab is not None
    n = table_facts(db, w.g, 1, "List of mountains", [tab], lambda title: resolve_title(db, w.g, title, create=False))
    assert n == 6  # 3 rows × (height, country); notes skipped
    k2_height = db.scalar("SELECT t.value FROM triples t JOIN predicates p ON p.id = t.p WHERE p.key = 'table:height' "
                          "AND t.s = ?", (w.e["K2"],))  # fmt: skip
    assert json.loads(k2_height) == {"amount": 8611.0, "unit": "m"}
    assert db.scalar("SELECT agree FROM infobox_alignment WHERE infobox_key = 'table:country' AND pid = 'P17'") == 1
    unresolved = {"headers": ["A", "B"], "rows": [[{"text": "x", "link": "Nowhere"}, {"text": "1", "link": None}]] * 3}
    assert table_facts(db, w.g, 1, "x", [unresolved], lambda title: None) == 0


# ------------------------------------------------------------------ a second language
def test_a_second_language_resolves_through_sitelinks_and_cross_checks(db):
    w = World(db)
    w.ent("Q183", "Germany")
    w.ent("Q64", "Berlin")
    db.execute("INSERT INTO wiki_sitelinks(lang, title, qid) VALUES('es', 'Alemania', 'Q183'), ('es', 'Berlín', 'Q64')")
    assert resolve_title(db, w.g, "Alemania", lang="es") == w.e["Germany"]
    assert resolve_title(db, w.g, "Nadie", lang="es") is None  # never creates anything
    cap = w.pred("P36", "capital")
    wd, _ = w.g.add_triple(w.e["Germany"], cap, o=w.e["Berlin"], kind="wikidata", source="wikidata", confidence=0.9)
    db.execute("INSERT INTO infobox_alignment(infobox_key, pid, agree) VALUES('es:capital', 'P36', 5)")
    boxes = [{"name": "Ficha de país", "params": {"capital": "[[Berlín]]", "imagen": "x.png"}}]
    added = extract_infobox_triples(db, w.g, w.e["Germany"], 7, boxes, lambda ti: resolve_title(db, w.g, ti, lang="es"),
                                    prefix="es:")  # fmt: skip
    assert added == 1
    assert db.scalar("SELECT n_sources FROM triples WHERE id = ?", (wd,)) == 2  # the Spanish article backs it up
    unaligned = [{"name": "Ficha de país", "params": {"idioma": "[[Berlín]]"}}]
    extract_infobox_triples(db, w.g, w.e["Germany"], 7, unaligned, lambda ti: resolve_title(db, w.g, ti, lang="es"),
                            prefix="es:")  # fmt: skip
    assert db.scalar("SELECT 1 FROM predicates WHERE key = 'infobox:es:idioma'")


def test_second_language_setting_is_checked(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('[senses]\nsecond_language = "xx"\n')
    with pytest.raises(ConfigError, match="second_language"):
        load_config(path)
    path.write_text('[senses]\nwikipedia_lang = "es"\nsecond_language = "es"\n')
    with pytest.raises(ConfigError, match="differ"):
        load_config(path)


# ------------------------------------------------------------------ merged duplicates
def test_duplicate_entries_are_merged_with_evidence(db):
    g = KnowledgeGraph(db)
    nyc = g.upsert_entity("Q60", "New York City", wiki_title="New York City")
    g.add_alias("New York City", nyc, "label")
    db.execute("INSERT INTO wiki_redirects(lang, title, target) VALUES('en', 'NYC', 'New York City')")
    ph = g.upsert_entity("wiki:NYC", "NYC", wiki_title="NYC")
    mayor = g.predicate("infobox:mayor", "mayor")
    adams = g.upsert_entity("Q7", "Eric Adams")
    g.add_triple(ph, mayor, o=adams, kind="infobox", source="wikipedia")
    from polymath.memory.documents import Document, DocumentStore

    doc, _ = DocumentStore(db).add(Document("wikipedia", "en:1", "NYC", "New York City text. " * 20, "CC BY-SA 4.0"))
    db.execute("INSERT INTO doc_entities(doc_id, entity_id, count, score) VALUES(?, ?, 2, 0.5)", (doc, ph))
    # unique name: one Wikidata item called "Big Apple Tower"
    tower = g.upsert_entity("Q61", "Big Apple Tower", wiki_title="Big Apple Tower (building)")
    g.add_alias("Big Apple Tower", tower, "label")
    ph2 = g.upsert_entity("wiki:Big Apple Tower", "Big Apple Tower", wiki_title="Big Apple Tower")
    # conflicting dates: not merged
    born = g.predicate("P569", "date of birth")
    jo = g.upsert_entity("Q62", "Jo Smith", wiki_title="Jo Smith (painter)")
    g.add_alias("Jo Smith", jo, "label")
    g.add_triple(jo, born, value={"time": "1901"}, kind="wikidata", source="wikidata")
    ph3 = g.upsert_entity("wiki:Jo Smith", "Jo Smith", wiki_title="Jo Smith")
    g.add_triple(ph3, born, value={"time": "1955"}, kind="infobox", source="wikipedia")
    res = merge.run(db)
    assert res["merged"] == 2 and res["remaining"] == 0
    assert g.entity(ph) is None and g.entity(ph2) is None and g.entity(ph3) is not None
    assert db.scalar("SELECT o FROM triples WHERE s = ? AND p = ?", (nyc, mayor)) == adams
    assert db.scalar("SELECT entity_id FROM doc_entities WHERE doc_id = ?", (doc,)) == nyc
    assert db.scalar("SELECT kept_id FROM entity_merges WHERE dropped_key = 'wiki:NYC'") == nyc
    summary = db.scalar("SELECT summary FROM self_changes WHERE action = 'merged'")
    assert summary.startswith("Merged 2 duplicate entries into one") and "“NYC” redirects to “New York City”" in summary
    agent = agent_for(db)
    merge.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'memory.merge'") == 1
    assert merge.merge_job(type("C", (), {"db": db, "tick": staticmethod(lambda: None)})()).result["merged"] == 0


# ------------------------------------------------------------------ big-brain mode
def test_big_brain_mode_raises_defaults_but_not_your_settings(tmp_path):
    cfg = make_config(tmp_path)
    assert brain_tier(cfg) == "normal" and apply_capacity(cfg) == []
    cfg.body.disk_budget_gb = BIG_BRAIN_GB
    changed = apply_capacity(cfg, {"senses": {"gutenberg_books": 50}})
    assert brain_tier(cfg) == "big" and "senses.gutenberg_books" not in changed
    assert cfg.senses.gutenberg_books == 200 and cfg.senses.openalex_files == 40  # yours stays as you set it
    assert cfg.senses.second_language == "es" and cfg.learning.embedding_vocab_max == 400_000
    assert len(cfg.senses.stackexchange_sites) == 15
    assert apply_capacity(cfg, {"senses": {"gutenberg_books": 50}}) == []  # already raised
    home = tmp_path / "data" / "home-drive.json"
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text(json.dumps({"id": "u1", "name": "T9", "budget_gb": 900}))
    path = tmp_path / "p.toml"
    path.write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\nrequire_separate_mount = false\n')
    loaded = load_config(path)
    assert loaded.body.disk_budget_gb == 900 and loaded.senses.second_language == "es"


def test_big_brain_switch_is_announced_once(tmp_path, db):
    cfg = make_config(tmp_path)
    agent = agent_for(db, cfg)
    capacity.planner(agent)
    assert db.kv_get("brain_tier") == "normal" and db.scalar("SELECT COUNT(*) FROM self_changes") == 0
    cfg.body.disk_budget_gb = 900
    capacity.planner(agent)
    capacity.planner(agent)
    rows = db.query("SELECT summary FROM self_changes WHERE area = 'capacity'")
    assert len(rows) == 1 and rows[0]["summary"].startswith("Switched to big-brain mode (900 GB of room)")
    cfg.body.disk_budget_gb = 100
    capacity.planner(agent)
    assert db.scalar("SELECT summary FROM self_changes WHERE area = 'capacity' ORDER BY id DESC LIMIT 1").startswith(
        "Back to normal mode"
    )


def test_a_spanish_article_is_read_for_its_infobox_and_tables_only(config, db):
    """End to end: index (no entity, no text index), anchors (cross-check), never read as English (stage 3)."""
    from polymath.memory import jobs as mj
    from polymath.memory.documents import Document, DocumentStore
    from polymath.perception import jobs as pj
    from tests.test_perception import ctx_for

    w = World(db)
    w.ent("Q183", "Germany")
    w.ent("Q64", "Berlin")
    db.execute("INSERT INTO wiki_sitelinks(lang, title, qid) VALUES('es', 'Alemania', 'Q183'), ('es', 'Berlín', 'Q64')")
    db.execute("INSERT INTO infobox_alignment(infobox_key, pid, agree) VALUES('es:capital', 'P36', 4)")
    cap = w.pred("P36", "capital")
    wd, _ = w.g.add_triple(w.e["Germany"], cap, o=w.e["Berlin"], kind="wikidata", source="wikidata", confidence=0.9)
    store = DocumentStore(db)
    store.add(Document("wikipedia", "es:1", "Alemania", "Alemania es un país de Europa central. " * 20, "CC BY-SA 4.0",
                       lang="es", extra={"infoboxes": [{"name": "Ficha de país", "params": {"capital": "[[Berlín]]"}}],
                                         "tables": []}))  # fmt: skip
    services = {"docs": store}
    before = db.scalar("SELECT COUNT(*) FROM entities")
    out = mj.index_documents(ctx_for(config, db, "memory.index", services))
    assert out.result["indexed"] == 1 and db.scalar("SELECT COUNT(*) FROM entities") == before
    assert db.scalar("SELECT COUNT(*) FROM chunks") == 0  # not searched as English text
    out = pj.anchor_job(ctx_for(config, db, "perception.anchors", services))
    assert out.result["docs"] == 1 and out.result["infobox_facts"] == 1
    assert db.scalar("SELECT stage FROM documents WHERE external_id = 'es:1'") == 3
    assert db.scalar("SELECT n_sources FROM triples WHERE id = ?", (wd,)) == 2


def test_wikidata_sitelinks_are_stored(db):
    from polymath.senses.wikidata import store_entity

    ctx = type("C", (), {"db": db})()
    ent: dict[str, Any] = {"qid": "Q183", "label": "Germany", "description": None, "aliases": [], "enwiki": "Germany", "type": "item",
           "claims": {"_sitelinks": {"es": "Alemania", "de": "Deutschland"}}}  # fmt: skip
    assert store_entity(ctx, ent)  # type: ignore[arg-type]
    assert dict(db.query("SELECT lang, title FROM wiki_sitelinks WHERE qid = 'Q183' ORDER BY lang")) == {  # type: ignore[arg-type]
        "de": "Deutschland", "es": "Alemania"}  # fmt: skip
