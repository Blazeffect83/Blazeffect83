"""Typed configuration loaded from TOML (stdlib ``tomllib``).

Precedence: built-in defaults → ``/etc/polymath/polymath.toml`` (or the file
named by ``$POLYMATH_CONFIG`` / ``--config``) → ``POLYMATH_DATA_DIR`` for the
data directory. Unknown keys are rejected so a typo never silently does
nothing.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/polymath/polymath.toml")


class ConfigError(ValueError):
    """Raised for invalid configuration files or values."""


@dataclass
class PathsConfig:
    data_dir: Path = Path("/srv/polymath")
    # Refuse to start when data_dir sits on the root filesystem (the SD card).
    require_separate_mount: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "polymath.sqlite3"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"


@dataclass
class LoopConfig:
    job_time_budget: float = 15.0  # seconds a single job slice may run
    idle_sleep: float = 2.0  # seconds to wait when there is nothing to do
    max_crashes: int = 3  # process deaths while running a job before it is dead-lettered
    retry_base: float = 30.0  # seconds; exponential backoff base for failed jobs
    retry_cap: float = 6 * 3600.0
    heartbeat_stale: float = 60.0
    planners: bool = True  # autonomous planning of source/learning work (off in tests)


@dataclass
class BodyConfig:
    thermal_zone: Path = Path("/sys/class/thermal/thermal_zone0/temp")
    throttle_celsius: float = 75.0
    pause_celsius: float = 82.0
    disk_budget_gb: float = 200.0
    disk_min_free_gb: float = 10.0
    backup_keep: int = 7
    backup_hour: int = 3  # local hour for the nightly backup
    minecraft_host: str = "127.0.0.1"
    minecraft_port: int = 25565
    minecraft_check: bool = True
    minecraft_poll_seconds: float = 60.0


@dataclass
class SensesConfig:
    user_agent: str = "PolymathBot/0.1 (+https://github.com/Blazeffect83; self-hosted learning agent)"
    contact: str = ""
    crawl_rate: float = 1.0  # max requests per second per host (hard ceiling 1.0)
    crawl_max_bytes: int = 5_000_000
    download_chunk: int = 1 << 20
    timeout: float = 30.0
    max_redirects: int = 5
    allow_private_networks: bool = False  # tests only; never enable in production
    wikipedia_lang: str = "en"
    wikipedia_parts: int = 1  # how many multistream part files to schedule
    wikidata_max_bytes: int = 0  # 0 = read the whole dump (it resumes across restarts)
    openalex_files: int = 2
    pubmed_files: int = 2
    gutenberg_books: int = 200
    stackexchange_sites: list[str] = field(default_factory=lambda: ["ai", "datascience", "cs"])
    feeds: list[str] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    crawl_allow_domains: list[str] = field(default_factory=list)
    default_feeds: bool = True  # with no feeds configured, follow the built-in list of public feeds
    default_seeds: bool = True  # with no seeds configured, crawl the built-in list of reference sites


@dataclass
class LearningConfig:
    embedding_dim: int = 128
    embedding_window: int = 5
    embedding_negatives: int = 5
    embedding_min_count: int = 5
    embedding_vocab_max: int = 200_000
    index_nlist: int = 0  # 0 → sqrt(N) chosen automatically
    index_nprobe: int = 32
    holdout_fraction: float = 0.05
    quiz_size: int = 50


@dataclass
class AgentsConfig:
    max_agents: int = 12  # user-created + evolved agents, active at once
    evolve: bool = True  # strong agents fork mutated children; weak evolved children retire
    step_interval: float = 120.0  # seconds between agent society slices
    min_tasks_to_judge: int = 30  # verified tasks before an agent can be forked or retired


@dataclass
class DashboardConfig:
    host: str = "0.0.0.0"
    port: int = 8765


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    body: BodyConfig = field(default_factory=BodyConfig)
    senses: SensesConfig = field(default_factory=SensesConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    log_level: str = "INFO"

    def validate(self) -> None:
        if self.loop.job_time_budget <= 0 or self.loop.job_time_budget > 90:
            raise ConfigError("loop.job_time_budget must be in (0, 90] to keep the watchdog fed")
        if not 0 < self.senses.crawl_rate <= 1.0:
            raise ConfigError("senses.crawl_rate must be in (0, 1] requests per second")
        if self.body.pause_celsius <= self.body.throttle_celsius:
            raise ConfigError("body.pause_celsius must exceed body.throttle_celsius")
        if not 1 <= self.body.backup_keep <= 365:
            raise ConfigError("body.backup_keep must be between 1 and 365")
        if self.learning.embedding_dim not in range(16, 513):
            raise ConfigError("learning.embedding_dim must be between 16 and 512")
        if not 1 <= self.agents.max_agents <= 100:
            raise ConfigError("agents.max_agents must be between 1 and 100")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError(f"unknown log_level {self.log_level!r}")


def _coerce(value: Any, tp: Any, where: str) -> Any:
    origin = typing.get_origin(tp)
    if tp is Path:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a path string")
        return Path(value)
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where}: expected a number")
        return float(value)
    if tp is int or tp is bool or tp is str:
        if type(value) is not tp:
            raise ConfigError(f"{where}: expected {tp.__name__}, got {type(value).__name__}")
        return value
    if origin is list:
        (item_tp,) = typing.get_args(tp)
        if not isinstance(value, list):
            raise ConfigError(f"{where}: expected a list")
        return [_coerce(v, item_tp, f"{where}[{i}]") for i, v in enumerate(value)]
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a table")
        return _build(tp, value, where)
    if origin in (typing.Union, types.UnionType):  # pragma: no cover - not used by the schema
        raise ConfigError(f"{where}: unsupported union type")
    raise ConfigError(f"{where}: unsupported type {tp!r}")  # pragma: no cover


def _build(cls: type[Any], data: dict[str, Any], where: str) -> Any:
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where or 'config'}: unknown key(s) {sorted(unknown)}")
    obj = cls()
    for name, value in data.items():
        setattr(obj, name, _coerce(value, hints[name], f"{where}.{name}" if where else name))
    return obj


def load_config(path: Path | str | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Load configuration. A missing default file yields built-in defaults."""
    env = dict(os.environ) if env is None else env
    explicit = path is not None or "POLYMATH_CONFIG" in env
    cfg_path = Path(path) if path is not None else Path(env.get("POLYMATH_CONFIG", DEFAULT_CONFIG_PATH))
    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg_path}: {exc}") from exc
    elif explicit:
        raise ConfigError(f"config file not found: {cfg_path}")
    cfg: Config = _build(Config, data, "")
    if env.get("POLYMATH_DATA_DIR"):
        cfg.paths.data_dir = Path(env["POLYMATH_DATA_DIR"])
    cfg.validate()
    return cfg


def to_dict(cfg: Config) -> dict[str, Any]:
    """Plain-dict view (paths as strings) for status output."""

    def conv(v: Any) -> Any:
        if isinstance(v, Path):
            return str(v)
        if isinstance(v, list):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        return v

    return typing.cast(dict[str, Any], conv(dataclasses.asdict(cfg)))
