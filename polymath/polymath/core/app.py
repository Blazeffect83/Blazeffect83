"""Assembles the agent: database, job registry, services, planners, policy, body."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from polymath.body.sensors import same_filesystem_as_root
from polymath.body.systemd_notify import Notifier
from polymath.core.config import Config
from polymath.core.db import Database, open_database
from polymath.core.jobs import JobRegistry, noop_handler
from polymath.core.loop import Agent, Planner


class StorageError(RuntimeError):
    """The data directory is unsafe to use (e.g. on the SD card)."""


def check_storage(config: Config) -> None:
    data_dir = config.paths.data_dir
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


def build_components(config: Config, db: Database) -> Components:
    registry = JobRegistry()
    registry.register("noop", noop_handler, "No-op job used for loop acceptance tests")
    return Components(registry=registry)


def build_agent(config: Config, *, notifier: Notifier | None = None) -> Agent:
    check_storage(config)
    db = open_database(config.paths.db_path)
    comps = build_components(config, db)
    return Agent(
        config,
        db,
        comps.registry,
        notifier=notifier,
        services=comps.services,
        planners=comps.planners,
    )
