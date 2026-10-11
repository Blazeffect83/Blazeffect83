"""Source registry (with license metadata), the source planner job and sample mode."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from polymath.core.config import Config
from polymath.core.db import Database
from polymath.core.jobs import JobContext, JobOutcome
from polymath.core.scheduler import Scheduler
from polymath.memory.documents import DocumentStore
from polymath.senses import books_qa, scholarly, wikidata, wikipedia
from polymath.senses.crawler import Crawler
from polymath.senses.net import FetchError, HttpClient


@dataclass(frozen=True)
class SourceInfo:
    name: str
    license: str
    license_url: str | None
    homepage: str
    description: str


SOURCES = [
    SourceInfo(
        "wikipedia",
        wikipedia.LICENSE,
        wikipedia.LICENSE_URL,
        "https://dumps.wikimedia.org",
        "Wikipedia articles from the official multistream XML dumps",
    ),
    SourceInfo(
        "wikidata",
        wikidata.LICENSE,
        wikidata.LICENSE_URL,
        "https://dumps.wikimedia.org/wikidatawiki/entities/",
        "Wikidata entities from the JSON dump (labels, aliases, statements)",
    ),
    SourceInfo(
        "openalex",
        scholarly.OPENALEX_LICENSE,
        scholarly.OPENALEX_LICENSE_URL,
        "https://openalex.org",
        "Scholarly works from the OpenAlex snapshot (titles and abstracts)",
    ),
    SourceInfo(
        "pubmed",
        scholarly.PUBMED_LICENSE,
        scholarly.PUBMED_LICENSE_URL,
        "https://pubmed.ncbi.nlm.nih.gov",
        "Biomedical citations and abstracts from the PubMed baseline/update files",
    ),
    SourceInfo(
        "gutenberg",
        books_qa.GUTENBERG_LICENSE,
        books_qa.GUTENBERG_LICENSE_URL,
        "https://www.gutenberg.org",
        "Public-domain books from Project Gutenberg",
    ),
    SourceInfo(
        "stackexchange",
        "CC BY-SA (version per post)",
        "https://creativecommons.org/licenses/by-sa/4.0/",
        "https://archive.org/details/stackexchange",
        "Questions and answers from Stack Exchange data dumps",
    ),
    SourceInfo("feed", "per publisher (recorded per item)", None, "", "Items from configured RSS/Atom feeds"),
    SourceInfo(
        "web",
        "per page (Creative Commons detected; otherwise unknown)",
        None,
        "",
        "Pages fetched by the polite crawler from allow-listed domains",
    ),
]


def register_sources(store: DocumentStore) -> None:
    for s in SOURCES:
        store.register_source(s.name, s.license, s.license_url, s.homepage, s.description)


def _active(db: Database, kind: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM jobs WHERE kind=? AND state IN ('queued','running')", (kind,), 0))


def _done_keys(db: Database, prefix: str) -> set[str]:
    return {str(r["key"]) for r in db.query("SELECT key FROM jobs WHERE key LIKE ?", (prefix + "%",))}


def plan_sources(ctx: JobContext) -> JobOutcome:
    """Job ``sources.plan``: resolve current dumps and keep every source pipeline fed."""
    cfg = ctx.config.senses
    client: HttpClient = ctx.services["http"]
    s = ctx.scheduler
    db = ctx.db
    planned: dict[str, Any] = {}
    errors: dict[str, str] = {}
    from polymath.drive.strategy import weight  # read more of what teaches it the most (drive.strategy)

    w = {
        name: weight(db, name) for name in ("wikipedia", "wikidata", "openalex", "pubmed", "gutenberg", "stackexchange")
    }

    def attempt(name: str, fn: Any) -> None:
        if ctx.should_stop():
            return
        try:
            planned[name] = fn()
        except (FetchError, ValueError, KeyError) as exc:
            errors[name] = str(exc)[:300]

    def plan_wikipedia() -> int:
        if _active(db, "wikipedia.part"):
            return 0
        seen = _done_keys(db, "wikipedia.part:")
        langs = [cfg.wikipedia_lang] + ([cfg.second_language] if cfg.second_language else [])
        todo: list[tuple[float, Any]] = []
        for i, lang in enumerate(langs):
            parts = wikipedia.resolve_multistream(client, lang)
            done = sum(f"wikipedia.part:{p.name}" in seen for p in parts)
            nxt = next((p for p in parts if f"wikipedia.part:{p.name}" not in seen), None)
            if nxt is not None:  # the second language gets about one part in five
                todo.append(((done + 1) * (4.0 if i else 1.0), nxt))
        if not todo:
            return 0
        part = min(todo, key=lambda t: t[0])[1]
        s.enqueue(
            "wikipedia.part",
            {"dump_url": part.dump_url, "index_url": part.index_url, "size": part.size, "lang": part.lang},
            key=f"wikipedia.part:{part.name}",
            priority=2.0 * w["wikipedia"],
        )
        return 1

    def plan_wikidata() -> int:
        if _active(db, "wikidata.dump"):
            return 0
        url, size = wikidata.resolve_dump(client)
        _, created = s.enqueue(
            "wikidata.dump",
            {"url": url, "size": size, "max_bytes": cfg.wikidata_max_bytes},
            key=f"wikidata.dump:{url}",
            priority=1.5 * w["wikidata"],
        )
        return int(created)

    def plan_openalex() -> int:
        if _active(db, "dump.download") + _active(db, "openalex.ingest") >= 2:
            return 0
        n = 0
        for f in scholarly.openalex_files(client, cfg.openalex_files):
            rel = "openalex/" + f["url"].split("/data/jsonl/works/", 1)[-1].replace("/", "_")
            _, created = s.enqueue(
                "dump.download",
                {
                    "url": f["url"],
                    "dest": rel,
                    "then": {"kind": "openalex.ingest", "payload": {"dest": rel}, "key": f"openalex.ingest:{rel}"},
                },
                key=f"download:{f['url']}",
                priority=1.0 * w["openalex"],
            )
            n += created
        return n

    def plan_pubmed() -> int:
        n = 0
        for url in scholarly.pubmed_files(client, cfg.pubmed_files):
            rel = "pubmed/" + url.rsplit("/", 1)[-1]
            _, created = s.enqueue(
                "dump.download",
                {
                    "url": url,
                    "dest": rel,
                    "then": {"kind": "pubmed.ingest", "payload": {"dest": rel}, "key": f"pubmed.ingest:{rel}"},
                },
                key=f"download:{url}",
                priority=1.0 * w["pubmed"],
            )
            n += created
        return n

    def plan_gutenberg() -> int:
        week = int(time.time() // (7 * 86400))
        rel = f"gutenberg/pg_catalog_{week}.csv.gz"
        _, created = s.enqueue(
            "dump.download",
            {
                "url": f"{books_qa.GUTENBERG_MIRROR}/cache/epub/feeds/pg_catalog.csv.gz",
                "dest": rel,
                "then": {
                    "kind": "gutenberg.books",
                    "payload": {"catalog": rel, "limit": cfg.gutenberg_books},
                    "key": f"gutenberg.books:{week}",
                },
            },
            key=f"download:gutenberg-catalog:{week}",
            priority=0.5 * w["gutenberg"],
        )
        return int(created)

    def plan_stackexchange() -> int:
        n = 0
        for site in cfg.stackexchange_sites:
            url, host = books_qa.se_archive_url(site)
            rel = f"stackexchange/{host}.7z"
            _, created = s.enqueue(
                "dump.download",
                {
                    "url": url,
                    "dest": rel,
                    "then": {
                        "kind": "stackexchange.ingest",
                        "payload": {"dest": rel, "host": host},
                        "key": f"stackexchange.ingest:{host}",
                    },
                },
                key=f"download:{url}",
                priority=0.8 * w["stackexchange"],
            )
            n += created
        return n

    for name, fn in (
        ("wikipedia", plan_wikipedia),
        ("wikidata", plan_wikidata),
        ("openalex", plan_openalex),
        ("pubmed", plan_pubmed),
        ("gutenberg", plan_gutenberg),
        ("stackexchange", plan_stackexchange),
    ):
        attempt(name, fn)
        ctx.tick()
    return JobOutcome(
        done=True, value=0.1 * sum(int(v) for v in planned.values()), result={"planned": planned, "errors": errors}
    )


def planner(agent: Any) -> None:
    """Cheap, network-free planner run from OBSERVE: keeps recurring sense jobs alive."""
    s: Scheduler = agent.scheduler
    cfg: Config = agent.config
    s.ensure_recurring("sources.plan", 6 * 3600, priority=3.0)
    from polymath.drive.strategy import weight

    if cfg.senses.feeds:
        s.ensure_recurring("feeds.poll", 3600, priority=1.0 * weight(agent.db, "feed"))
    crawler: Crawler | None = agent.services.get("crawler")
    if crawler is not None:
        for url in cfg.senses.seeds:
            crawler.frontier.add(url, priority=1.0)
        if crawler.frontier.pending():
            s.ensure_recurring("crawl.step", 60, priority=0.6 * weight(agent.db, "web"))


# ---------------------------------------------------------------- sample mode

SAMPLE_FEEDS = [
    "https://www.nasa.gov/feed/",
    "https://www.sciencedaily.com/rss/all.xml",
    "https://phys.org/rss-feed/",
    "https://www.nature.com/nature.rss",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.theguardian.com/science/rss",
    "https://www.theguardian.com/technology/rss",
    "https://arstechnica.com/feed/",
    "https://www.raspberrypi.com/news/feed/",
    "https://blog.python.org/feeds/posts/default",
    "https://lwn.net/headlines/rss",
    "https://www.quantamagazine.org/feed/",
    "https://www.eurekalert.org/rss/technology_engineering.xml",
    "https://www.eurekalert.org/rss.xml",
    "https://www.newscientist.com/feed/home/",
    "https://www.livescience.com/feeds/all",
    "https://www.wired.com/feed/rss",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.npr.org/1019/rss.xml",
    "https://feeds.npr.org/1007/rss.xml",
    "https://www.smithsonianmag.com/rss/latest_articles/",
    "https://www.scientificamerican.com/platform/syndication/rss/",
    "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    "https://www.sciencenews.org/feed",
    "https://www.sciencedaily.com/rss/top/science.xml",
    "https://www.sciencedaily.com/rss/computers_math.xml",
    "https://www.sciencedaily.com/rss/health_medicine.xml",
    "https://www.sciencedaily.com/rss/earth_climate.xml",
    "https://www.sciencedaily.com/rss/space_time.xml",
    "https://www.sciencedaily.com/rss/matter_energy.xml",
    "https://www.sciencedaily.com/rss/plants_animals.xml",
    "https://www.sciencedaily.com/rss/mind_brain.xml",
    "https://feeds.arstechnica.com/arstechnica/science",
    "https://www.theguardian.com/world/rss",
    "https://www.theguardian.com/environment/rss",
    "https://feeds.bbci.co.uk/news/health/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://www.technologyreview.com/feed/",
    "https://spectrum.ieee.org/feeds/feed.rss",
    "https://rss.arxiv.org/rss/cs.AI",
    "https://rss.arxiv.org/rss/cs.LG",
    "https://www.nature.com/subjects/physics.rss",
    "https://www.nasa.gov/news-release/feed/",
    "https://feeds.npr.org/1001/rss.xml",
]
SAMPLE_SEEDS = [
    "https://docs.python.org/3/tutorial/index.html",
    "https://www.sqlite.org/docs.html",
    "https://www.gnu.org/philosophy/philosophy.html",
    "https://www.raspberrypi.com/documentation/computers/raspberry-pi.html",
    "https://en.wikibooks.org/wiki/Wikibooks:Featured_books",
]


def enqueue_sample(
    db: Database, client: HttpClient, config: Config, sources: list[str], n: int, tag: str = ""
) -> dict[str, str]:
    """Enqueue one bounded job per source producing ``n`` items each (acceptance sampling)."""
    s = Scheduler(db)
    notes: dict[str, str] = {}
    tag = tag or f"sample{n}"
    if "wikipedia" in sources:
        part = wikipedia.resolve_multistream(client, config.senses.wikipedia_lang)[0]
        s.enqueue(
            "wikipedia.part",
            {"dump_url": part.dump_url, "index_url": part.index_url, "size": part.size, "lang": part.lang, "limit": n},
            key=f"{tag}:wikipedia",
            priority=5,
        )
        notes["wikipedia"] = part.dump_url
    if "wikidata" in sources:
        url, size = wikidata.resolve_dump(client)
        s.enqueue(
            "wikidata.dump",
            {"url": url, "size": size, "limit": n, "max_bytes": 600_000_000},
            key=f"{tag}:wikidata",
            priority=5,
        )
        notes["wikidata"] = url
    if "openalex" in sources:
        f = scholarly.openalex_files(client, 1)[0]
        rel = f"openalex/{tag}.jsonl.gz"
        s.enqueue(
            "dump.download",
            {
                "url": f["url"],
                "dest": rel,
                "limit": 60_000_000,
                "then": {
                    "kind": "openalex.ingest",
                    "payload": {"dest": rel, "limit": n},
                    "key": f"{tag}:openalex.ingest",
                },
            },
            key=f"{tag}:openalex",
            priority=5,
        )
        notes["openalex"] = f["url"]
    if "pubmed" in sources:
        url = scholarly.pubmed_files(client, 1)[0]
        rel = f"pubmed/{tag}.xml.gz"
        s.enqueue(
            "dump.download",
            {
                "url": url,
                "dest": rel,
                "limit": 25_000_000,
                "then": {"kind": "pubmed.ingest", "payload": {"dest": rel, "limit": n}, "key": f"{tag}:pubmed.ingest"},
            },
            key=f"{tag}:pubmed",
            priority=5,
        )
        notes["pubmed"] = url
    if "gutenberg" in sources:
        rel = f"gutenberg/{tag}_catalog.csv.gz"
        s.enqueue(
            "dump.download",
            {
                "url": f"{books_qa.GUTENBERG_MIRROR}/cache/epub/feeds/pg_catalog.csv.gz",
                "dest": rel,
                "then": {
                    "kind": "gutenberg.books",
                    "payload": {"catalog": rel, "limit": n},
                    "key": f"{tag}:gutenberg.books",
                },
            },
            key=f"{tag}:gutenberg",
            priority=4,
        )
        notes["gutenberg"] = "pg_catalog.csv.gz"
    if "stackexchange" in sources:
        site = config.senses.stackexchange_sites[0] if config.senses.stackexchange_sites else "ai"
        url, host = books_qa.se_archive_url(site)
        rel = f"stackexchange/{host}.7z"
        s.enqueue(
            "dump.download",
            {
                "url": url,
                "dest": rel,
                "then": {
                    "kind": "stackexchange.ingest",
                    "payload": {"dest": rel, "host": host, "limit": n},
                    "key": f"{tag}:stackexchange.ingest",
                },
            },
            key=f"{tag}:stackexchange",
            priority=5,
        )
        notes["stackexchange"] = url
    return notes
