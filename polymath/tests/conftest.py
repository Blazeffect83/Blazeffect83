from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from polymath.core.config import Config, load_config
from polymath.core.db import Database, open_database

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_config(tmp_path: Path) -> Config:
    cfg_file = tmp_path / "polymath.toml"
    cfg_file.write_text(
        f"""
[paths]
data_dir = "{tmp_path / "data"}"
require_separate_mount = false

[loop]
idle_sleep = 0.05
planners = false
retry_base = 0.01

[senses]
default_feeds = false
default_seeds = false

[body]
thermal_zone = "{tmp_path / "thermal"}"
""",
        encoding="utf-8",
    )
    return load_config(cfg_file, env={})


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture()
def db(config: Config) -> Iterator[Database]:
    database = open_database(config.paths.db_path)
    yield database
    database.close()


@pytest.fixture()
def python() -> str:
    return sys.executable


@pytest.fixture(autouse=True)
def _no_storage_pool_leaks() -> Iterator[None]:
    """The process-wide storage pool (activated by build_agent) must not leak from one test into the next."""
    from polymath.memory import pool

    yield
    pool.deactivate()
