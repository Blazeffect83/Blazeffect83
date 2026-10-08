"""Service container wiring every component together."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .agent.state_machine import StateMachine
from .config import Settings
from .db import Database
from .knowledge.store import KnowledgeStore
from .models.base import ModelProvider
from .models.router import Router
from .notifications import Notifier
from .observability.audit import AuditLog
from .research.fetcher import Fetcher
from .security.approvals import Approvals
from .security.network_policy import PinnedOriginPolicy, URLPolicy
from .security.policy import PolicyEngine
from .security.redact import configure_redactor
from .security.sandbox import Sandbox, SandboxLimits
from .tools.catalog import build_registry
from .tools.registry import ToolRegistry


@dataclass
class Services:
    settings: Settings
    db: Database
    audit: AuditLog
    state: StateMachine
    knowledge: KnowledgeStore
    router: Router
    notifier: Notifier
    approvals: Approvals
    policy: PolicyEngine
    tools: ToolRegistry
    sandbox: Sandbox
    url_policy: URLPolicy
    fetcher: Fetcher
    searx_fetcher: Fetcher
    research: object = field(default=None)  # ResearchEngine
    controller: object = field(default=None)  # Controller


def build_services(settings: Settings, *, providers: dict[str, ModelProvider] | None = None,
                   http_transport: httpx.BaseTransport | None = None, sandbox: Sandbox | None = None,
                   migrate: bool = True, fetch_sleep=None) -> Services:
    settings.ensure_dirs()
    configure_redactor(settings.secret_values())
    db = Database(settings.db_path, settings.migrations_dir)
    if migrate:
        db.migrate()
    notifier = Notifier.from_settings(db, settings)
    router = Router(settings, db, providers=providers,
                    notify=lambda level, kind, msg: notifier.notify(level, kind, msg))
    url_policy = URLPolicy(settings.research_allowed_domains, settings.research_blocked_domains)
    fkw = dict(max_bytes=settings.research_max_bytes, connect_timeout=settings.research_connect_timeout,
               read_timeout=settings.research_read_timeout, max_redirects=settings.research_max_redirects,
               user_agent=settings.research_user_agent, per_domain_delay=settings.research_per_domain_delay_seconds,
               transport=http_transport)
    if fetch_sleep is not None:
        fkw["sleep"] = fetch_sleep
    fetcher = Fetcher(url_policy, **fkw)
    # The search backend is user-configured (often on the LAN): it gets a policy that
    # permits exactly that one origin, and a plain transport (no public-IP requirement).
    searx_policy = PinnedOriginPolicy(settings.searxng_url or "https://invalid.invalid")
    searx_kw = {**fkw, "transport": http_transport or httpx.HTTPTransport()}
    searx_fetcher = Fetcher(searx_policy, respect_robots=False, **searx_kw)
    sb = sandbox or Sandbox(
        backend=settings.sandbox_backend, allow_weak=settings.sandbox_allow_weak, python=settings.sandbox_python,
        limits=SandboxLimits(settings.sandbox_timeout_seconds, settings.sandbox_memory_mb,
                             settings.sandbox_max_processes, settings.sandbox_max_file_mb),
        workspace_root=settings.workspace)
    s = Services(
        settings=settings, db=db, audit=AuditLog(db), state=StateMachine(db), knowledge=KnowledgeStore(db),
        router=router, notifier=notifier, approvals=Approvals(db, settings.approval_ttl_minutes),
        policy=PolicyEngine(), tools=build_registry(), sandbox=sb, url_policy=url_policy, fetcher=fetcher,
        searx_fetcher=searx_fetcher,
    )
    from .agent.controller import Controller
    from .research.engine import ResearchEngine
    s.research = ResearchEngine(s)
    s.controller = Controller(s)
    return s
