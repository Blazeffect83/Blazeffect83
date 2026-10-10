"""Phase 2 (web side): HTTP client safety, resumable downloads, robots, HTML extraction, crawler, feeds."""

from __future__ import annotations

import itertools
import json
import os
import socket
import threading
import time

import pytest

from polymath.core.jobs import JobContext
from polymath.core.scheduler import Job, Scheduler
from polymath.memory.documents import Document, DocumentStore, decode_body, encode_body
from polymath.senses import download as dl
from polymath.senses.crawler import Crawler, TokenBucket, crawl_job, domain_allowed, normalize_url
from polymath.senses.feeds import feeds_job, parse_feed, poll_feed
from polymath.senses.html_text import classify, detect_license, extract
from polymath.senses.net import BlockedURL, FetchError, HttpClient, TooLarge, _host_in_no_proxy, is_public_address
from polymath.senses.robots import parse_robots, robots_for_status
from polymath.senses.safexml import UnsafeXML, parse_untrusted_xml
from tests.webserver import FakeWeb, client


@pytest.fixture()
def web():
    w = FakeWeb()
    yield w
    w.close()


def make_ctx(config, db, kind="x", payload=None, services=None, budget=10.0, checkpoint=None):
    s = Scheduler(db)
    jid, _ = s.enqueue(kind, payload or {}, key=f"{kind}:{time.time_ns()}")
    job = Job(
        id=jid,
        kind=kind,
        key=f"k{jid}",
        payload=payload or {},
        checkpoint=checkpoint,
        priority=0,
        attempts=0,
        crashes=0,
        max_attempts=5,
        slices=0,
    )
    return JobContext(
        config=config,
        db=db,
        scheduler=s,
        job=job,
        deadline=time.monotonic() + budget,
        stop_event=threading.Event(),
        services=services or {},
    )


# ---------------------------------------------------------------------- net


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/x",
        "file:///etc/passwd",
        "gopher://x",
        "http://user:pw@example.org/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        "http://printer.local/",
        "http:///nohost",
        "http://example.org:99999/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
    ],
)
def test_unsafe_urls_rejected(url):
    c = HttpClient(env={}, resolver=lambda h, p: ["93.184.216.34"])
    with pytest.raises((BlockedURL, FetchError)), c.stream(url):
        pass


def test_dns_answer_pointing_inside_is_rejected():
    c = HttpClient(env={}, resolver=lambda h, p: ["93.184.216.34", "10.1.2.3"])
    with pytest.raises(BlockedURL, match="non-public"):
        c.get("http://rebind.example/")


def test_public_address_and_no_proxy_rules():
    assert is_public_address("93.184.216.34") and not is_public_address("::ffff:10.0.0.1")
    assert not is_public_address("224.0.0.1")
    assert _host_in_no_proxy("api.example.com", "localhost,.example.com")
    assert _host_in_no_proxy("10.2.3.4", "10.0.0.0/8") and not _host_in_no_proxy("11.2.3.4", "10.0.0.0/8, ,bad/x")
    assert _host_in_no_proxy("x.y", "*") and not _host_in_no_proxy("dumps.wikimedia.org", "wikimedia.com")
    c = HttpClient(env={"https_proxy": "http://proxy:3128", "no_proxy": "internal.org", "HTTP_PROXY": "p2"})
    assert c._proxy_for("https", "dumps.wikimedia.org") == ("proxy", 3128)
    assert c._proxy_for("https", "a.internal.org") is None
    assert c._proxy_for("http", "x.org") == ("p2", 8080)
    assert HttpClient(env={"https_proxy": "http://"})._proxy_for("https", "x.org") is None


def test_pinned_connection_redirects_size_cap_and_gzip(web):
    web.add("/a", status=302, headers={"Location": "/b"})
    web.add("/b", "<p>hello</p>" * 100, gzip=True)
    web.add("/loop", status=301, headers={"Location": "/loop"})
    web.add("/big", b"x" * 5000)
    web.add("/err", "boom", status=503)
    c = client()
    r = c.get(web.base + "/a")
    assert r.status == 200 and r.url.endswith("/b") and r.text().startswith("<p>hello")
    assert r.content_type == "text/html" and r.charset == "utf-8"
    with pytest.raises(FetchError, match="too many redirects"):
        c.get(web.base + "/loop")
    with pytest.raises(TooLarge):
        c.get(web.base + "/big", max_bytes=1000)
    with pytest.raises(FetchError) as ei:
        c.get(web.base + "/err")
    assert ei.value.transient and ei.value.status == 503
    with pytest.raises(BlockedURL):  # same server, but private addresses are refused by default
        HttpClient(env={}).get(web.base + "/b")
    rng = c.get_range(web.base + "/big", 10, 19)
    assert rng.body == b"x" * 10 and rng.status == 206
    web.add("/norange", b"abc", ranges=False)
    with pytest.raises(FetchError, match="expected 206"):
        c.get_range(web.base + "/norange", 0, 1)


def test_redirect_to_private_address_blocked(web):
    # A public-looking start that redirects to an internal address must be refused at the hop.
    c = HttpClient(env={}, resolver=lambda h, p: ["127.0.0.1"] if h == "127.0.0.1" else ["93.184.216.34"])
    with pytest.raises(BlockedURL):
        c.get(web.base + "/")


def test_unreachable_host_is_transient():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(FetchError) as ei:
        client().get(f"http://127.0.0.1:{port}/")
    assert ei.value.transient


# ----------------------------------------------------------------- download


def test_download_resume_validators_and_power_loss(tmp_path, web):
    payload = os.urandom(300_000)
    url = web.add("/dump.bin", payload, etag='"v1"', ctype="application/octet-stream")
    dest = tmp_path / "raw" / "dump.bin"
    c = client()
    st = dl.download_step(c, url, dest, limit=100_000)
    assert st.complete and st.verified == 100_000 and dest.read_bytes() == payload[:100_000]
    dl.remove_download(dest)
    # slice 1: stop early by time budget 0 → at least one chunk written
    st = dl.download_step(c, url, dest, time_budget=0.0, chunk=50_000)
    assert not st.complete and 0 < st.verified < len(payload)
    # simulate a power cut that left an unverified tail of garbage
    with open(dest.with_name(dest.name + ".part"), "ab") as fh:
        fh.write(b"GARBAGE")
    st = dl.download_step(c, url, dest, chunk=50_000)
    assert st.complete and dest.read_bytes() == payload and st.etag == '"v1"'
    assert dl.available_bytes(dest) == len(payload) and dl.readable_path(dest) == dest
    hdrs = [h for _t, _m, p, h in web.log if p == "/dump.bin"]
    assert hdrs[-1].get("range", "").startswith("bytes=") and hdrs[-1].get("if-range") == '"v1"'
    assert dl.download_step(c, url, dest).complete  # finished downloads are a no-op
    assert dl.remove_download(dest) >= len(payload)


def test_download_restarts_when_file_changes(tmp_path, web):
    url = web.add("/f", b"A" * 100_000, etag='"a"')
    dest = tmp_path / "f"
    c = client()
    dl.download_step(c, url, dest, time_budget=0.0, chunk=10_000)
    web.add("/f", b"B" * 120_000, etag='"b"')  # server-side change: If-Range fails → full 200
    st = dl.download_step(c, url, dest, chunk=10_000)
    assert st.complete and dest.read_bytes() == b"B" * 120_000


def test_download_416_and_errors(tmp_path, web):
    url = web.add("/g", b"0123456789", etag='"g"')
    dest = tmp_path / "g"
    part = dest.with_name("g.part")
    part.write_bytes(b"0123456789")
    meta = dl.DownloadState(url=url, path=str(dest), verified=10, total=10, etag='"g"')
    dl._write_meta(dest, meta)
    assert dl.download_step(client(), url, dest).complete
    with pytest.raises(FetchError):
        dl.download_step(client(), web.base + "/missing", tmp_path / "m")
    assert dl.load_state(tmp_path / "nothing") is None
    assert dl.available_bytes(tmp_path / "nothing") == 0


# ---------------------------------------------------------------- documents


def test_document_store_dedupe_update_and_codecs(db):
    st = DocumentStore(db)
    d = Document("web", "u1", "T", "Some text here " * 20, "CC BY 4.0", url="https://x")
    i1, s1 = st.add(d)
    assert s1 == "new" and st.add(d) == (i1, "unchanged")
    d.text += " more"
    assert st.add(d) == (i1, "updated")
    dup = Document("feed", "f1", "T2", d.text.upper(), "unknown")
    i2, s2 = st.add(dup)
    assert s2 == "duplicate" and st.get(i2).text == ""  # type: ignore[union-attr]
    got = st.get(i1)
    assert got and got.text.endswith("more") and got.license == "CC BY 4.0" and st.count("web") == 1
    assert st.get(9999) is None and st.count() == 2
    with pytest.raises(ValueError):
        st.add(Document("web", "u2", "T", "x", ""))
    for codec in ("lzma", "zlib", "none"):
        assert decode_body(encode_body({"text": "é"}, codec), codec)["text"] == "é"
    assert decode_body(None, "evicted")["text"] == ""


# ------------------------------------------------------------------- robots


ROBOTS = """
# comment
User-agent: *
Disallow: /private/
Disallow: /*.pdf$
Allow: /private/public
Crawl-delay: 3

User-agent: PolymathBot
User-agent: otherbot
Disallow: /nopoly
Allow: /nopoly/ok
Disallow: /tie
Allow: /tie

Sitemap: https://example.org/sitemap.xml
"""


def test_robots_semantics():
    r = parse_robots(ROBOTS)
    ua = "PolymathBot/0.1 (+x)"
    assert not r.allowed("https://e.org/nopoly/x", ua)
    assert r.allowed("https://e.org/nopoly/ok/1", ua)  # longest match wins
    assert r.allowed("https://e.org/tie", ua)  # Allow wins ties
    assert r.allowed("https://e.org/private/x", ua)  # our group overrides '*'
    assert r.crawl_delay(ua) is None
    other = "SomeBot/2"
    assert not r.allowed("https://e.org/private/x", other)
    assert r.allowed("https://e.org/private/public/y", other)
    assert not r.allowed("https://e.org/doc.pdf", other) and r.allowed("https://e.org/doc.pdf?x=1", other)
    assert r.crawl_delay(other) == 3.0
    assert r.allowed("https://e.org/robots.txt", other)
    assert r.sitemaps == ["https://example.org/sitemap.xml"]
    assert parse_robots("User-agent: *\nDisallow:\n").allowed("https://e.org/anything", ua)
    assert parse_robots("Disallow: /orphan\n").allowed("https://e.org/orphan", ua)
    assert parse_robots("User-agent: *\nDisallow: /%7Euser\n").allowed("https://e.org/~other", ua)
    assert not parse_robots("User-agent: *\nDisallow: /%7Euser\n").allowed("https://e.org/~user/x", ua)
    assert not parse_robots("User-agent: *\nDisallow: private\nCrawl-delay: x\n").allowed("https://e.org/private", ua)
    assert robots_for_status(404, "").allowed("https://e.org/x", ua)
    assert not robots_for_status(503, "").allowed("https://e.org/x", ua)
    assert robots_for_status(503, "").crawl_delay(ua) is None
    assert parse_robots("User-agent: foo\nDisallow: /\n").allowed("https://e.org/x", ua)


# -------------------------------------------------------------- HTML → text


ARTICLE = """<!doctype html><html lang="en-GB"><head><title>Thermal throttling explained</title>
<meta name="description" content="How CPUs protect themselves.">
<link rel="canonical" href="/articles/thermal"><link rel="license" href="https://creativecommons.org/licenses/by/4.0/">
<script>var x = "<p>not text</p>";</script><style>p{color:red}</style></head>
<body><header><a href="/">Home</a> <a href="/about">About</a></header>
<nav class="menu"><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li></ul></nav>
<div id="cookie-banner">We use cookies to improve your experience on this website and for analytics.</div>
<article><h1>Thermal throttling</h1>
<p>Thermal throttling is a mechanism by which a processor reduces its clock speed when its temperature
rises above a threshold. It is used to protect the chip from damage and to keep it within its design limits,
and it is common in small single-board computers that have no active cooling.</p>
<h2>Why it matters</h2>
<p>When a computer throttles, the work it was doing takes longer, so the performance that the user sees drops.
For a server that must run continuously, the effect can be significant over a whole day of load.</p>
<p>See <a href="/fans">fans</a> and <a href="https://other.example/heatsinks" rel="nofollow">heatsinks</a>.</p>
<p style="display: none">hidden text that should not appear in the extracted article body at all</p>
</article>
<aside><p>Related: ten things you did not know about the history of computer cooling fans and more.</p></aside>
<div class="comments"><p>Great article! I really enjoyed reading this and will share it with all of my friends.</p></div>
<footer><p>Copyright 2026 Example Corp. All rights reserved. Terms of service and privacy policy apply.</p></footer>
</body></html>"""


def test_html_extraction_keeps_article_drops_boilerplate():
    ex = extract(ARTICLE, "https://example.org/x")
    assert ex.title == "Thermal throttling explained" and ex.lang == "en"
    assert ex.description == "How CPUs protect themselves."
    assert ex.canonical == "https://example.org/articles/thermal"
    assert "Thermal throttling is a mechanism" in ex.text and "Why it matters" in ex.text
    for junk in ("cookies", "Great article", "Copyright", "not text", "color:red", "hidden text", "Related:"):
        assert junk not in ex.text, junk
    hrefs = {link.url: link for link in ex.links}
    assert "https://example.org/fans" in hrefs and hrefs["https://other.example/heatsinks"].nofollow
    assert "https://example.org/a" not in hrefs  # inside the nav
    lic, lic_url = detect_license(ex, ARTICLE)
    assert lic == "CC BY 4.0" and lic_url and "creativecommons.org/licenses/by/4.0" in lic_url


def test_html_robots_meta_short_pages_and_malformed():
    ex = extract('<html><head><meta name="robots" content="noindex, nofollow"></head><body><p>x</p></body></html>')
    assert ex.noindex and ex.nofollow
    short = extract("<p>" + "A short abstract that is the only content but still useful to keep. " * 2 + "</p>")
    assert short.text  # no 'good' block, but the best paragraph is kept
    weird = extract("<div><p>Unclosed <b>bold <i>italic</p></div></span>" + "<p>" + "word " * 60 + "</p>")
    assert "word" in weird.text
    assert classify([]) == []
    assert detect_license(extract("<p>x</p>"), "")[0].startswith("unknown")
    pd = extract(
        '<html><head><meta name="dcterms.license" content="https://creativecommons.org/publicdomain/zero/1.0/">'
        "</head></html>"
    )
    assert detect_license(pd, "")[0] == "CC0 1.0"


# ------------------------------------------------------------------ crawler


def test_url_normalization_and_domains():
    assert normalize_url("HTTP://Example.ORG:80/a/./b/../c?utm_source=x&id=2#frag") == "http://example.org/a/c?id=2"
    assert normalize_url("https://e.org:8443") == "https://e.org:8443/"
    assert normalize_url("mailto:x@y") is None and normalize_url("http://e.org:bad/") is None
    assert normalize_url("https://e.org/../../x") == "https://e.org/x"
    assert domain_allowed("docs.python.org", ["python.org"]) and not domain_allowed("evilpython.org", ["python.org"])


def test_token_bucket_math():
    b = TokenBucket(1.0, 1.0, 1.0, 0.0)
    assert b.try_take(0.0) and not b.try_take(0.5) and b.available_at(0.5) == pytest.approx(1.0)
    assert b.try_take(1.0)
    with pytest.raises(ValueError):
        TokenBucket(0)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _site(web: FakeWeb) -> None:
    body = "<p>" + "Polymath learns from text about many topics including science and history. " * 6 + "</p>"
    web.add("/robots.txt", "User-agent: *\nDisallow: /secret\nCrawl-delay: 0\n", ctype="text/plain")
    web.add(
        "/",
        f"<html><head><title>Home</title></head><body>{body}<a href='/p1'>one</a> <a href='/secret/x'>s</a>"
        f"<a href='https://elsewhere.example/x'>out</a><a href='/p2'>two</a><a href='/feed.xml'>feed</a>"
        f"<a href='/sitemap.xml'>sm</a><a href='/img.png'>img</a><a href='/gone'>gone</a></body></html>",
    )
    web.add(
        "/p1",
        f"<html><body>{body}<p>"
        + "Page one discusses the history of computing machines in great detail. " * 4
        + "</p><a href='/p3'>three</a></body></html>",
        etag='"p1"',
    )
    web.add("/p2", f"<html><head><meta name='robots' content='noindex'></head><body>{body}</body></html>")
    web.add(
        "/p3",
        f"<html><body>{body}<p>"
        + "Page three describes the biology of marine animals living in the deep sea. " * 4
        + "</p></body></html>",
    )
    web.add("/secret/x", f"<html><body>{body}</body></html>")
    web.add("/feed.xml", "<rss><channel><title>F</title></channel></rss>", ctype="application/rss+xml")
    web.add("/sitemap.xml", "<urlset><url><loc>http://127.0.0.1/p4</loc></url></urlset>", ctype="application/xml")
    web.add("/img.png", b"\x89PNG", ctype="image/png")
    web.add("/gone", "x", status=410)


def test_crawler_politeness_robots_and_links(config, db, web):
    _site(web)
    store = DocumentStore(db)
    cr = Crawler(db, client(), store, user_agent="PolymathBot/0.1", allow_domains=["127.0.0.1"], rate=1.0)
    cr.frontier.add(web.base + "/", priority=1.0)
    ctx = make_ctx(config, db, "crawl.step", services={"crawler": cr}, budget=12.0)
    out = crawl_job(ctx)
    while cr.frontier.pending() and out.result["requests"] < 20:
        ctx = make_ctx(config, db, "crawl.step", services={"crawler": cr}, budget=12.0)
        out = crawl_job(ctx)
    paths = [p for _t, _m, p, _h in web.log]
    assert paths[0] == "/robots.txt"
    assert "/secret/x" not in paths  # robots.txt respected
    assert paths.count("/robots.txt") == 1  # cached
    times = sorted(t for t, _m, _p, _h in web.log)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert min(gaps) >= 0.95  # ≤ 1 request per second to this host, including robots.txt
    rows = db.query("SELECT * FROM frontier")
    states = {r["url"].split(str(web.port))[1]: (r["state"], r["reason"]) for r in rows if str(web.port) in r["url"]}
    assert any(r["url"] == "http://127.0.0.1/p4" for r in rows)  # from the sitemap
    assert states["/secret/x"] == ("skipped", "robots.txt disallow")
    assert states["/gone"][0] == "failed" and states["/img.png"][0] == "skipped"
    assert states["/feed.xml"][1] == "feed registered" and states["/sitemap.xml"][1].startswith("sitemap")
    assert states["/p2"][1] == "not stored"  # noindex
    assert db.scalar("SELECT COUNT(*) FROM feeds") == 1
    titles = {r["title"] for r in db.query("SELECT title FROM documents WHERE source='web'")}
    assert "Home" in titles and store.count("web") == 3  # /, /p1, /p3
    assert not any("elsewhere" in r["url"] for r in db.query("SELECT url FROM frontier"))


def test_crawler_conditional_get_backoff_and_errors(config, db, web):
    clock = Clock()
    store = DocumentStore(db)
    cr = Crawler(db, client(), store, user_agent="PolymathBot", allow_domains=["127.0.0.1"], clock=clock)
    web.add("/robots.txt", "", status=404)
    web.add("/busy", "slow down", status=429, headers={"Retry-After": "120"})
    web.add("/p", "<p>" + "content words for the page " * 20 + "</p>", etag='"e"')
    cr.frontier.add(web.base + "/busy")
    assert cr.step()["action"] == "robots"
    clock.t += 2
    assert cr.step()["action"] == "backoff"
    host = db.one("SELECT * FROM hosts WHERE host=?", (f"127.0.0.1:{web.port}",))
    assert host["next_fetch_at"] >= clock.t + 119
    assert cr.step()["action"] == "none"
    clock.t += 200
    db.execute("UPDATE frontier SET state='done'")
    cr.frontier.add(web.base + "/p")
    assert cr.step()["stored"] is True
    db.execute("UPDATE frontier SET state='new' WHERE url LIKE '%/p'")
    clock.t += 5
    assert cr.step()["action"] == "unchanged"  # If-None-Match → 304
    assert cr.frontier.next_time() is None or cr.frontier.pending() == 0
    # unreachable host: transient error defers host and retries later
    cr2 = Crawler(db, client(), store, user_agent="PolymathBot", allow_domains=["localhost"], clock=clock)
    db.execute(
        "INSERT INTO hosts(host, robots, robots_status, robots_fetched) VALUES('localhost:1', '', 404, ?)", (clock.t,)
    )
    cr2.frontier.add("http://localhost:1/x")
    assert cr2.step()["action"] in {"error", "blocked"}


def test_robots_unreachable_disallows_temporarily(config, db):
    clock = Clock()
    cr = Crawler(db, client(), DocumentStore(db), user_agent="PolymathBot", allow_domains=["x"], clock=clock)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    robots = cr.fetch_robots(f"127.0.0.1:{port}", "http")
    assert not robots.allowed("http://x/y", "PolymathBot")


# -------------------------------------------------------------------- feeds

RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Science Feed</title><copyright>CC BY 4.0</copyright>
<item><title>New battery chemistry</title><link>http://127.0.0.1/news/1</link><guid>n1</guid>
<description>&lt;p&gt;Researchers describe a sodium battery that charges quickly and lasts for many cycles.&lt;/p&gt;</description>
<pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Tiny</title><guid>n2</guid><description>x</description></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>Entry one</title><link rel="alternate" href="https://example.org/e1"/><id>e1</id>
<updated>2026-10-01T00:00:00Z</updated><summary>A summary long enough to be stored as a document item.</summary></entry>
</feed>"""
RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
xmlns="http://purl.org/rss/1.0/"><channel><title>RDF</title></channel>
<item rdf:about="https://example.org/r1"><title>RDF item</title><description>Plenty of words in this description here.</description></item>
</rdf:RDF>"""


def test_parse_feed_formats_and_bomb():
    rss = parse_feed(RSS)
    assert rss.title == "Science Feed" and rss.rights == "CC BY 4.0" and rss.items[0].link == "http://127.0.0.1/news/1"
    atom = parse_feed(ATOM)
    assert atom.items[0].link == "https://example.org/e1" and atom.items[0].published == "2026-10-01T00:00:00Z"
    rdf = parse_feed(RDF)
    assert rdf.items[0].link == "https://example.org/r1"
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss>&b;</rss>'
    with pytest.raises(UnsafeXML):
        parse_feed(bomb)
    with pytest.raises(UnsafeXML):
        parse_untrusted_xml(b"<a><b></a>")
    with pytest.raises(UnsafeXML):
        parse_untrusted_xml(b"x" * 20_000_001)


def test_feed_poll_conditional_and_job(config, db, web):
    url = web.add("/rss", RSS, ctype="application/rss+xml", etag='"r1"')
    web.add("/atom", ATOM, ctype="application/atom+xml")
    web.add("/bad", b"<rss><channel>", ctype="application/rss+xml")
    store = DocumentStore(db)
    cr = Crawler(db, client(), store, user_agent="PolymathBot", allow_domains=["127.0.0.1"])
    res = poll_feed(db, client(), store, cr.frontier, url, ["127.0.0.1"])
    assert res["new"] == 1 and res["items"] == 2
    doc = store.get(int(db.scalar("SELECT id FROM documents WHERE source='feed'")))
    assert doc and "sodium battery" in doc.text and "<p>" not in doc.text and doc.license.startswith("publisher terms")
    assert db.scalar("SELECT depth FROM frontier WHERE url='http://127.0.0.1/news/1'") == 0
    assert poll_feed(db, client(), store, cr.frontier, url, [])["status"] == 304
    config.senses.feeds = [web.base + "/atom", web.base + "/bad"]
    ctx = make_ctx(config, db, "feeds.poll", {"interval": 0}, {"http": client(), "docs": store, "crawler": cr})
    out = feeds_job(ctx)
    assert out.value >= 1 and out.result["polled"] == 3
    assert db.scalar("SELECT errors FROM feeds WHERE url LIKE '%/bad'") == 1
    # the Atom feed lives on 127.0.0.1 but links to example.org: summary kept, the other site never fetched
    assert db.scalar("SELECT COUNT(*) FROM frontier WHERE url='https://example.org/e1'") == 0
    assert db.scalar("SELECT COUNT(*) FROM documents WHERE title='Entry one'") == 1


def test_json_job_results_are_serialisable(config, db, web):
    web.add("/robots.txt", "", status=404)
    cr = Crawler(db, client(), DocumentStore(db), user_agent="PolymathBot", allow_domains=[])
    out = crawl_job(make_ctx(config, db, services={"crawler": cr}))
    assert json.dumps(out.result) and out.done


def test_only_approved_sites_are_ever_fetched(config, db, web):
    """The crawler fetches only the allow-list and the approved feeds' own sites — never where links lead."""
    from polymath.senses.crawler import site_of

    assert [site_of(h) for h in ("feeds.npr.org", "rss.nytimes.com", "feeds.bbci.co.uk", "www.bbc.co.uk",
                                 "news.example.com:8080", "127.0.0.1:9", "localhost")] == [
        "npr.org", "nytimes.com", "bbci.co.uk", "bbc.co.uk", "example.com", "127.0.0.1", "localhost"]  # fmt: skip
    web.add("/robots.txt", "", status=404)
    web.add("/ok", "<p>" + "approved page words here " * 20 + "</p>")
    web.add("/hop", "", status=302, headers={"Location": f"http://localhost:{web.port}/ok"})  # same server, other site
    clock = Clock()
    cr = Crawler(db, client(), DocumentStore(db), user_agent="PolymathBot", allow_domains=["127.0.0.1"], clock=clock)
    assert cr.permitted("127.0.0.1:5") and not cr.permitted("evil.example.net")
    cr.frontier.add("https://evil.example.net/page", priority=9.0, depth=99)  # e.g. queued before this rule existed
    out = cr.step()
    assert out == {"action": "skipped", "url": "https://evil.example.net/page", "reason": "not an approved site"}
    assert db.scalar("SELECT reason FROM frontier WHERE url='https://evil.example.net/page'") == "not an approved site"
    cr.frontier.add(web.base + "/hop", priority=5.0)
    assert cr.step()["action"] == "robots"
    clock.t += 5
    out = cr.step()
    assert out["action"] == "skipped" and out["reason"] == "redirected off the approved sites"
    assert not db.scalar("SELECT COUNT(*) FROM documents WHERE source='web'")
    feeds_cr = Crawler(db, client(), DocumentStore(db), user_agent="PolymathBot", allow_domains=[],
                       feed_sites=["www.nature.com"])  # fmt: skip
    assert feeds_cr.permitted("nature.com") and feeds_cr.permitted("www.nature.com")
    assert not feeds_cr.permitted("nature.example.com")


def test_default_feeds_link_only_to_their_own_sites():
    """No aggregator whose items point anywhere on the web is in the default feed list."""
    from polymath.senses.sources import SAMPLE_FEEDS

    assert not [u for u in SAMPLE_FEEDS if "hnrss" in u or "reddit" in u]
