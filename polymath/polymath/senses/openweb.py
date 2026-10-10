"""Open-web learning, vetted: sites beyond the fixed sources, each checked before its first visit.

**Where new sites come from.** Only from links that trusted sources point to: every external link in the
Wikipedia articles the agent reads is counted per site (``site_citations``). A site cited by many articles
has been vetted by Wikipedia's editors; a random link has not. The crawler never searches and never follows
links to sites that were not vetted.

**The gate** (``web.vet``), all checks offline except one request for robots.txt:

1. the **safety lists** must be loaded — until they are, nothing is vetted (fail closed);
2. the site and every parent domain must be absent from the lists (adult, malware, phishing, gambling, fake
   news: StevenBlack, URLhaus, Université Toulouse 1 — downloaded as plain files, matched by 64-bit hashes);
3. its name must not suggest adult or gambling content;
4. it must be cited by at least ``open_web_min_citations`` Wikipedia articles;
5. it must answer over **HTTPS with a valid certificate** (verified by Python's TLS) on a public address;
6. its robots.txt must allow the crawler on the cited pages.

**Probation.** A site cited by fewer than ``open_web_trusted_citations`` articles starts on probation: at most
``open_web_probation_pages`` pages are read. The facts read from it are compared with well-sourced ones
(``web.trust``). A site whose facts mostly contradict them is dropped; one whose facts agree is approved.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.core.logging import get_logger
from polymath.senses.crawler import Crawler, host_of, normalize_url, site_of
from polymath.senses.net import BlockedURL, FetchError, HttpClient
from polymath.senses.robots import MAX_ROBOTS_BYTES, robots_for_status

log = get_logger("openweb")
EXTERNAL_LINK = re.compile(r"https?://[^\s\[\]<>|{}\"'`]+", re.I)
# Link targets that are not sites to learn from: archives, identifier resolvers, Wikimedia itself, social media.
SKIP_SITES = {
    "archive.org",
    "archive.today",
    "archive.ph",
    "archive.is",
    "webcitation.org",
    "doi.org",
    "dx.doi.org",
    "handle.net",
    "worldcat.org",
    "google.com",
    "google.co.uk",
    "books.google.com",
    "wikipedia.org",
    "wikimedia.org",
    "wikidata.org",
    "wiktionary.org",
    "wikisource.org",
    "wikiquote.org",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "linkedin.com",
    "bit.ly",
    "t.co",
    "goo.gl",
    "tinyurl.com",
}
URLS_PER_SITE = 5
BAD_SUBSTRINGS = ("porn", "xxx", "hentai", "camgirl", "escort", "casino", "gambling", "warez", "nsfw")
BAD_TOKENS = {"sex", "adult", "nude", "nudes", "bet", "betting", "torrent", "torrents", "crack", "cracks"}
VET_BATCH = 20
RETRY_AFTER = 86400.0
TRUST_MIN_CHECKED = 10
DROP_BELOW = 0.4
APPROVE_ABOVE = 0.6


def event(db: Database, kind: str, text: str, **detail: Any) -> None:
    db.execute(
        "INSERT INTO events(at, kind, text, detail) VALUES(?,?,?,?)",
        (time.time(), kind, text, json.dumps(detail, default=str)),
    )


# ------------------------------------------------------------------ citations from Wikipedia
def record_citations(db: Database, wikitext: str, now: float | None = None) -> int:
    """Count the sites one article cites (once per site per article). Returns how many sites it cited."""
    now = time.time() if now is None else now
    per_site: dict[str, list[str]] = {}
    for raw in EXTERNAL_LINK.findall(wikitext):
        url = normalize_url(raw.rstrip(".,;:)!?'"))
        if url is None:
            continue
        host = host_of(url)
        site = site_of(host)
        if not site or "." not in site or site in SKIP_SITES or site.replace(".", "").isdigit():
            continue
        per_site.setdefault(site, [])
        if len(per_site[site]) < URLS_PER_SITE:
            per_site[site].append(url)
    for site, urls in per_site.items():
        db.execute(
            "INSERT INTO site_citations(site, citations, first_seen, last_seen) VALUES(?,1,?,?) "
            "ON CONFLICT(site) DO UPDATE SET citations = citations + 1, last_seen = excluded.last_seen",
            (site, now, now),
        )
        have = int(db.scalar("SELECT COUNT(*) FROM site_urls WHERE site=?", (site,), default=0))
        for url in urls[: max(0, URLS_PER_SITE - have)]:
            db.execute("INSERT OR IGNORE INTO site_urls(site, url) VALUES(?,?)", (site, url))
    return len(per_site)


# ------------------------------------------------------------------ safety lists
def domain_hash(domain: str) -> int:
    return int.from_bytes(hashlib.blake2b(domain.lower().encode(), digest_size=8).digest(), "big", signed=True)


def suffixes(host: str) -> list[str]:
    """``a.b.example.com`` → ``[a.b.example.com, b.example.com, example.com]`` (a listed parent blocks children)."""
    name = (host.rsplit(":", 1)[0] if host.count(":") == 1 else host).lower().strip(".")
    parts = name.split(".")
    return [".".join(parts[i:]) for i in range(max(1, len(parts) - 1))]


def list_names(db: Database) -> list[str]:
    return list(db.kv_get("blocklists") or [])


def blocked(db: Database, host: str) -> str | None:
    """The safety list that blocks ``host`` (or one of its parent domains), else None."""
    names = list_names(db)
    for s in suffixes(host):
        row = db.one("SELECT list FROM blocked_domains WHERE h=?", (domain_hash(s),))
        if row is not None:
            i = int(row["list"])
            return names[i] if i < len(names) else "a safety list"
    return None


def bad_name(host: str) -> bool:
    name = host.lower()
    if any(b in name for b in BAD_SUBSTRINGS):
        return True
    return any(tok in BAD_TOKENS for tok in re.split(r"[.\-_0-9]+", name))


def list_name(url: str) -> str:
    if "StevenBlack" in url:
        return "StevenBlack (" + url.rstrip("/").split("/")[-2] + ")"
    if "urlhaus" in url:
        return "URLhaus (malware)"
    if "ut-capitole" in url:
        return "UT1 " + Path(url).name.removesuffix(".tar.gz")
    return url.split("/")[2] if "//" in url else url


def blocklists_ready(db: Database, config: Any) -> bool:
    loaded = db.kv_get("blocklists_loaded") or {}
    return bool(config.senses.blocklists) and all(str(i) in loaded for i in range(len(config.senses.blocklists)))


def plan_blocklists_job(ctx: JobContext) -> JobOutcome:
    """Job ``web.blocklists``: (re)download every safety list (weekly); each download then loads itself."""
    urls = list(ctx.config.senses.blocklists)
    ctx.db.kv_set("blocklists", [list_name(u) for u in urls])
    week = int(time.time() // (7 * 86400))
    for i, url in enumerate(urls):
        dest = f"blocklists/{i}-{Path(url.rstrip('/')).name or 'list'}"
        ctx.scheduler.enqueue(
            "dump.download",
            {
                "url": url,
                "dest": dest,
                "then": {
                    "kind": "web.blocklist",
                    "payload": {"index": i, "url": url, "dest": dest},
                    "key": f"web.blocklist:{i}:{week}",
                    "priority": 3.2,
                },
            },
            key=f"blocklist-download:{i}:{week}",
            priority=3.2,
        )
    return JobOutcome(done=True, value=0.1, result={"lists": len(urls)})


def _domains_of_line(line: str) -> str | None:
    """A domain from a hosts-file line (``0.0.0.0 example.com``) or a plain list line (``example.com``)."""
    line = line.split("#", 1)[0].strip().lower()
    if not line:
        return None
    parts = line.split()
    name = parts[1] if len(parts) >= 2 and parts[0] in {"0.0.0.0", "127.0.0.1", "::", "::1"} else parts[0]
    if name in {"localhost", "localhost.localdomain", "local", "broadcasthost", "0.0.0.0"} or "." not in name:
        return None
    if "/" in name or name.replace(".", "").isdigit():
        return None
    return name.strip(".")


def _extract_tar_domains(path: Path, out: Path) -> int:
    """UT1 archives hold ``<category>/domains`` files: concatenate them into one plain list."""
    n = 0
    with tarfile.open(path, "r:gz") as tar, out.open("w", encoding="utf-8") as dst:
        for member in tar:
            if member.isfile() and member.name.endswith("/domains"):
                f = tar.extractfile(member)
                if f is None:
                    continue
                for raw in f:
                    dst.write(raw.decode("utf-8", "replace"))
                    n += 1
    return n


def load_blocklist_job(ctx: JobContext) -> JobOutcome:
    """Job ``web.blocklist``: payload {index, url, dest}. Load a downloaded list in slices (hashes only)."""
    from polymath.senses.dumpfiles import raw_path

    p = ctx.job.payload
    i = int(p["index"])
    path = raw_path(ctx, str(p["dest"]))
    cp = dict(ctx.job.checkpoint or {})
    loaded = dict(ctx.db.kv_get("blocklists_loaded") or {})
    gen = int(cp.get("gen") or int(loaded.get(str(i), {}).get("gen", 0)) + 1)
    plain = path
    if str(p["url"]).endswith(".tar.gz"):
        plain = path.with_name(path.name + ".domains")
        if not cp.get("extracted"):
            _extract_tar_domains(path, plain)
            cp["extracted"] = True
            ctx.tick()
    offset, count = int(cp.get("offset", 0)), int(cp.get("count", 0))
    batch: list[tuple[int, int, int]] = []
    with plain.open("rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                break
            name = _domains_of_line(line.decode("utf-8", "replace"))
            if name:
                batch.append((domain_hash(name), i, gen))
            if len(batch) >= 5000:
                ctx.db.executemany("INSERT OR REPLACE INTO blocked_domains(h, list, gen) VALUES(?,?,?)", batch)
                count += len(batch)
                batch = []
                if ctx.should_stop():
                    offset = f.tell()
                    return JobOutcome(
                        done=False,
                        checkpoint={**cp, "gen": gen, "offset": offset, "count": count},
                        value=0.0,
                        result={"loaded": count},
                    )
        if batch:
            ctx.db.executemany("INSERT OR REPLACE INTO blocked_domains(h, list, gen) VALUES(?,?,?)", batch)
            count += len(batch)
    ctx.db.execute("DELETE FROM blocked_domains WHERE list=? AND gen<>?", (i, gen))  # entries gone from the list
    names = list_names(ctx.db)
    name = names[i] if i < len(names) else list_name(str(p["url"]))
    loaded[str(i)] = {"name": name, "count": count, "gen": gen, "at": time.time()}
    ctx.db.kv_set("blocklists_loaded", loaded)
    for f_ in {path, plain}:
        f_.unlink(missing_ok=True)
    event(ctx.db, "safety", f"safety list loaded: {name} ({count:,} domains)", list=name, count=count)
    if blocklists_ready(ctx.db, ctx.config) and not ctx.db.kv_get("open_web_announced"):
        ctx.db.kv_set("open_web_announced", True)
        event(ctx.db, "safety", "all safety lists loaded: open-web learning can start vetting sites")
    return JobOutcome(done=True, value=0.2, result={"list": name, "domains": count})


# ------------------------------------------------------------------ the gate
def _record(db: Database, site: str, status: str, reason: str, citations: int, retry_at: float | None = None) -> None:
    db.execute(
        "INSERT INTO sites(site, status, reason, citations, vetted_at, retry_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(site) DO UPDATE SET status=excluded.status, reason=excluded.reason, citations=excluded.citations, "
        "vetted_at=excluded.vetted_at, retry_at=excluded.retry_at",
        (site, status, reason, citations, time.time(), retry_at),
    )


def vet_site(ctx: JobContext, site: str, citations: int) -> tuple[str, str]:
    """Run the gate on one site; records and announces the verdict. Returns (status, reason)."""
    cfg = ctx.config.senses
    db = ctx.db

    def verdict(status: str, reason: str, retry: bool = False) -> tuple[str, str]:
        _record(db, site, status, reason, citations, time.time() + RETRY_AFTER if retry else None)
        if status != "retry":
            event(
                db, "site", f"{site}: {status} — {reason}", site=site, status=status, reason=reason, citations=citations
            )
        return status, reason

    listed = blocked(db, site)
    if listed:
        return verdict("refused", f"on the safety list {listed}")
    if bad_name(site):
        return verdict("refused", "its name suggests adult or gambling content")
    urls = [str(r["url"]) for r in db.query("SELECT url FROM site_urls WHERE site=? ORDER BY url", (site,))]
    candidates = [normalize_url("https://" + u.split("://", 1)[1]) for u in urls]
    https = [u for u in candidates if u and site_of(host_of(u)) == site and not blocked(db, host_of(u))]
    if not https:
        return verdict("refused", "no usable cited pages")
    host = host_of(https[0])
    client: HttpClient = ctx.services["http"]
    try:
        resp = client.get(f"https://{host}/robots.txt", max_bytes=MAX_ROBOTS_BYTES, ok=tuple(range(200, 600)))
    except BlockedURL as exc:
        return verdict("refused", f"not a public address ({exc})")
    except FetchError as exc:
        msg = str(exc)
        if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
            return verdict("refused", "no valid HTTPS certificate")
        return verdict("retry", f"unreachable for now: {msg[:120]}", retry=True)
    final = host_of(resp.url)
    if not resp.url.startswith("https://") or site_of(final) != site:
        return verdict("refused", f"robots.txt redirects away to {final or resp.url[:60]}")
    robots = robots_for_status(resp.status, resp.text() if resp.status < 400 else "")
    agent = getattr(client, "user_agent", "PolymathBot")
    allowed = [u for u in https if robots.allowed(u, agent)]
    if not allowed:
        return verdict("refused", "its robots.txt asks crawlers to stay away")
    status = "approved" if citations >= cfg.open_web_trusted_citations else "probation"
    why = f"cited by {citations:,} Wikipedia articles, valid HTTPS, robots.txt allows"
    if status == "probation":
        why += f"; on probation for {cfg.open_web_probation_pages} pages"
    result = verdict(status, why)  # recorded first, so the crawler sees the site as vetted right away
    crawler: Crawler | None = ctx.services.get("crawler")
    if crawler is not None:
        crawler.refresh_vetted()
        for u in allowed:
            crawler.frontier.add(u, priority=0.8, depth=max(0, crawler.max_depth - 1))
    return result


def vet_job(ctx: JobContext) -> JobOutcome:
    """Job ``web.vet``: run the gate on the most-cited sites not vetted yet."""
    cfg = ctx.config.senses
    if not cfg.open_web:
        return JobOutcome(done=True, result={"skipped": "senses.open_web is off"})
    if not blocklists_ready(ctx.db, ctx.config):
        return JobOutcome(done=True, result={"skipped": "waiting for the safety lists"})
    now = time.time()
    today = int(ctx.db.scalar("SELECT COUNT(*) FROM sites WHERE vetted_at >= ? AND status != 'retry'", (now - 86400,)))
    room = max(0, cfg.open_web_sites_per_day - today)
    rows = ctx.db.query(
        "SELECT c.site, c.citations FROM site_citations c LEFT JOIN sites s ON s.site = c.site "
        "WHERE c.citations >= ? AND (s.site IS NULL OR (s.status = 'retry' AND s.retry_at <= ?)) "
        "ORDER BY c.citations DESC LIMIT ?",
        (cfg.open_web_min_citations, now, min(VET_BATCH, room)),
    )
    out: dict[str, int] = {}
    for r in rows:
        status, _ = vet_site(ctx, str(r["site"]), int(r["citations"]))
        out[status] = out.get(status, 0) + 1
        ctx.tick()
        if ctx.should_stop():
            break
    return JobOutcome(done=True, value=0.05 * sum(out.values()), result=out or {"vetted": 0})


# ------------------------------------------------------------------ probation: do its facts check out?
def site_agreement(db: Database, site: str, functional: set[int]) -> tuple[int, int]:
    """(agree, contradict) for entity facts read on ``site``, compared with Wikidata-sourced facts."""
    agree = contradict = 0
    rows = db.query(
        "SELECT DISTINCT t.id, t.s, t.p, t.o FROM provenance v JOIN documents d ON d.id = v.doc_id "
        "JOIN triples t ON t.id = v.triple_id WHERE d.source = 'web' AND t.o != 0 AND "
        "(json_extract(d.meta, '$.host') = ? OR json_extract(d.meta, '$.host') LIKE ?) LIMIT 2000",
        (site, f"%.{site}"),
    )
    for t in rows:
        if db.scalar("SELECT 1 FROM provenance WHERE triple_id=? AND kind='wikidata' LIMIT 1", (t["id"],)):
            agree += 1
        elif int(t["p"]) in functional and db.scalar(
            "SELECT 1 FROM triples t2 JOIN provenance v2 ON v2.triple_id = t2.id AND v2.kind = 'wikidata' "
            "WHERE t2.s = ? AND t2.p = ? AND t2.o != 0 AND t2.o != ? LIMIT 1",
            (t["s"], t["p"], t["o"]),
        ):
            contradict += 1
    return agree, contradict


def functional_by_wikidata(db: Database, threshold: float = 0.9, min_subjects: int = 20) -> set[int]:
    """Single-valued relations, judged from Wikidata-sourced facts only — so the claims of the site being judged
    cannot make a relation look multi-valued and dodge the check."""
    out = set()
    for r in db.query(
        "SELECT p, COUNT(*) AS subjects, SUM(n = 1) AS single FROM (SELECT t.p, t.s, COUNT(*) AS n FROM triples t "
        "WHERE t.holdout = 0 AND t.o != 0 AND EXISTS (SELECT 1 FROM provenance v WHERE v.triple_id = t.id "
        "AND v.kind = 'wikidata') GROUP BY t.p, t.s) GROUP BY p HAVING subjects >= ?",
        (min_subjects,),
    ):
        if int(r["single"]) / int(r["subjects"]) >= threshold:
            out.add(int(r["p"]))
    return out


def trust_job(ctx: JobContext) -> JobOutcome:
    """Job ``web.trust``: judge sites on probation by how their facts compare with well-sourced ones."""
    functional = functional_by_wikidata(ctx.db)
    out = {"approved": 0, "dropped": 0, "waiting": 0}
    for r in ctx.db.query("SELECT site FROM sites WHERE status = 'probation'"):
        site = str(r["site"])
        agree, contra = site_agreement(ctx.db, site, functional)
        checked = agree + contra
        trust = (agree + 1) / (checked + 2)
        ctx.db.execute("UPDATE sites SET trust=?, checked=? WHERE site=?", (round(trust, 3), checked, site))
        if checked < TRUST_MIN_CHECKED:
            out["waiting"] += 1
            continue
        if trust < DROP_BELOW:
            ctx.db.execute(
                "UPDATE sites SET status='dropped', reason=? WHERE site=?",
                (f"its facts contradicted well-sourced ones ({contra} of {checked})", site),
            )
            ctx.db.execute(
                "UPDATE frontier SET state='skipped' WHERE state='new' AND (host = ? OR host LIKE ?)",
                (site, f"%.{site}"),
            )
            event(
                ctx.db,
                "site",
                f"{site}: dropped — {contra} of {checked} checked facts contradicted well-sourced ones",
                site=site,
                status="dropped",
                trust=trust,
            )
            out["dropped"] += 1
        elif trust >= APPROVE_ABOVE:
            ctx.db.execute(
                "UPDATE sites SET status='approved', reason=? WHERE site=?",
                (f"{agree} of {checked} checked facts agree with well-sourced ones", site),
            )
            event(
                ctx.db,
                "site",
                f"{site}: approved after probation — {agree} of {checked} facts check out",
                site=site,
                status="approved",
                trust=trust,
            )
            out["approved"] += 1
        ctx.tick()
    return JobOutcome(done=True, value=0.05, result=out)


def iter_vetted(db: Database) -> Iterator[tuple[str, str]]:
    for r in db.query("SELECT site, status FROM sites WHERE status IN ('approved', 'probation')"):
        yield str(r["site"]), str(r["status"])


def planner(agent: Any) -> None:
    s = agent.scheduler
    s.ensure_recurring("web.blocklists", 7 * 86400, priority=3.2)
    if agent.config.senses.open_web:
        s.ensure_recurring("web.vet", 600, priority=1.0)
        s.ensure_recurring("web.trust", 3600, priority=1.0)
