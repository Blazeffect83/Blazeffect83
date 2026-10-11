"""The knowledge map, the face as data, and the dashboard's new panels and live page."""

from __future__ import annotations

import http.client
import json
import threading
import time
from typing import Any

import pytest

from polymath.core.db import open_database
from polymath.interface import face
from polymath.interface import knowledge_map as km
from polymath.interface.dashboard import make_server
from polymath.memory.documents import Document, DocumentStore
from tests.conftest import make_config
from tests.fixtures.kb import build


def topics(db, spec: dict[str, int], shared: list[tuple[str, str, int]], when: float | None = None) -> dict[str, int]:
    """Topics with that many documents each, plus documents shared between pairs (co-occurrence)."""
    store = DocumentStore(db)
    ids = {}
    for name in spec:
        ids[name] = int(db.execute("INSERT INTO topics(name, kind) VALUES(?, 'category')", (name,)).lastrowid or 0)
    k = 0

    def doc(names: list[str], at: float | None) -> None:
        nonlocal k
        k += 1
        did, _ = store.add(Document("web", f"u{k}", f"Doc {k}", f"body number {k} " * 12, "CC0"))
        if at is not None:
            db.execute("UPDATE documents SET fetched = ? WHERE id = ?", (at, did))
        for n in names:
            db.execute("INSERT INTO doc_topics(doc_id, topic_id, weight) VALUES(?,?,1)", (did, ids[n]))

    for name, n in spec.items():
        for i in range(n):
            doc([name], None if when is None else when + i * 3600)
    for a, b, n in shared:
        for _ in range(n):
            doc([a, b], None)
    return ids


def test_map_nodes_edges_clusters_layout_and_history(db):
    now = time.time()
    ids = topics(db, {"Stars": 10, "Planets": 8, "Galaxies": 6, "Poetry": 9, "Novels": 7, "Lonely": 3},
                 [("Stars", "Planets", 5), ("Stars", "Galaxies", 4), ("Poetry", "Novels", 5)],
                 when=now - 3 * 86400)  # fmt: skip
    m = km.build_map(db)
    by = {n["name"]: n for n in m["nodes"]}
    assert by["Stars"]["docs"] == 19 and all(0.0 <= n["x"] <= 1.0 and 0.0 <= n["y"] <= 1.0 for n in m["nodes"])
    assert by["Stars"]["group"] == by["Planets"]["group"] == by["Galaxies"]["group"]  # one cluster
    assert by["Poetry"]["group"] == by["Novels"]["group"] != by["Stars"]["group"]
    pairs = {(e["a"], e["b"]) for e in m["edges"]}
    assert (ids["Stars"], ids["Planets"]) in pairs and all(ids["Lonely"] not in p for p in pairs)
    d = lambda a, b: ((by[a]["x"] - by[b]["x"]) ** 2 + (by[a]["y"] - by[b]["y"]) ** 2) ** 0.5  # noqa: E731
    assert d("Stars", "Planets") < d("Stars", "Novels")  # linked topics sit closer together
    # the history was rebuilt from read times: it grows to today's size
    series = m["history"][str(ids["Stars"])]
    assert len(m["times"]) == len(series) >= 2 and series[0] < series[-1] == 19
    assert series == sorted(series)
    # stable: a second build starts from the stored layout and keeps topics where they were
    again = {n["name"]: n for n in km.build_map(db)["nodes"]}
    assert abs(again["Stars"]["x"] - by["Stars"]["x"]) < 0.2 and abs(again["Novels"]["y"] - by["Novels"]["y"]) < 0.2


def test_snapshots_keep_history_after_eviction(db):
    now = time.time()
    ids = topics(db, {"Chemistry": 6}, [], when=now - 5 * 86400)
    db.execute("DELETE FROM documents WHERE id IN (SELECT doc_id FROM doc_topics LIMIT 3)")  # evicted later
    db.execute("DELETE FROM doc_topics WHERE doc_id NOT IN (SELECT id FROM documents)")
    db.execute("INSERT INTO topic_snapshots(day, topic_id, docs, facts) VALUES(?, ?, 6, 0)",
               (km.day_of(now - 2 * 86400), ids["Chemistry"]))  # fmt: skip
    m = km.build_map(db)
    assert max(m["history"][str(ids["Chemistry"])]) == 6  # the recorded size, not just what is left


def test_snapshot_job_planner_and_empty(db, config):
    assert km.build_map(db) == {"nodes": [], "edges": [], "times": [], "history": {}}
    assert km.layout([], []) == {} and km.history(db, []) == ([], {})
    topics(db, {"Maths": 4}, [])
    out = km.snapshot_job(type("Ctx", (), {"db": db})())
    assert out.result["topics"] == 1 and db.scalar("SELECT docs FROM topic_snapshots") == 4
    from polymath.core.scheduler import Scheduler

    agent = type("A", (), {"db": db, "scheduler": Scheduler(db)})()
    km.planner(agent)
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'memory.snapshot'") == 1


def test_adaptive_steps(db):
    now = time.time()
    ids = topics(db, {"Old": 3}, [], when=now - 200 * 86400)
    times, hist = km.history(db, [ids["Old"]])
    assert len(times) <= km.FRAMES and times[-1] >= now - 1 and hist[ids["Old"]][-1] == 3
    assert times[1] - times[0] >= 2 * 86400  # long histories step by days


def test_face_definitions_and_state():
    d = face.definitions()
    assert d["moods"]["reading"]["frames"] == ["◐‿◐", "◐‿◐", "◑‿◑", "◑‿◑"] and d["frame_s"] == face.FRAME_S
    st = {"online": True, "state": "running", "mode": "normal", "action": "reason.infer"}
    assert face.state(st, [], now=1000.0) == {"mood": "thinking", "reaction": None}
    s2 = face.state(st, [{"kind": "note", "what": "fixed"}, {"kind": "inferred"}], now=1000.0)
    assert s2["mood"] == "thinking" and s2["reaction"] == {"mood": "proud", "seconds": 8.0, "priority": 3}


@pytest.fixture()
def server(tmp_path):
    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=6)
        db.kv_set("heartbeat", {"ts": time.time(), "state": "running", "cycle": 3, "version": "v0", "build": "zz"})
        db.execute("INSERT INTO events(at, kind, text) VALUES(?, 'didyouknow', 'Did you know? Odd fact.')",
                   (time.time(),))  # fmt: skip
        db.execute("INSERT INTO disk_writes VALUES(?, 'mmcblk0p2', 'data', 'sd', ?, ?, 0)",
                   (time.time() - 60, 64 * 10**9, 10**9))  # fmt: skip
    db.close()
    srv = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv, cfg
    srv.shutdown()
    srv.server_close()


def fetch(srv, path: str) -> tuple[int, bytes, str]:
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body, r.getheader("Content-Type") or ""


def get(srv, path: str) -> Any:
    status, body, _ = fetch(srv, path)
    assert status == 200, (path, status, body[:200])
    return json.loads(body)


def test_dashboard_endpoints(server):
    srv, cfg = server
    ins = get(srv, "/api/insights")
    assert ins["recap"]["week"].endswith("(so far)") and ins["recap"]["lines"][0].startswith("Read ")
    assert ins["predictions"]["made"] == 0 and "Predicted 0" in ins["predictions"]["line"]
    assert ins["didyouknow"] == ["Did you know? Odd fact."] and ins["home"] is None
    assert ins["wear"][0]["line"].startswith("SD card (data disk)")
    assert ins["changes"] == [] and ins["tuned"] == [] and ins["reading_weights"] == {}
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        from polymath.drive import changelog, selftune

        changelog.record(db, "tuning", "link.features", "adopted", "Changed clues from 40 to 20", state="watching")
        selftune.set_value(db, "link.features", 20)
        db.kv_set("reading_weights", {"wikipedia": 2.0, "pubmed": 1.04, "feed": 0.5})
    db.close()
    ins = get(srv, "/api/insights")
    assert ins["changes"][0]["watching"] and ins["changes"][0]["summary"] == "Changed clues from 40 to 20"
    assert ins["tuned"] == [{"name": "link.features", "label": selftune.KNOBS["link.features"].label, "value": 20,
                             "default": 40}]  # fmt: skip
    assert ins["reading_weights"] == {"wikipedia": 2.0, "feed": 0.5}  # pubmed ×1.04 is no real change
    (cfg.paths.data_dir / "home-drive.json").write_text(json.dumps({"id": "u1", "name": "T7", "budget_gb": 900}))
    assert get(srv, "/api/insights")["home"]["name"] == "T7"
    m = get(srv, "/api/map")
    assert m["nodes"] and {"x", "y", "group", "docs"} <= set(m["nodes"][0])
    assert get(srv, "/api/map") == m  # cached
    assert get(srv, "/api/face")["moods"]["idle"]["trail"] == ["z", "zZ", "zZz", "zZ"]
    tell = get(srv, "/api/tell?q=Country02")
    assert tell["subject"] == "Country02" and "Its capital is Capitol02." in tell["paragraph"]
    assert get(srv, "/api/tell?q=")["paragraph"] == ""
    feed = get(srv, "/api/feed?render=1")
    assert feed["face"]["mood"] in face.MOODS and feed["lines"] and isinstance(feed["lines"][0][0], list)
    assert feed["badge"]["current"] is False and "agent still on v0" in feed["badge"]["text"]
    plain = get(srv, "/api/feed")
    assert "lines" not in plain and plain["face"]
    for page, marker in (("/live", b"live-feed"), ("/static/live.js", b"PolyFace"), ("/static/face.js", b"PolyFace")):
        status, body, _ctype = fetch(srv, page)
        assert status == 200 and marker in body, page
    status, body, _ = fetch(srv, "/")
    assert b"c-map" in body and b"face.js" in body and b"/live" in body and b'id="changes"' in body


def test_dashboard_insights_with_a_written_recap(server):
    srv, cfg = server
    from polymath.evaluation import recap

    db = open_database(cfg.paths.db_path)
    with db.transaction():
        recap.write_recap(db, cfg=cfg)
    db.close()
    ins = get(srv, "/api/insights")
    assert not ins["recap"]["week"].endswith("(so far)")
