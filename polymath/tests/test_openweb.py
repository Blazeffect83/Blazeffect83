"""Vetted open-web learning, the daily digest, and learning from mistakes."""

from __future__ import annotations

import io
import json
import tarfile
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.evaluation import digest, remedy
from polymath.interface import cli
from polymath.interface.feed import Feed, render_event
from polymath.memory.documents import Document, DocumentStore
from polymath.memory.graph import KnowledgeGraph
from polymath.senses import openweb as ow
from polymath.senses.crawler import Crawler
from polymath.senses.net import BlockedURL, FetchError, Response
from tests.fixtures.kb import build

WIKITEXT = """'''Mars''' is a planet.<ref>{{cite web|url=https://www.nasa.gov/mars/facts.html|title=Mars facts}}</ref>
More at [https://science.nasa.gov/mars/ NASA science] and [http://www.esa.int/Mars ESA].
Archived: https://web.archive.org/web/2020/https://nasa.gov/x  {{doi|10.1/x}} https://doi.org/10.1/x
https://www.nasa.gov/a https://www.nasa.gov/b https://www.nasa.gov/c https://www.nasa.gov/d https://www.nasa.gov/e."""


def text_of(segs: list[tuple[str, str]]) -> str:
    return "".join(t for t, _ in segs)


def ctx_for(config: Any, db: Any, kind: str, payload: dict[str, Any] | None = None, services: dict[str, Any] | None = None,
            checkpoint: dict[str, Any] | None = None, budget: float = 30.0) -> JobContext:  # fmt: skip
    s = Scheduler(db)
    jid, _ = s.enqueue(kind, payload or {}, key=f"{kind}:{time.time_ns()}")
    return JobContext(config=config, db=db, scheduler=s,
                      job=Job(jid, kind, f"k{jid}", payload or {}, checkpoint, 0, 0, 0, 5, 0),
                      deadline=time.monotonic() + budget, stop_event=threading.Event(), services=services or {},
                      on_tick=lambda: None)  # fmt: skip


class FakeHTTP:
    """Answers robots.txt requests like the real client would, per host."""

    user_agent = "PolymathBot/0.1"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def get(self, url: str, headers: Any = None, *, max_bytes: int = 0, ok: Any = (200,)) -> Response:
        self.calls.append(url)
        a = self.answers[url.split("/")[2]]
        if isinstance(a, Exception):
            raise a
        status, body, final = a if len(a) == 3 else (*a, url)
        return Response(final, status, {"content-type": "text/plain"}, body.encode())


def load_lists(db: Any, config: Any, tmp_path: Any, lists: dict[int, str]) -> None:
    """Load safety lists from local files through the real loader job."""
    config.senses.blocklists = [f"https://lists.example/{i}" for i in range(len(lists))]
    db.kv_set("blocklists", [f"list {i}" for i in range(len(lists))])
    for i, content in lists.items():
        dest = f"blocklists/{i}"
        path = config.paths.raw_dir / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        ow.load_blocklist_job(ctx_for(config, db, "web.blocklist", {"index": i, "url": f"https://lists.example/{i}",
                                                                    "dest": dest}))  # fmt: skip


# ------------------------------------------------------------------ citations and safety lists
def test_citations_count_sites_once_per_article_and_skip_archives(db):
    assert ow.record_citations(db, WIKITEXT) == 2  # nasa.gov and esa.int; archives and doi resolvers skipped
    ow.record_citations(db, WIKITEXT)
    cites = {r["site"]: r["citations"] for r in db.query("SELECT site, citations FROM site_citations")}
    assert cites == {"nasa.gov": 2, "esa.int": 2}
    urls = [r["url"] for r in db.query("SELECT url FROM site_urls WHERE site='nasa.gov'")]
    assert len(urls) == ow.URLS_PER_SITE and "https://www.nasa.gov/mars/facts.html" in urls


def test_safety_lists_block_domains_and_their_subdomains(db, config, tmp_path):
    hosts = "# comment\n0.0.0.0 badsite.example\n127.0.0.1 localhost\n0.0.0.0 tracker.ads.test  # inline\n"
    plain = "phish.example\n\n192.168.1.1\nnot a domain\n"
    load_lists(db, config, tmp_path, {0: hosts, 1: plain})
    assert ow.blocklists_ready(db, config)
    assert ow.blocked(db, "badsite.example") == "list 0"
    assert ow.blocked(db, "www.cdn.badsite.example:443") == "list 0"  # a listed parent blocks its subdomains
    assert ow.blocked(db, "phish.example") == "list 1"
    assert ow.blocked(db, "goodsite.example") is None and ow.blocked(db, "localhost") is None
    texts = [r["text"] for r in db.query("SELECT text FROM events ORDER BY id")]
    assert texts[0] == "safety list loaded: list 0 (2 domains)" and "open-web learning can start" in texts[-1]
    # a reload replaces the list: entries that left it stop blocking
    load_lists(db, config, tmp_path, {0: "0.0.0.0 other.example\n"})
    assert ow.blocked(db, "badsite.example") is None and ow.blocked(db, "other.example") == "list 0"


def test_ut1_archives_are_unpacked_and_loaded_in_slices(db, config):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("adult/domains", b"a.example\nb.example\n" + b"".join(b"x%d.example\n" % i for i in range(12000))),
                           ("adult/urls", b"a.example/page\n"), ("adult/usage", b"black\n")):  # fmt: skip
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    config.senses.blocklists = ["https://dsi.ut-capitole.fr/blacklists/download/adult.tar.gz"]
    ow.plan_blocklists_job(ctx_for(config, db, "web.blocklists"))
    job = db.one("SELECT payload FROM jobs WHERE kind='dump.download'")
    p = json.loads(job["payload"])
    assert p["then"]["kind"] == "web.blocklist" and json.loads(json.dumps(db.kv_get("blocklists"))) == ["UT1 adult"]
    path = config.paths.raw_dir / p["dest"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    ctx = ctx_for(config, db, "web.blocklist", p["then"]["payload"], budget=-1)  # out of time at once: slices
    out = ow.load_blocklist_job(ctx)
    cp = out.checkpoint or {}
    assert not out.done and cp["extracted"] and cp["count"] == 5000
    for _ in range(10):  # bounded: a loader that never finishes fails the test instead of hanging it
        out = ow.load_blocklist_job(
            ctx_for(config, db, "web.blocklist", p["then"]["payload"], checkpoint=out.checkpoint)
        )
        if out.done:
            break
    assert out.result["domains"] == 12002 and ow.blocked(db, "x11999.example") == "UT1 adult"
    assert not path.exists()  # downloads are deleted once loaded


@pytest.mark.parametrize(
    ("host", "bad"),
    [("pornhub.example", True), ("best-casino.example", True), ("sex.example", True), ("bet365.example", True),
     ("middlesex.ac.uk", False), ("essex.gov.uk", False), ("alphabet.example", False), ("nasa.gov", False)],
)  # fmt: skip
def test_unsafe_names(host, bad):
    assert ow.bad_name(host) is bad


# ------------------------------------------------------------------ the gate
def gate(db: Any, config: Any, tmp_path: Any, answers: dict[str, Any]) -> tuple[FakeHTTP, Crawler]:
    load_lists(db, config, tmp_path, {0: "0.0.0.0 badsite.example\n"})
    http = FakeHTTP(answers)
    crawler = Crawler(db, http, DocumentStore(db), user_agent="PolymathBot/0.1", allow_domains=[])  # type: ignore[arg-type]
    return http, crawler


def cite(db: Any, site: str, n: int, url: str) -> None:
    db.execute("INSERT INTO site_citations(site, citations, first_seen, last_seen) VALUES(?,?,0,0)", (site, n))
    db.execute("INSERT INTO site_urls(site, url) VALUES(?,?)", (site, url))


def test_gate_fails_closed_until_the_safety_lists_are_loaded(db, config):
    cite(db, "nasa.gov", 100, "https://www.nasa.gov/mars")
    config.senses.blocklists = ["https://lists.example/0"]
    out = ow.vet_job(ctx_for(config, db, "web.vet"))
    assert out.result == {"skipped": "waiting for the safety lists"} and not db.scalar("SELECT COUNT(*) FROM sites")
    config.senses.open_web = False
    assert ow.vet_job(ctx_for(config, db, "web.vet")).result == {"skipped": "senses.open_web is off"}


def test_gate_verdicts(db, config, tmp_path):
    http, crawler = gate(db, config, tmp_path, {
        "www.nasa.gov": (404, ""),                                         # no robots.txt: everything allowed
        "www.smallblog.example": (200, "User-agent: *\nDisallow: /private\n"),
        "selfsigned.example": FetchError("SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED]", transient=True),
        "lan.example": BlockedURL("private address 192.168.1.5"),
        "down.example": FetchError("ConnectionRefusedError: refused", transient=True),
        "closed.example": (200, "User-agent: *\nDisallow: /\n"),
        "hop.example": (200, "", "https://elsewhere.example/robots.txt"),
    })  # fmt: skip
    cite(db, "nasa.gov", 120, "http://www.nasa.gov/mars")  # http is upgraded to https
    cite(db, "smallblog.example", 5, "https://www.smallblog.example/post")
    cite(db, "badsite.example", 99, "https://badsite.example/x")
    cite(db, "camgirls-live.example", 40, "https://camgirls-live.example/")
    cite(db, "selfsigned.example", 30, "https://selfsigned.example/a")
    cite(db, "lan.example", 30, "https://lan.example/a")
    cite(db, "down.example", 30, "https://down.example/a")
    cite(db, "closed.example", 30, "https://closed.example/a")
    cite(db, "hop.example", 30, "https://hop.example/a")
    cite(db, "rare.example", 2, "https://rare.example/a")  # below open_web_min_citations
    out = ow.vet_job(ctx_for(config, db, "web.vet", services={"http": http, "crawler": crawler}))
    v = {r["site"]: (r["status"], r["reason"]) for r in db.query("SELECT site, status, reason FROM sites")}
    assert v["nasa.gov"][0] == "approved" and "cited by 120" in v["nasa.gov"][1]
    assert v["smallblog.example"][0] == "probation"
    assert v["badsite.example"] == ("refused", "on the safety list list 0")
    assert v["camgirls-live.example"] == ("refused", "its name suggests adult or gambling content")
    assert v["selfsigned.example"] == ("refused", "no valid HTTPS certificate")
    assert v["lan.example"][0] == "refused" and "public address" in v["lan.example"][1]
    assert v["down.example"][0] == "retry"
    assert v["closed.example"] == ("refused", "its robots.txt asks crawlers to stay away")
    assert v["hop.example"][0] == "refused" and "redirects away" in v["hop.example"][1]
    assert "rare.example" not in v and out.result["approved"] == 1
    assert not any("badsite" in c or "camgirls" in c for c in http.calls)  # refused offline, never contacted
    queued = {r["url"]: r["depth"] for r in db.query("SELECT url, depth FROM frontier")}
    assert queued == {"https://www.nasa.gov/mars": 2, "https://www.smallblog.example/post": 2}
    assert crawler.permitted("www.nasa.gov") and crawler.permitted("smallblog.example")
    assert not crawler.permitted("selfsigned.example") and not crawler.permitted("badsite.example")
    notes = [text_of(render_event(e)) for e in Feed(db).poll("e0")["events"] if e["kind"] == "note"]
    assert any("site     nasa.gov: approved" in n for n in notes) and any(
        "no valid HTTPS certificate" in n for n in notes
    )
    # a site waiting for a retry comes back once its time is up
    db.execute("UPDATE sites SET retry_at = 0 WHERE site='down.example'")
    http.answers["down.example"] = (404, "")
    ow.vet_job(ctx_for(config, db, "web.vet", services={"http": http, "crawler": crawler}))
    assert db.scalar("SELECT status FROM sites WHERE site='down.example'") == "probation"


def test_daily_cap_on_new_sites(db, config, tmp_path):
    http, crawler = gate(db, config, tmp_path, {f"s{i}.example": (404, "") for i in range(5)})
    for i in range(5):
        cite(db, f"s{i}.example", 10, f"https://s{i}.example/")
    config.senses.open_web_sites_per_day = 3
    ow.vet_job(ctx_for(config, db, "web.vet", services={"http": http, "crawler": crawler}))
    assert db.scalar("SELECT COUNT(*) FROM sites") == 3
    ow.vet_job(ctx_for(config, db, "web.vet", services={"http": http, "crawler": crawler}))
    assert db.scalar("SELECT COUNT(*) FROM sites") == 3


def test_probation_quota_limits_links_followed(db, config):
    crawler = Crawler(db, None, DocumentStore(db), user_agent="x", allow_domains=["ref.example"], probation_pages=2)  # type: ignore[arg-type]
    db.execute("INSERT INTO sites(site, status, vetted_at) VALUES('trial.example', 'probation', 0)")
    db.execute("INSERT INTO sites(site, status, vetted_at) VALUES('good.example', 'approved', 0)")
    crawler.refresh_vetted()
    assert crawler.followable("ref.example") and crawler.followable("www.good.example")
    assert crawler.followable("trial.example")
    crawler.frontier.add("https://trial.example/1")
    crawler.frontier.add("https://www.trial.example/2")
    assert not crawler.followable("trial.example")  # quota reached until its facts are judged
    assert not crawler.followable("random.example")


def test_probation_sites_are_judged_by_their_facts(db, config):
    kb = build(db, n=24)  # enough subjects to tell that "capital" is single-valued
    g = KnowledgeGraph(db)
    store = DocumentStore(db)
    for site in ("liar.example", "honest.example"):
        db.execute("INSERT INTO sites(site, status, vetted_at) VALUES(?, 'probation', 0)", (site,))
    liar, _ = store.add(Document("web", "l1", "Liar page", "x " * 300, "u", meta={"host": "www.liar.example"}))
    honest, _ = store.add(Document("web", "h1", "Honest page", "y " * 300, "u", meta={"host": "honest.example"}))
    db.execute("INSERT INTO frontier(url, host, priority, depth, added) VALUES('https://www.liar.example/2', "
               "'www.liar.example', 0, 1, 0)")  # fmt: skip
    for i in range(12):  # capitals: the liar names the wrong city, the honest site the right one
        g.add_triple(kb.countries[i], kb.preds["capital"], o=kb.cities[(i + 1) % 24], kind="pattern", source="text",
                     doc_id=liar)  # fmt: skip
        g.add_triple(kb.countries[i], kb.preds["capital"], o=kb.cities[i], kind="pattern", source="text",
                     doc_id=honest)  # fmt: skip
    out = ow.trust_job(ctx_for(config, db, "web.trust"))
    st = {r["site"]: (r["status"], r["checked"]) for r in db.query("SELECT site, status, checked FROM sites")}
    assert st["liar.example"] == ("dropped", 12) and st["honest.example"] == ("approved", 12)
    assert out.result == {"approved": 1, "dropped": 1, "waiting": 0}
    assert db.scalar("SELECT state FROM frontier WHERE host='www.liar.example'") == "skipped"


# ------------------------------------------------------------------ learning from mistakes
class Predictor:
    def __init__(self, picks: list[int]) -> None:
        self.picks = picks

    def predict(self, s: int, p: int, options: list[int]) -> Any:
        return SimpleNamespace(best=self.picks.pop(0), confidence=0.6, method="text")


def wrong_answer(db: Any, kb: Any, i: int) -> int:
    t = db.one("SELECT id FROM triples WHERE s=? AND p=?", (kb.countries[i], kb.preds["capital"]))
    qid = db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, 1, 0, 0, .25, '{}')",
                     (time.time(),)).lastrowid  # fmt: skip
    opts = json.dumps([{"entity": kb.cities[i], "label": "a"}, {"entity": kb.cities[i + 1], "label": "b"}])
    db.execute("INSERT INTO quiz_answers(quiz_id, triple_id, question, options, answer, chosen, correct, method, "
               "confidence) VALUES(?,?,?,?,?,?,0,'none',0.3)",
               (qid, t["id"], f"What is the capital of Country{i:02d}?", opts, kb.cities[i], kb.cities[i + 1]))  # fmt: skip
    return int(t["id"])


def test_mistakes_are_read_about_retested_and_fixed_or_given_up(db, config):
    kb = build(db, n=8)
    wrong_answer(db, kb, 1)
    wrong_answer(db, kb, 2)
    config.learning.relearn_delay_hours = 0  # re-test immediately in the test
    out = remedy.remedy_job(ctx_for(config, db, "eval.remedy"), predictor=Predictor([]))
    assert out.result["new"] == 2 and out.result["reading"] == 2
    reads = [
        json.loads(r["payload"])["titles"] for r in db.query("SELECT payload FROM jobs WHERE kind='wikipedia.titles'")
    ]
    assert ["Country01", "Capitol01"] in reads  # the subject and the true answer
    # re-test: the first now right, the second still wrong → reads more (now also the wrong choice)
    out = remedy.remedy_job(ctx_for(config, db, "eval.remedy"), predictor=Predictor([kb.cities[1], kb.cities[3]]))
    assert out.result["fixed"] == 1 and out.result["retry"] == 1
    reads = [
        json.loads(r["payload"])["titles"] for r in db.query("SELECT payload FROM jobs WHERE kind='wikipedia.titles'")
    ]
    assert ["Country02", "Capitol02", "Capitol03"] in reads
    for _ in range(2):
        remedy.remedy_job(ctx_for(config, db, "eval.remedy"), predictor=Predictor([kb.cities[3]]))
    states = {r["question"]: r["state"] for r in db.query("SELECT question, state FROM remediation")}
    assert states == {"What is the capital of Country01?": "fixed", "What is the capital of Country02?": "still_wrong"}
    notes = [text_of(render_event(e)) for e in Feed(db).poll("e0")["events"] if e["kind"] == "note"]
    assert any("fixed ✓  learned it: What is the capital of Country01? → Capitol01" in n for n in notes)
    assert any("still wrong after 3 tries" in n for n in notes)
    assert remedy.stats(db, 0)["fixed"] == 1 and remedy.collect(db) == 0  # nothing new to collect


def test_agent_quiz_mistakes_are_collected_too(db):
    from polymath.agents import society, store

    kb = build(db, n=6)
    a = society.spawn(db, "verify capitals", name="caps")
    t = db.one("SELECT id FROM triples WHERE s=? AND p=?", (kb.countries[2], kb.preds["capital"]))
    ag = store.get(db, a.id)
    assert ag is not None
    tid = store.open_task(db, ag, kind="quiz", action="quiz", target=f"quiz:{t['id']}",
                          payload={"question": "Q?", "options": [{"entity": kb.cities[2], "label": "x"},
                                                                  {"entity": kb.cities[3], "label": "y"}],
                                   "chosen": kb.cities[3]})  # fmt: skip
    assert tid is not None
    store.reward(db, tid, -0.4, "hidden fact differs", correct=False)
    assert remedy.collect(db) == 1
    assert db.one("SELECT origin, answer FROM remediation")["answer"] == kb.cities[2]


# ------------------------------------------------------------------ the digest
def test_digest_summarises_the_day(db, config):
    kb = build(db, n=10)
    now = time.time()
    db.execute("INSERT INTO quizzes(created, n, correct, accuracy, chance, details) VALUES(?, 10, 6, .6, .25, '{}')",
               (now - 3 * 86400,))  # fmt: skip
    wrong_answer(db, kb, 1)
    db.execute("UPDATE quizzes SET n = 10, correct = 8 WHERE id = (SELECT MAX(id) FROM quizzes)")
    db.execute("INSERT INTO sites(site, status, reason, citations, vetted_at) VALUES('nasa.gov', 'approved', 'ok', 120, ?)",
               (now,))  # fmt: skip
    db.execute("INSERT INTO reasoning_runs(kind, started, ended, result) VALUES('contradictions', ?, ?, ?)",
               (now, now, json.dumps({"settled": 3, "disputed": 1})))  # fmt: skip
    d = digest.write_digest(db, now=now)
    text = "\n".join(digest.lines(d))
    assert "Read 15 documents (15 wikipedia)." in text
    assert "Self-test accuracy: 80% (up 20 points from the 7 days before)." in text
    assert "Got 1 self-test questions wrong" in text and "New sites vetted: 1 approved." in text
    assert "Checked conflicting claims: 3 settled, 1 still disputed." in text
    assert "New: " in text  # notable new facts
    md = digest.to_markdown(d, d["day"])
    assert md.startswith(f"# What I learned — {d['day']}\n\n- Read 15 documents")
    assert digest.latest(db)["lines"] == digest.lines(d)  # type: ignore[index]
    feed = [text_of(render_event(e)) for e in Feed(db).poll("x0")["events"] if e["kind"] == "digest"]
    assert f"what I learned — {d['day']}" in feed[0] and feed[1].endswith("· Read 15 documents (15 wikipedia).")


def test_cli_digest_sites_and_dashboard(tmp_path, capsys):
    from tests.conftest import make_config

    make_config(tmp_path)
    path = str(tmp_path / "polymath.toml")
    assert cli.main(["--config", path, "init"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", path, "digest"]) == 0
    assert "(so far)" in capsys.readouterr().out
    assert cli.main(["--config", path, "digest", "--write"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# What I learned") and "Read 0 documents." in out
    assert cli.main(["--config", path, "digest", "--json"]) == 0
    assert "documents" in json.loads(capsys.readouterr().out)
    assert cli.main(["--config", path, "sites"]) == 0
    out = capsys.readouterr().out
    assert "safety lists: not loaded yet" in out and "none vetted yet" in out
    assert cli.main(["--config", path, "sites", "--check", "https://www.pornhub.example/x"]) == 0
    chk = json.loads(capsys.readouterr().out)
    assert chk["site"] == "pornhub.example" and chk["name_looks_unsafe"] and not chk["safety_lists_loaded"]
    assert cli.main(["--config", path, "sites", "--json", "--status", "approved"]) == 0
    assert json.loads(capsys.readouterr().out)["sites"] == []


def test_dashboard_digest_panel(tmp_path):
    import http.client

    from polymath.core.db import open_database
    from polymath.interface.dashboard import make_server
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    db = open_database(cfg.paths.db_path)
    with db.transaction():
        build(db, n=4)
        db.execute("INSERT INTO sites(site, status, reason, citations, vetted_at) VALUES('nasa.gov', 'approved', "
                   "'cited by 500 Wikipedia articles', 500, ?)", (time.time(),))  # fmt: skip
    db.close()
    server = make_server(cfg, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:

        def get(path: str) -> Any:
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            c.request("GET", path)
            body = c.getresponse().read()
            c.close()
            return json.loads(body)

        d = get("/api/digest")
        assert d["day"].startswith("last 24 hours") and d["lines"][0] == "Read 9 documents (9 wikipedia)."
        assert d["sites"][0]["site"] == "nasa.gov"
        db = open_database(cfg.paths.db_path)
        with db.transaction():
            digest.write_digest(db)
        db.close()
        assert get("/api/digest")["day"] == time.strftime("%Y-%m-%d")
    finally:
        server.shutdown()
        server.server_close()
