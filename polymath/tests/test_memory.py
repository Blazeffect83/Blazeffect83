"""Phase 3: passages + FTS5, SimHash/MinHash near-duplicates, knowledge graph, topic map, memory jobs."""

from __future__ import annotations

import json
import random
import threading
import time

import numpy as np

from polymath.body.systemd_notify import Notifier
from polymath.core.app import build_components
from polymath.core.jobs import JobContext
from polymath.core.loop import Agent
from polymath.core.scheduler import Job, Scheduler
from polymath.memory import jobs as mj
from polymath.memory.dedup import NearDupIndex, hamming, jaccard_estimate, minhash, shingles, simhash
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph, norm_alias
from polymath.memory.text_index import TextIndex, chunk_spans, fts_query
from polymath.memory.topics import TopicMap, clean_topic, labels_from_meta
from polymath.senses.wikipedia import Page, store_category_edges

WORDS = ["energy", "matter", "atom", "photon", "electron", "proton", "neutron", "field", "wave", "particle", "force", "mass", "charge", "spin", "quark", "lepton", "boson", "gluon", "star", "planet", "galaxy", "orbit", "gravity", "light", "time", "space"]


def prose(seed: int, n: int = 120) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(WORDS) for _ in range(n)) + "."


def ctx_for(config, db, kind, services, payload=None):
    s = Scheduler(db)
    key = f"{kind}:{time.time_ns()}"
    jid, _ = s.enqueue(kind, payload or {}, key=key)
    job = Job(jid, kind, key, payload or {}, None, 0, 0, 0, 5, 0)
    return JobContext(config=config, db=db, scheduler=s, job=job, deadline=time.monotonic() + 30,
                      stop_event=threading.Event(), services=services)


# ------------------------------------------------------------------ passages


def test_chunk_spans_cover_text_and_respect_limits():
    paras = [prose(i, 40) for i in range(10)] + [prose(99, 600)]
    text = "\n\n".join(paras)
    spans = chunk_spans(text)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(b - a <= 1600 for a, b in spans)
    covered = "".join(text[a:b] for a, b in spans).replace(" ", "").replace("\n", "")
    assert covered == text.replace("\n", "").replace(" ", "")
    assert chunk_spans("") == [] and chunk_spans("one para") == [(0, 8)]


def test_fts_query_is_injection_safe():
    q = fts_query('cats AND dogs OR NEAR(x) "quoted" -minus col:val ’tis')
    assert q == '"cats" OR "and" OR "dogs" OR "or" OR "near" OR "x" OR "quoted" OR "minus" OR "col" OR "val" OR "tis"'
    assert fts_query("***") == "" and fts_query("a b", mode="and") == '"a" AND "b"'


def test_text_index_search_unindex_and_update(db):
    store = DocumentStore(db)
    ti = TextIndex(db, store)
    docs = [("Raspberry Pi thermal throttling", "The Raspberry Pi 5 throttles its CPU at 85 degrees Celsius. " * 5),
            ("Photosynthesis", "Plants convert light into chemical energy using chlorophyll in chloroplasts. " * 5),
            ("Minecraft servers", "PaperMC is a fast Minecraft server that runs plugins written in Java. " * 5)]
    ids = []
    for i, (title, text) in enumerate(docs):
        did, _ = store.add(Document("wikipedia" if i < 2 else "web", str(i), title, text, "CC BY-SA 4.0"))
        ti.index(did, title, text)
        ids.append(did)
    hits = ti.search("at what temperature does the Pi throttle")
    assert hits and hits[0].doc_id == ids[0] and "85 degrees" in hits[0].text and hits[0].license == "CC BY-SA 4.0"
    assert ti.search("chlorophyll", sources=["web"]) == []
    assert ti.search_docs("minecraft plugins")[0][0] == ids[2]
    assert ti.search("") == [] and ti.search_docs("!!!") == []
    # updating a document removes its old passages from the contentless index
    store.add(Document("wikipedia", "0", docs[0][0], "Completely different words about gardening tools. " * 4,
                       "CC BY-SA 4.0"))
    assert ti.search("throttles celsius") == []
    ti.unindex(ids[1], docs[1][0], docs[1][1])
    assert ti.search("chlorophyll") == []


# ------------------------------------------------------------ near-duplicates


def test_simhash_and_minhash_behaviour():
    a = prose(1, 400)
    b = a.replace("photon", "photons", 3)  # light edit
    c = prose(2, 400)
    assert simhash(a) == simhash(a)
    assert hamming(simhash(a), simhash(b)) < hamming(simhash(a), simhash(c))
    assert -(1 << 63) <= simhash(c) < (1 << 63) and simhash("") == 0
    sa, sb, sc = minhash(a), minhash(b), minhash(c)
    true_j = len(set(shingles(a)) & set(shingles(b))) / len(set(shingles(a)) | set(shingles(b)))
    assert abs(jaccard_estimate(sa, sb) - true_j) < 0.12
    assert jaccard_estimate(sa, sc) < 0.2
    assert minhash("").max() == np.iinfo(np.uint64).max and shingles("two words") == ["two words"]


def test_near_dup_index(db):
    nd = NearDupIndex(db)
    base = prose(5, 500)
    nd.add(1, minhash(base))
    nd.add(2, minhash(prose(6, 500)))
    found = nd.find(minhash(base + " extra tail words"))
    assert found and found[0] == 1 and found[1] >= 0.85
    assert nd.find(minhash(prose(7, 500))) is None
    assert nd.find(minhash(base), exclude=1) is None
    nd.remove(1)
    nd.remove(1)  # idempotent
    assert nd.find(minhash(base)) is None
    assert db.scalar("SELECT COUNT(*) FROM minhash_bands WHERE doc_id=1") == 0


# ------------------------------------------------------------------- graph


def test_graph_entities_aliases_and_placeholder_merge(db):
    g = KnowledgeGraph(db)
    w = g.upsert_entity("wiki:Paris", "Paris", wiki_title="Paris", doc_id=7)
    q = g.upsert_entity("Q90", "Paris", description="capital of France", wiki_title="Paris")
    assert q == w and g.by_key("Q90").doc_id == 7 and g.by_key("wiki:Paris") is None  # type: ignore[union-attr]
    s = g.stub("Q142")
    assert g.entity(s).kind == "stub" and g.stub("Q142") == s  # type: ignore[union-attr]
    g.upsert_entity("Q142", "France")
    assert g.upsert_entity("Q142", "Q142", kind="stub") == s and g.entity(s).label == "France"  # type: ignore[union-attr]
    g.add_alias("Paris", q, "label")
    g.add_alias("PARIS!", q, "anchor", 5)
    other = g.upsert_entity("Q830149", "Paris (mythology)")
    g.add_alias("Paris", other, "anchor", 2)
    g.add_alias("", other, "anchor")
    cands = g.candidates("paris")
    assert [c[0] for c in cands] == [q, other] and cands[0][2] == 6
    assert norm_alias("  São-Paulo  ") == "são paulo"
    assert g.by_wiki_title("Paris").id == q and g.entity(999) is None  # type: ignore[union-attr]


def test_graph_triples_evidence_status_neighbors_and_holdout(db):
    g = KnowledgeGraph(db)
    fr, pa, ly = (g.upsert_entity(k, lab) for k, lab in (("Q142", "France"), ("Q90", "Paris"), ("Q456", "Lyon")))
    cap = g.predicate("P36", "capital")
    pop = g.predicate("P1082", "population", datatype="quantity")
    assert g.predicate("P36") == cap and g.predicate_info(cap)["label"] == "capital"
    t1, new = g.add_triple(fr, cap, o=pa, kind="wikidata", source="wikidata", detail="P36")
    assert new
    _, again = g.add_triple(fr, cap, o=pa, kind="wikidata", source="wikidata", detail="P36")
    assert not again  # same evidence is not double counted
    g.add_triple(fr, cap, o=pa, kind="pattern", source="wikipedia", doc_id=3, detail="X is the capital of Y")
    t2, _ = g.add_triple(fr, pop, value={"amount": 68e6}, kind="wikidata", source="wikidata")
    t3, _ = g.add_triple(ly, cap, o=pa, status="inferred", kind="rule", source="rule", detail="test")
    g.add_triple(ly, cap, o=pa, kind="pattern", source="web", doc_id=4)
    rows = {r["id"]: r for r in db.query("SELECT * FROM triples")}
    assert rows[t1]["n_sources"] == 2 and rows[t3]["status"] == "sourced"  # evidence upgrades inferred
    assert g.objects(fr, cap) == [pa] and g.objects(fr, pop) == [{"amount": 68000000.0}]
    edges = g.neighbors(pa)
    assert {(e.direction, e.other_label) for e in edges} == {("in", "France"), ("in", "Lyon")}
    out = g.neighbors(fr)
    assert any(e.value == {"amount": 68000000.0} for e in out)
    db.execute("UPDATE triples SET holdout=1 WHERE id=?", (t1,))
    assert g.objects(fr, cap) == [] and g.objects(fr, cap, include_holdout=True) == [pa]
    assert all(e.triple_id != t1 for e in g.neighbors(pa))
    g.set_status(t2, "disputed", 0.3)
    g.set_status(t2, "sourced")
    assert db.scalar("SELECT confidence FROM triples WHERE id=?", (t2,)) == 0.3
    prov = g.provenance(t1)
    assert {p["kind"] for p in prov} == {"wikidata", "pattern"}
    st = g.stats()
    assert st["entities"] == 3 and st["triples_sourced"] == 3


# ------------------------------------------------------------------- topics


def test_topic_cleaning_and_labels():
    assert clean_topic("Living people") is None and clean_topic("1950 births") is None
    assert clean_topic("Articles with short description") is None and clean_topic("1990s") is None
    assert clean_topic("planetary_science") == "Planetary science"
    labels = labels_from_meta("x", {"categories": ["Planets", "Living people"], "mesh": ["Humans"],
                                    "tags": ["neural-networks"], "bookshelves": ["Category: Novels"],
                                    "topics": ["Machine learning"], "subjects": ["Fiction -- Juvenile"]})
    assert ("Planets", "category") in labels and ("Neural networks", "tag") in labels
    assert ("Novels", "subject") in labels and ("Fiction", "subject") in labels
    assert len(labels) == len(set(labels))


def test_topic_hierarchy_levels_rollup_and_cycles(db):
    tm = TopicMap(db)
    sci, phys, astro, planets = (tm.topic(n, "category") for n in ("Science", "Physics", "Astronomy", "Planets"))
    tm.link(phys, sci, "t")
    tm.link(astro, sci, "t")
    tm.link(planets, astro, "t")
    tm.link(planets, phys, "t")  # DAG with two parents
    a, b = tm.topic("Cycle A"), tm.topic("Cycle B")
    tm.link(a, b, "t")
    tm.link(b, a, "t")
    tm.link(sci, sci, "t")  # self loops ignored
    store = DocumentStore(db)
    ids = [store.add(Document("web", str(i), "t", prose(i + 50), "x"))[0] for i in range(4)]
    for d in ids[:3]:
        assert tm.assign(d, planets)
    assert not tm.assign(ids[0], planets)
    tm.assign(ids[3], phys)
    st = tm.recompute()
    lv = {r["name"]: (r["level"], r["n_total"]) for r in db.query("SELECT * FROM topics")}
    assert lv["Science"] == (0, 4) and lv["Planets"][0] == 2
    assert lv["Cycle A"][0] == 0 and st["max_level"] == 2
    assert tm.path_to_root(planets)[-1] == "Science"
    assert next(c["name"] for c in tm.children(sci)) in {"Physics", "Astronomy"}
    assert tm.top(1)[0]["name"] == "Science"


def test_category_pages_become_topic_edges(config, db):
    ctx = ctx_for(config, db, "x", {})
    page = Page(1, 14, "Category:Planets of the Solar System", None,
                "[[Category:Planets]] [[Category:Solar System|P]] [[Category:Planets of the Solar System]]", None)
    assert store_category_edges(ctx, "en", page) == 3
    tm = TopicMap(db)
    tm.topic("Planets of the Solar System", "category")
    assert tm.import_category_edges() == 2
    assert {r["name"] for r in db.query("SELECT name FROM topics")} >= {"Planets", "Solar System"}


# ------------------------------------------------------------------- jobs


def test_index_job_dedupes_links_entities_and_topics(config, db):
    store = DocumentStore(db)
    base = prose(11, 600)
    store.add(Document("wikipedia", "en:1", "Atom", base, "CC BY-SA 4.0",
                       meta={"categories": ["Physics", "Living people"], "short_description": "Basic unit"}))
    store.add(Document("web", "u", "Atom copy", base + " A mirror footer line.", "unknown"))
    store.add(Document("pubmed", "9", "Trial", prose(12, 300), "NLM", meta={"mesh": ["Humans"]}))
    store.add(Document("wikipedia", "en:2", "Disambig", prose(13, 300), "CC", meta={"disambiguation": True}))
    out = mj.index_documents(ctx_for(config, db, "memory.index", {"docs": store}))
    assert out.done and out.result["indexed"] == 3 and out.result["near_duplicates"] == 1
    assert db.scalar("SELECT near_dup_of FROM documents WHERE external_id='u'") == 1
    g = KnowledgeGraph(db)
    atom = g.by_wiki_title("Atom")
    assert atom and atom.key == "wiki:Atom" and atom.doc_id == 1 and atom.description == "Basic unit"
    assert g.by_wiki_title("Disambig") is None
    assert {r["name"] for r in db.query("SELECT name FROM topics")} == {"Physics", "Humans"}
    assert TextIndex(db, store).search("atom photon")
    assert db.scalar("SELECT COUNT(*) FROM documents WHERE state='new'") == 0


def test_graph_job_from_wikidata_and_redirects(config, db):
    now = time.time()
    rows = [
        ("P36", "capital", None, [], None, {"_datatype": ["wikibase-item"]}),
        ("Q142", "France", "country in Europe", ["République française"], "France",
         {"P36": ["Q90"], "P1082": [{"amount": 68e6, "unit": None}], "P31": ["Q6256"]}),
        ("Q90", "Paris", "capital of France", ["City of Light"], "Paris", {"P17": ["Q142"]}),
    ]
    for qid, label, desc, aliases, enwiki, claims in rows:
        db.execute("INSERT INTO wd_entities(qid,label,description,aliases,enwiki,claims,state,fetched) "
                   "VALUES(?,?,?,?,?,?,'new',?)", (qid, label, desc, json.dumps(aliases), enwiki, json.dumps(claims), now))
    store = DocumentStore(db)
    store.add(Document("wikipedia", "en:5", "Paris", prose(20, 300), "CC BY-SA 4.0"))
    mj.index_documents(ctx_for(config, db, "memory.index", {"docs": store}))
    db.execute("INSERT INTO wiki_redirects(lang,title,target) VALUES('en','Paris, France','Paris')")
    out = mj.build_graph(ctx_for(config, db, "memory.graph", {}))
    assert out.done and out.result["entities"] == 3 and out.result["triples"] == 4
    assert out.result["redirect_aliases"] == 1
    g = KnowledgeGraph(db)
    paris = g.by_key("Q90")
    assert paris and paris.doc_id == 1 and g.by_key("wiki:Paris") is None  # placeholder merged
    assert g.by_key("Q6256").kind == "stub"  # type: ignore[union-attr]
    assert {c[0] for c in g.candidates("city of light")} == {paris.id}
    assert {c[0] for c in g.candidates("paris france")} == {paris.id}
    assert db.scalar("SELECT label FROM predicates WHERE key='P36'") == "capital"
    assert {c[1] for c in g.candidates("capital")} == {"capital"}
    topics = mj.maintain_topics(ctx_for(config, db, "memory.topics", {}))
    assert topics.done


def test_memory_planner_and_registration(config, db):
    comps = build_components(config, db)
    assert {"memory.index", "memory.graph", "memory.topics"} <= set(comps.registry.kinds())
    agent = Agent(config, db, comps.registry, services=comps.services, notifier=Notifier({}))
    DocumentStore(db).add(Document("web", "1", "t", prose(1), "x"))
    db.execute("INSERT INTO wd_entities(qid,label,claims,state,fetched) VALUES('Q1','x','{}','new',0)")
    mj.planner(agent)
    assert {r["kind"] for r in db.query("SELECT kind FROM jobs")} == {"memory.index", "memory.graph", "memory.topics"}


def test_query_planning_prunes_common_and_rejects_vague(db):
    store = DocumentStore(db)
    ti = TextIndex(db, store)
    for i in range(300):
        text = f"the common word appears here in passage {i}. " + ("rare zebra striped " if i == 7 else "")
        did, _ = store.add(Document("web", str(i), f"T{i}", text * 3, "x"))
        ti.index(did, f"T{i}", text * 3)
    terms, common = ti.plan("the zebra and the passage")
    assert "zebra" in terms and "the" not in terms and not common
    assert ti.search("zebra common")[0].title == "T7"
    assert ti.plan("the common word") == ([], True)  # nothing specific to search for
    assert ti.search("the common word") == [] and ti.plan("unseenword") == ([], False)
    assert ti.plan("") == ([], False)
