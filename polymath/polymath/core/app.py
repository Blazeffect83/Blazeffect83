"""Assembles the agent: database, job registry, services, planners, policy, body."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from polymath.body.sensors import same_filesystem_as_root
from polymath.body.systemd_notify import Notifier
from polymath.core.config import MOVED_FILE, Config
from polymath.core.db import Database, open_database
from polymath.core.jobs import JobRegistry, noop_handler
from polymath.core.loop import Agent, Planner


class StorageError(RuntimeError):
    """The data directory is unsafe to use (e.g. on the SD card)."""


def check_storage(config: Config) -> None:
    data_dir = config.paths.data_dir
    moved = data_dir / MOVED_FILE
    if moved.exists():  # the brain moved to a drive, and what is mounted here is the SD card's old directory
        try:
            info = json.loads(moved.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        name = info.get("name") or info.get("id") or "a USB drive"
        raise StorageError(
            f"the brain lives on the drive {name}, which is not plugged in (or not mounted yet). "
            "Plug it in: Polymath starts by itself."
        )
    if config.paths.require_separate_mount and same_filesystem_as_root(data_dir):
        raise StorageError(
            f"{data_dir} is on the root filesystem (SD card). Mount the NVMe drive there, or set "
            "paths.require_separate_mount = false for development."
        )
    for sub in (
        data_dir,
        config.paths.raw_dir,
        config.paths.index_dir,
        config.paths.backup_dir,
        config.paths.report_dir,
        config.paths.db_path.parent,
    ):
        sub.mkdir(parents=True, exist_ok=True)


@dataclass
class Components:
    registry: JobRegistry
    services: dict[str, Any] = field(default_factory=dict)
    planners: list[tuple[float, Planner]] = field(default_factory=list)


def build_components(config: Config, db: Database, *, planners: bool = True) -> Components:
    from polymath.agents import society
    from polymath.body import maintenance
    from polymath.drive import jobs as djobs
    from polymath.drive import learn as dlearn
    from polymath.evaluation import jobs as vjobs
    from polymath.evaluation import surprise
    from polymath.memory import jobs as memjobs
    from polymath.memory.documents import DocumentStore
    from polymath.perception import embed_jobs as ejobs
    from polymath.perception import jobs as pjobs
    from polymath.reasoning import jobs as rjobs
    from polymath.reasoning import predictions
    from polymath.senses import (
        books_qa,
        crawler,
        dumpfiles,
        feeds,
        openweb,
        scholarly,
        sources,
        wikidata,
        wikidata_props,
        wikipedia,
    )
    from polymath.senses.net import client_from_config

    http = client_from_config(config.senses)
    docs = DocumentStore(db)
    sources.register_sources(docs)
    if not config.senses.feeds and config.senses.default_feeds:
        config.senses.feeds = list(sources.SAMPLE_FEEDS)
    if not config.senses.seeds and config.senses.default_seeds:
        config.senses.seeds = list(sources.SAMPLE_SEEDS)
    allow = list(config.senses.crawl_allow_domains) or sorted(
        {crawler.host_of(u) for u in config.senses.seeds if crawler.host_of(u)}
    )
    allow += [d for d in (db.kv_get("user_allow_domains", []) or []) if d not in allow]
    feed_sites = sorted({crawler.site_of(crawler.host_of(u)) for u in config.senses.feeds if crawler.host_of(u)})
    crawl = crawler.Crawler(
        db,
        http,
        docs,
        user_agent=http.user_agent,
        allow_domains=allow,
        feed_sites=feed_sites,
        probation_pages=config.senses.open_web_probation_pages,
        max_bytes=config.senses.crawl_max_bytes,
        rate=config.senses.crawl_rate,
    )
    registry = JobRegistry()
    reg = registry.register
    reg("noop", noop_handler, "No-op job used for loop acceptance tests")
    reg("sources.plan", sources.plan_sources, "Resolve current dumps and feed every source pipeline", action="plan")
    reg("dump.download", dumpfiles.download_job, "Resumable download of a dump file", action="download")
    reg("wikipedia.part", wikipedia.ingest_part, "Read Wikipedia multistream dump streams", action="read", heavy=True)
    reg("wikidata.dump", wikidata.ingest_dump, "Read Wikidata entity dump blocks", action="read", heavy=True)
    reg("wikidata.propindex", wikidata_props.propindex_job, "Locate Wikidata property pages", action="download")
    reg("wikidata.properties", wikidata_props.properties_job, "Read Wikidata property records", action="read")
    reg("openalex.ingest", scholarly.ingest_openalex, "Read an OpenAlex works file", action="read", heavy=True)
    reg("pubmed.ingest", scholarly.ingest_pubmed, "Read a PubMed XML file", action="read", heavy=True)
    reg("gutenberg.books", books_qa.gutenberg_job, "Fetch Project Gutenberg books", action="read")
    reg("stackexchange.ingest", books_qa.ingest_stackexchange, "Read Stack Exchange posts", action="read", heavy=True)
    reg("feeds.poll", feeds.feeds_job, "Poll RSS/Atom feeds", action="crawl")
    reg("crawl.step", crawler.crawl_job, "Polite crawl of the frontier", action="crawl")
    reg("memory.index", memjobs.index_documents, "Index passages, near-duplicates and topics", action="memorize")
    reg("memory.graph", memjobs.build_graph, "Build the knowledge graph from Wikidata", action="memorize")
    reg("memory.topics", memjobs.maintain_topics, "Maintain the hierarchical topic map", action="memorize")
    reg("perception.anchors", pjobs.anchor_job, "Harvest Wikipedia links, profiles, infoboxes", action="perceive")
    reg("perception.automaton", pjobs.automaton_job, "Compile the alias automaton", action="perceive", heavy=True)
    reg(
        "perception.read",
        pjobs.read_job,
        "Read documents: sentences, entities, contexts",
        action="perceive",
        heavy=True,
    )
    reg("perception.train_sentences", pjobs.train_sentences_job, "Learn sentence boundaries", action="learn")
    reg("perception.train_phrases", pjobs.train_phrases_job, "Learn phrases (NPMI)", action="learn")
    reg("perception.train_linker", pjobs.train_linker_job, "Train the entity linker", action="learn", heavy=True)
    reg("perception.relations", pjobs.relations_job, "Bootstrap relation patterns", action="learn", heavy=True)
    reg("embed.train", ejobs.train_job, "Train word embeddings (SGNS)", action="learn", heavy=True)
    reg("embed.docs", ejobs.embed_docs_job, "Embed documents and entities (SIF)", action="learn")
    reg("embed.rebuild", ejobs.rebuild_index_job, "Rebuild the IVF vector indexes", action="learn", heavy=True)
    reg("embed.quality", ejobs.quality_job, "Graph-generated embedding quality checks", action="evaluate")
    reg("reason.rules", rjobs.rules_job, "Learn inference rules and type constraints", action="reason")
    reg("reason.infer", rjobs.infer_job, "Forward-chain new facts", action="reason", heavy=True)
    reg("reason.contradictions", rjobs.contradictions_job, "Detect contradictions", action="reason")
    reg("reason.reliability", rjobs.reliability_job, "Learn source reliability", action="reason")
    reg("reason.predict", predictions.predict_job, "Guess missing facts, check earlier guesses", action="reason")
    reg("wikipedia.titles", wikipedia.fetch_titles, "Read specific Wikipedia articles (curiosity)", action="read")
    reg("drive.pagerank", djobs.pagerank_job, "Entity importance (PageRank)", action="plan")
    reg("drive.priorities", djobs.priorities_job, "Rank topics and pursue knowledge gaps", action="plan")
    reg("agents.step", society.step_job, "Agent society: give agents their turns", action="agents")
    reg("agents.verify", society.verify_job, "Agent society: settle and reward verified tasks", action="agents")
    reg("agents.evolve", society.evolve_job, "Agent society: fork strong agents, retire weak ones", action="agents")
    reg("agents.command", society.command_job, "Agent society: apply your commands", action="agents")
    reg("body.backup", maintenance.backup_job, "Nightly verified, compressed database backup", action="maintain")
    reg("body.housekeeping", maintenance.housekeeping_job, "Prune old job and cycle records", action="maintain")
    reg("body.evict", maintenance.evict_job, "Free disk space (least valuable data first)", action="maintain")
    reg("body.spill", maintenance.spill_job, "Move document bodies to plugged-in drives", action="maintain")
    reg("body.recall", maintenance.recall_job, "Bring a drive's documents back (retire it)", action="maintain")
    reg("drive.learn", dlearn.learn_job, "Learn what the user asked for", action="plan")
    reg("web.blocklists", openweb.plan_blocklists_job, "Refresh the offline safety lists", action="download")
    reg("web.blocklist", openweb.load_blocklist_job, "Load a safety list", action="maintain")
    reg("web.vet", openweb.vet_job, "Vet new sites cited by Wikipedia before any visit", action="crawl")
    reg("web.trust", openweb.trust_job, "Judge sites on probation by their facts", action="reason")
    reg("eval.digest", vjobs.digest_job, "Write the daily 'what I learned today' digest", action="evaluate")
    reg("eval.remedy", vjobs.remedy_job, "Read up on wrong answers and re-test them", action="evaluate")
    reg("eval.surprise", surprise.surprise_job, "Find facts that surprised its model", action="evaluate")
    reg("eval.holdout", vjobs.holdout_job, "Hold out facts for self-evaluation", action="evaluate")
    reg("eval.quiz", vjobs.quiz_job, "Quiz itself on held-out facts", action="evaluate")
    reg("eval.report", vjobs.report_job, "Write the nightly report", action="evaluate")
    services: dict[str, Any] = {"http": http, "docs": docs, "crawler": crawl}
    plan: list[tuple[float, Planner]] = (
        [
            (60.0, sources.planner),
            (300.0, wikidata_props.planner),
            (20.0, memjobs.planner),
            (30.0, pjobs.planner),
            (60.0, ejobs.planner),
            (120.0, rjobs.planner),
            (300.0, djobs.planner),
            (300.0, vjobs.planner),
            (600.0, maintenance.planner),
            (300.0, openweb.planner),
            (600.0, predictions.planner),
            (600.0, surprise.planner),
            (60.0, society.planner),
        ]
        if planners
        else []
    )
    return Components(registry=registry, services=services, planners=plan)


def build_agent(config: Config, *, notifier: Notifier | None = None, planners: bool = True) -> Agent:
    from polymath.memory import pool as storage_pool

    check_storage(config)
    storage_pool.activate(config)
    db = open_database(config.paths.db_path)
    db.execute(f"PRAGMA wal_autocheckpoint={int(config.body.wal_autocheckpoint)}")  # fewer write-backs
    comps = build_components(config, db, planners=planners and config.loop.planners)
    from polymath.body.guard import Guard
    from polymath.drive.bandit import BanditPolicy

    return Agent(
        config,
        db,
        comps.registry,
        policy=BanditPolicy(db),
        body=Guard(config, db),
        notifier=notifier,
        services=comps.services,
        planners=comps.planners,
    )
