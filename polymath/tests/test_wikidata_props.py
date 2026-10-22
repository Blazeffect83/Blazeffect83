"""Wikidata property bootstrap: locate Property: pages via the multistream index, read only those streams."""

from __future__ import annotations

import bz2
import json
import threading
import time
from types import SimpleNamespace

import pytest

from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.memory import jobs as mj
from polymath.memory.graph import KnowledgeGraph
from polymath.reasoning.inference import learn_rules
from polymath.senses import wikidata_props as wp
from polymath.senses.wikipedia import iter_pages
from tests.fixtures import builders as B
from tests.fixtures.typing import some
from tests.webserver import FakeWeb, client


def prop(pid: str, label: str, *, aliases=(), claims=None) -> str:
    return json.dumps(B.wikidata_entity(pid, label, aliases=list(aliases), claims=claims or {}, kind="property"))


def item(qid: str, label: str) -> str:
    return json.dumps(B.wikidata_entity(qid, label))


PART1 = [
    (1, "Q1", item("Q1", "universe"), None),
    (2, "Property:P31", prop("P31", "instance of", aliases=["is a"]), None, 120),
    (3, "Q2", item("Q2", "Earth"), None),
    (4, "Property:P40", prop("P40", "child", claims={"P1696": [B.snak_item("P1696", "P22")]}), None, 120),
    (5, "Property:P22", prop("P22", "father"), None, 120),
    (6, "Q3", item("Q3", "life"), None),
    (7, "Property:P36", prop("P36", "capital", aliases=["capital city", "seat"]), None, 120),
]
PART2 = [
    (100, "Q100", item("Q100", "thing"), None),
    (101, "Talk:Q100", "a talk page", None, 1),
    (102, "Property:P279", prop("P279", "subclass of", claims={"P31": [B.snak_item("P31", "Q18647515")]}), None, 120),
    (103, "Property:P17", prop("P17", "country"), None, 120),
    (104, "Property:Pbad", "{}", None, 120),
]


@pytest.fixture()
def wd_web():
    web = FakeWeb()
    d1, i1 = B.wiki_multistream(PART1, per_stream=2)
    d2, i2 = B.wiki_multistream(PART2, per_stream=3)
    web.add("/wikidatawiki/", '<a href="20261001/">20261001/</a>')
    web.add(
        "/wikidatawiki/20261001/dumpstatus.json",
        B.dumpstatus(
            {
                "wikidatawiki-20261001-pages-articles-multistream1.xml-p1p99.bz2": (len(d1), "/wd/d1.bz2"),
                "wikidatawiki-20261001-pages-articles-multistream-index1.txt-p1p99.bz2": (len(i1), "/wd/i1.bz2"),
                "wikidatawiki-20261001-pages-articles-multistream2.xml-p100p199.bz2": (len(d2), "/wd/d2.bz2"),
                "wikidatawiki-20261001-pages-articles-multistream-index2.txt-p100p199.bz2": (len(i2), "/wd/i2.bz2"),
            }
        ),
        ctype="application/json",
    )
    for name, body in (("d1", d1), ("i1", i1), ("d2", d2), ("i2", i2)):
        web.add(f"/wd/{name}.bz2", body, ctype="application/octet-stream")
    yield web, {"d1": d1, "i1": i1, "d2": d2, "i2": i2}
    web.close()


def ctx_for(config, db, kind, payload=None, checkpoint=None, budget=30.0):
    s = Scheduler(db)
    jid, _ = s.enqueue(kind, payload or {}, key=f"{kind}:{time.time_ns()}")
    return JobContext(
        config=config,
        db=db,
        scheduler=s,
        job=Job(jid, kind, f"k{jid}", payload or {}, checkpoint, 0, 0, 0, 5, 0),
        deadline=time.monotonic() + budget,
        stop_event=threading.Event(),
        services={"http": client()},
    )


def test_property_streams_from_an_index_part(wd_web):
    _web, files = wd_web
    found = wp.property_streams(files["i1"], len(files["d1"]))
    lines = bz2.decompress(files["i1"]).decode().splitlines()
    offs = sorted({int(x.split(":")[0]) for x in lines})
    assert [f[0] for f in found] == ["P31", "P40", "P22", "P36"]
    p31 = next(f for f in found if f[0] == "P31")
    assert p31[1] == offs[0] and p31[2] == offs[1]  # P31 shares the first stream with Q1
    assert found[-1] == ("P36", offs[-1], len(files["d1"]))  # last stream ends at the end of the file
    pages = list(iter_pages(bz2.decompress(files["d1"][p31[1] : p31[2]])))
    assert {p.title for p in pages} == {"Q1", "Property:P31"}
    # concatenated bz2 streams in one index file, no trailing newline, junk lines
    raw = bz2.compress(b"10:1:Property:P5\n") + bz2.compress(b"garbage\n20:2:Q9\n30:3:Property:P6")
    assert wp.property_streams(raw, 99) == [("P5", 10, 20), ("P6", 30, 99)]
    assert wp.property_streams(bz2.compress(b""), 5) == []


def test_index_then_read_properties_end_to_end(config, db, wd_web):
    web, _files = wd_web
    g = KnowledgeGraph(db)
    g.predicate("P36")  # already used by known facts → read first
    g.predicate("P17")
    out = wp.propindex_job(ctx_for(config, db, "wikidata.propindex", {"base": web.base}, budget=0))
    assert not out.done and out.result == {"parts_done": 1, "parts": 2, "properties": 4}  # one part per slice
    out = wp.propindex_job(ctx_for(config, db, "wikidata.propindex", {"base": web.base}, out.checkpoint, budget=0))
    assert out.done and out.result["properties"] == 6  # "Property:Pbad" is not a property id
    assert db.kv_get("wd_propindex")["date"] == "20261001"
    assert db.scalar("SELECT COUNT(*) FROM wd_property_pages") == 6
    due = wp.due_streams(db, 10)
    assert [bool(r["used"]) for r in due][:2] == [True, True] and not any(r["used"] for r in due[2:])

    first = wp.properties_job(ctx_for(config, db, "wikidata.properties", budget=0))
    assert first.result["streams"] == 1 and first.result["more"] and not first.done  # bounded slice
    rest = wp.properties_job(ctx_for(config, db, "wikidata.properties"))
    assert rest.done and not rest.result["more"]
    stored = {r["qid"] for r in db.query("SELECT qid FROM wd_entities")}
    assert stored == {"P31", "P40", "P22", "P36", "P279", "P17"}  # items in the same streams are not stored
    reads = [(path, h.get("range")) for _t, _m, path, h in web.log if path.startswith("/wd/d")]
    assert len(reads) == len(set(reads)) == 6 and all(r for _p, r in reads)  # one range request per stream
    assert wp.properties_job(ctx_for(config, db, "wikidata.properties")).result["streams"] == 0
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='reason.rules'") == 1  # rules refresh after the last read

    mj.build_graph(ctx_for(config, db, "memory.graph"))
    assert db.scalar("SELECT label FROM predicates WHERE key='P36'") == "capital"
    cap = some(g.by_key("P36"))
    assert cap.kind == "property" and {c[1] for c in g.candidates("capital city")} == {"capital"}
    # the property's own metadata became facts, which the rule learner trusts
    p40 = some(g.by_key("P40"))
    inv = int(db.scalar("SELECT id FROM predicates WHERE key='P1696'"))
    assert db.scalar("SELECT o FROM triples WHERE s=? AND p=?", (p40.id, inv)) == some(g.by_key("P22")).id
    sub = g.predicate("P279")
    a, b = g.upsert_entity("Q900", "cat"), g.upsert_entity("Q901", "mammal")
    g.add_triple(a, sub, o=b, kind="wikidata", source="wikidata")  # one fact is enough for a declared rule
    found = learn_rules(db)
    assert found["inverse"] >= 1 and found["transitive"] >= 1
    kinds = {
        (r["kind"], r["key"]) for r in db.query("SELECT r.kind, p.key FROM rules r JOIN predicates p ON p.id = r.p")
    }
    assert ("transitive", "P279") in kinds and ("inverse", "P40") in kinds


def test_planner_schedules_index_and_reading(config, db):
    agent = SimpleNamespace(db=db, scheduler=Scheduler(db))
    wp.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='wikidata.propindex'") == 1
    wp.planner(agent)  # idempotent within the month
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind='wikidata.propindex'") == 1
    assert not db.scalar("SELECT 1 FROM jobs WHERE kind='wikidata.properties'")
    db.execute("INSERT INTO wd_property_pages(pid, dump_url, stream, next) VALUES('P1', 'u', 0, 9)")
    db.kv_set("wd_propindex", {"date": "x", "found": 1, "at": time.time()})
    db.execute("DELETE FROM jobs")
    wp.planner(agent)
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs")}
    assert kinds == {"wikidata.properties"}


def test_transient_failure_keeps_slice_progress(config, db, wd_web):
    web, _files = wd_web
    out = wp.propindex_job(ctx_for(config, db, "wikidata.propindex", {"base": web.base}))
    assert out.done
    busy = (503, {"Content-Type": "text/plain"}, b"busy")
    web.routes["/wd/d2.bz2"].handler = lambda _h: busy  # the second part's server is having a bad moment
    res = wp.properties_job(ctx_for(config, db, "wikidata.properties"))
    assert not res.done and res.delay == wp.RETRY_DELAY and "503" in res.result["error"]
    fetched = db.scalar("SELECT COUNT(*) FROM wd_property_pages WHERE fetched IS NOT NULL")
    assert fetched == res.result["streams"] > 0  # streams read before the failure stay read
    web.routes["/wd/d2.bz2"].handler = None
    assert wp.properties_job(ctx_for(config, db, "wikidata.properties")).done
    web.routes["/wd/d1.bz2"].handler = lambda _h: (404, {"Content-Type": "text/plain"}, b"gone")  # not hidden
    db.execute("UPDATE wd_property_pages SET fetched=NULL")
    with pytest.raises(Exception, match="404"):
        wp.properties_job(ctx_for(config, db, "wikidata.properties"))
