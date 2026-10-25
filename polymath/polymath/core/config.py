"""Typed configuration loaded from TOML (stdlib ``tomllib``).

Precedence: built-in defaults → ``/etc/polymath/polymath.toml`` (or the file
named by ``$POLYMATH_CONFIG`` / ``--config``) → the drop-ins next to it
(``/etc/polymath/polymath.d/*.toml``, in name order; ``scripts/sd-card-mode.sh``
writes one, so ``polymath.toml`` itself is never edited) → ``POLYMATH_DATA_DIR`` for the
data directory. Unknown keys are rejected so a typo never silently does
nothing; keys of retired features (``RETIRED_KEYS``) are ignored with a warning,
so an upgrade never breaks a configuration file written for an older version.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tomllib
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/polymath/polymath.toml")

# Settings of features that were removed: accepted (and ignored) so older configuration files keep working.
RETIRED_KEYS: dict[str, frozenset[str]] = {
    "body": frozenset({"minecraft_host", "minecraft_port", "minecraft_check", "minecraft_poll_seconds"}),
}


class ConfigError(ValueError):
    """Raised for invalid configuration files or values."""


ACCESS_NETWORKS = (  # private and link-local ranges: the home network (interface.access)
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
)


@dataclass
class PathsConfig:
    data_dir: Path = Path("/srv/polymath")
    # Refuse to start when data_dir sits on the root filesystem (the SD card).
    require_separate_mount: bool = True
    # Memory-backed and readable by everyone: the agent's live snapshot for the feed (never the SD card, no wear)
    run_dir: Path = Path("/run/polymath")

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
    # SD card wear guard (body.wear): an SD card wears out from writes. Its rated life is estimated as
    # capacity × sd_endurance_cycles full-card writes, divided by sd_write_amplification (the card's own
    # extra internal writes for small random writes). Writes are budgeted to last sd_target_years.
    sd_endurance_cycles: float = 1000.0
    sd_write_amplification: float = 3.0
    sd_target_years: float = 5.0
    wal_autocheckpoint: int = 4000  # SQLite pages (4 kB) in the WAL before it is written back to the database


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
    # a second Wikipedia whose infoboxes and tables cross-check facts ("es", "de", "fr", "it", "pt"; "" for none)
    second_language: str = ""
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
    # Open-web learning: sites cited by Wikipedia, each vetted before its first visit (senses.openweb).
    open_web: bool = True
    open_web_min_citations: int = 3  # cited by at least this many Wikipedia articles
    open_web_trusted_citations: int = 50  # this many citations: approved outright (no probation)
    open_web_sites_per_day: int = 200  # new sites vetted per day at most
    open_web_probation_pages: int = 20  # pages read from a site on probation before its facts are judged
    blocklists: list[str] = field(
        default_factory=lambda: [
            "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/fakenews-gambling-porn/hosts",
            "https://urlhaus.abuse.ch/downloads/hostfile/",
            "https://dsi.ut-capitole.fr/blacklists/download/adult.tar.gz",
            "https://dsi.ut-capitole.fr/blacklists/download/phishing.tar.gz",
            "https://dsi.ut-capitole.fr/blacklists/download/malware.tar.gz",
            "https://dsi.ut-capitole.fr/blacklists/download/gambling.tar.gz",
        ]
    )


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
    digest_hour: int = 7  # local hour of the daily "what I learned today" digest
    relearn_delay_hours: float = 3.0  # read about a wrong answer, then re-test after this long
    relearn_attempts: int = 3


@dataclass
class AgentsConfig:
    max_agents: int = 12  # user-created + evolved agents, active at once
    evolve: bool = True  # strong agents fork mutated children; weak evolved children retire
    step_interval: float = 120.0  # seconds between agent society slices
    min_tasks_to_judge: int = 30  # verified tasks before an agent can be forked or retired
    # Orin the caretaker (agents/caretaker.py): watches Polymath and the Pi, fixes what it may, tells you the rest
    caretaker: bool = True


@dataclass
class StorageConfig:
    """The storage pool: drives plugged in later become extra brain space (see ``memory.pool``)."""

    mount_root: Path = Path("/mnt/polymath")  # where plugged-in drives are mounted
    adopt: bool = True  # add every drive that is plugged in (except the system disk and ignored ones)
    format_blank_disks: bool = True  # a drive with nothing at all on it is formatted (ext4, label POLYMATH)
    # a fresh shop drive (exFAT/NTFS/FAT, ≥ home_min_gb, only the maker's files) is formatted, once per drive ever
    format_fresh_drives: bool = True
    shared_drive_share: float = 0.5  # on a drive that already holds files: this share of its free space
    reserve_fraction: float = 0.1  # always leave this share of every drive free
    spill_at: float = 0.75  # move document bodies to drives once the main disk is this full (of its budget)
    ignore: list[str] = field(default_factory=list)  # filesystem UUIDs never to adopt
    allow_virtual: bool = False  # adopt loop devices too (testing only)
    # Move the whole brain (database, indexes, downloads) from the SD card onto a dedicated drive when one
    # with room is plugged in, so the SD card only boots the Pi (see body.volumes.Helper.move_home).
    move_home: bool = True
    home_min_gb: float = 32.0  # a drive needs at least this much room (and 3× the brain) to become home
    # Bring the brain's drive back by itself when it is missing or dead (`polymath storage heal`, every minute):
    # drop a dead mount, soft-reset a USB drive that reports 0 bytes, and as a last resort cut the USB ports'
    # power for a moment (a replug; for enclosures with a capacitor that do not power-cycle on a reboot)
    heal: bool = True
    heal_power_cycle: bool = True
    heal_hubs: list[str] = field(default_factory=lambda: ["2", "1-1"])  # Pi 4: USB 3 hub, then the USB 2 hub
    heal_power_off_s: float = 20.0  # long enough for a capacitor-backed enclosure (DockCase) to really switch off
    # Standby brain (core.standby): when the brain's drive has been missing, frozen or dead this long, keep learning
    # on a small, separate brain on the SD card; everything it reads moves to the real brain when the drive is back
    standby: bool = True
    standby_after_s: float = 300.0
    standby_budget_gb: float = 3.0
    standby_dir: Path = Path("/var/lib/polymath/standby")
    # SD-card mode (core.sdcard): the whole brain runs on the SD card, the SSD and every other drive are left alone.
    # For when the brain's drive cannot be trusted; switched on and off by scripts/sd-card-mode.sh (a drop-in).
    # What it learns there moves onto the drive by itself once the mode is off and the drive is back.
    sd_only: bool = False
    sd_dir: Path = Path("/var/lib/polymath/brain")
    sd_budget_gb: float = 0.0  # 0: everything the card can give (what the brain uses + what is free − sd_keep_free_gb)
    sd_keep_free_gb: float = 3.0  # the SD card keeps this much free (the system, and a card that is not full lasts)


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"  # the Pi's own, sign-in-free listener: always loopback (other devices use [access])
    port: int = 8765


@dataclass
class AccessConfig:
    """Private access from your own browsers on the home network (``interface.access``)."""

    remote: bool = True  # serve https://<pi>:<port> to paired devices on the home network
    port: int = 8443
    networks: list[str] = field(default_factory=lambda: list(ACCESS_NETWORKS))  # who may even connect
    names: list[str] = field(default_factory=list)  # extra host names the Pi answers to (besides its own)
    state_dir: Path = Path("/var/lib/polymath/access")  # paired devices, the pairing code, the access log
    tls_dir: Path = Path("/etc/polymath/tls")  # ca.crt, server.crt, server.key (scripts/access-cert.sh)


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    body: BodyConfig = field(default_factory=BodyConfig)
    senses: SensesConfig = field(default_factory=SensesConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    log_level: str = "INFO"
    # set by Polymath itself while it runs on the standby brain (core.standby), never in polymath.toml
    standby: bool = False

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
        if self.senses.second_language not in {"", "es", "de", "fr", "it", "pt"}:
            raise ConfigError("senses.second_language must be one of es, de, fr, it, pt (or empty)")
        if self.senses.second_language == self.senses.wikipedia_lang:
            raise ConfigError("senses.second_language must differ from senses.wikipedia_lang")
        if not 1 <= self.agents.max_agents <= 100:
            raise ConfigError("agents.max_agents must be between 1 and 100")
        if not 0 <= self.learning.digest_hour <= 23:
            raise ConfigError("learning.digest_hour must be 0..23")
        if self.senses.open_web_min_citations < 1:
            raise ConfigError("senses.open_web_min_citations must be at least 1")
        if not 0 < self.storage.shared_drive_share <= 1:
            raise ConfigError("storage.shared_drive_share must be in (0, 1]")
        if not 0 <= self.storage.reserve_fraction < 0.5:
            raise ConfigError("storage.reserve_fraction must be in [0, 0.5)")
        if not 0.1 <= self.storage.spill_at <= 1:
            raise ConfigError("storage.spill_at must be in [0.1, 1]")
        if self.body.sd_endurance_cycles <= 0 or self.body.sd_write_amplification < 1:
            raise ConfigError("body.sd_endurance_cycles must be > 0 and body.sd_write_amplification >= 1")
        if self.storage.standby_after_s < 30:
            raise ConfigError("storage.standby_after_s must be at least 30 seconds")
        if not 0.5 <= self.storage.standby_budget_gb <= 50:
            raise ConfigError("storage.standby_budget_gb must be between 0.5 and 50")
        if not 1 <= self.access.port <= 65535 or self.access.port == self.dashboard.port:
            raise ConfigError("access.port must be a free port other than dashboard.port")
        try:
            import ipaddress

            for net in self.access.networks:
                n = ipaddress.ip_network(net, strict=False)
                if n.prefixlen == 0:
                    raise ConfigError(f"access.networks: {net} would let the whole internet in")
        except ValueError as exc:
            raise ConfigError(f"access.networks: {exc}") from exc
        if self.storage.sd_budget_gb < 0:
            raise ConfigError("storage.sd_budget_gb must be >= 0 (0: sized to the SD card)")
        if not 0.5 <= self.storage.sd_keep_free_gb <= 50:
            raise ConfigError("storage.sd_keep_free_gb must be between 0.5 and 50")
        if self.body.sd_target_years <= 0:
            raise ConfigError("body.sd_target_years must be > 0")
        if not 100 <= self.body.wal_autocheckpoint <= 100_000:
            raise ConfigError("body.wal_autocheckpoint must be between 100 and 100000 pages")
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
    retired = set(data) & RETIRED_KEYS.get(where, frozenset()) - known
    if retired:
        logging.getLogger("polymath.config").warning(
            "ignoring retired configuration key(s) %s in [%s]", sorted(retired), where
        )
        data = {k: v for k, v in data.items() if k not in retired}
    unknown = set(data) - known
    if not where and "standby" in data:
        unknown.add("standby")  # set by Polymath itself, never by the file
    if unknown:
        raise ConfigError(f"{where or 'config'}: unknown key(s) {sorted(unknown)}")
    obj = cls()
    for name, value in data.items():
        setattr(obj, name, _coerce(value, hints[name], f"{where}.{name}" if where else name))
    return obj


def dropin_dir(cfg_path: Path) -> Path:
    """Where the drop-ins of a configuration file live: ``/etc/polymath/polymath.d`` for ``polymath.toml``."""
    return cfg_path.with_name(cfg_path.stem + ".d")


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """``extra`` over ``base``: tables are merged key by key, anything else is replaced."""
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def read_dropins(cfg_path: Path) -> tuple[dict[str, Any], list[Path]]:
    """The drop-ins of ``cfg_path`` merged in name order, and which files they were."""
    merged: dict[str, Any] = {}
    files = sorted(dropin_dir(cfg_path).glob("*.toml")) if dropin_dir(cfg_path).is_dir() else []
    for f in files:
        try:
            merged = _merge(merged, tomllib.loads(f.read_text(encoding="utf-8")))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"{f}: {exc}") from exc
    return merged, files


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
    extra, _files = read_dropins(cfg_path)
    data = _merge(data, extra)
    cfg: Config = _build(Config, data, "")
    apply_sd_only(cfg)
    if env.get("POLYMATH_DATA_DIR"):
        cfg.paths.data_dir = Path(env["POLYMATH_DATA_DIR"])
    if env.get("POLYMATH_RUN_DIR"):
        cfg.paths.run_dir = Path(env["POLYMATH_RUN_DIR"])
    if not cfg.storage.sd_only:
        apply_home(cfg)  # home-drive.json lives on the drive: never looked for in SD-card mode
        apply_capacity(cfg, data)
    elif cfg.storage.sd_budget_gb <= 0:
        cfg.body.disk_budget_gb = sd_budget(cfg)
    cfg.validate()
    return cfg


SD_BACKUPS = 1  # backups kept in SD-card mode: each is a GB or more on the card the brain itself lives on
SD_BACKUP_EVERY_DAYS = 7  # SD-card mode backs up weekly: each backup writes the whole brain twice to the card


def apply_sd_only(cfg: Config) -> None:
    """SD-card mode (``storage.sd_only``, see ``core.sdcard``): the brain lives in ``storage.sd_dir`` on the SD card
    and nothing touches a drive. No pool, no moving or mounting the brain's drive, no healing it, no standby brain
    (this *is* the brain), one backup a week, and the card keeps ``sd_keep_free_gb`` free instead of the drive's
    10 GB."""
    st = cfg.storage
    if not st.sd_only:
        return
    cfg.paths.data_dir = Path(st.sd_dir)
    cfg.paths.require_separate_mount = False  # on the SD card on purpose
    st.adopt = st.move_home = st.heal = st.heal_power_cycle = st.standby = False
    st.format_blank_disks = st.format_fresh_drives = False
    st.mount_root = Path(st.sd_dir) / "no-drives"  # the pool sees no drive, even one still mounted elsewhere
    cfg.body.disk_min_free_gb = min(cfg.body.disk_min_free_gb, st.sd_keep_free_gb)
    cfg.body.backup_keep = min(cfg.body.backup_keep, SD_BACKUPS)
    if st.sd_budget_gb > 0:
        cfg.body.disk_budget_gb = st.sd_budget_gb


def sd_budget(cfg: Config) -> float:
    """Room for the brain on the SD card: what it uses now plus what is free, less ``sd_keep_free_gb``.

    Constant while the brain grows (what it writes comes out of the free space), so the guard and eviction keep the
    card from filling up. At least 1 GB. Only the SD card is looked at (the brain's own directory): never a drive."""
    import shutil

    data = Path(cfg.paths.data_dir)
    probe = data
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return 1.0
    used = 0
    for root, _dirs, files in os.walk(data):
        for name in files:
            try:
                used += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return round(max(1.0, (used + free) / 1e9 - cfg.storage.sd_keep_free_gb), 1)


# Big-brain mode: with a large drive (its budget at least BIG_BRAIN_GB) it reads and keeps much more. Only settings
# left at their defaults are raised; anything set in polymath.toml stays as set.
BIG_BRAIN_GB = 400.0
BIG_BRAIN: dict[tuple[str, str], Any] = {
    ("learning", "embedding_vocab_max"): 400_000,
    ("senses", "openalex_files"): 40,
    ("senses", "pubmed_files"): 40,
    ("senses", "gutenberg_books"): 3000,
    ("senses", "stackexchange_sites"): [
        "ai",
        "datascience",
        "cs",
        "physics",
        "chemistry",
        "biology",
        "astronomy",
        "history",
        "earthscience",
        "diy",
        "cooking",
        "gardening",
        "travel",
        "english",
        "philosophy",
    ],
    ("senses", "second_language"): "es",
}


def brain_tier(cfg: Config) -> str:
    return "big" if cfg.body.disk_budget_gb >= BIG_BRAIN_GB else "normal"


def apply_capacity(cfg: Config, data: dict[str, Any] | None = None) -> list[str]:
    """Raise default settings in big-brain mode; returns the settings changed ("section.key")."""
    if brain_tier(cfg) != "big":
        return []
    changed = []
    for (section, key), value in BIG_BRAIN.items():
        if key in ((data or {}).get(section) or {}):
            continue  # set by the user
        part = getattr(cfg, section)
        if getattr(part, key) != value:
            setattr(part, key, value)
            changed.append(f"{section}.{key}")
    return changed


HOME_FILE = "home-drive.json"  # in data_dir when the brain lives on a drive (written by the storage helper)
MOVED_FILE = "MOVED-TO-DRIVE.json"  # left on the SD card's data directory after the brain moved away


def apply_home(cfg: Config) -> None:
    """When the brain lives on a drive, its budget is the drive's (not the SD card's small one). Asks the drive
    first with a time limit: every command loads the configuration, and a frozen drive must not hang them all."""
    from polymath.core import fsprobe

    home = cfg.paths.data_dir / HOME_FILE
    if fsprobe.probe(home, read=True, timeout=5.0) != fsprobe.OK:
        return
    try:
        data = json.loads(home.read_text(encoding="utf-8"))
        budget = float(data["budget_gb"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if budget > cfg.body.disk_budget_gb:
        cfg.body.disk_budget_gb = budget


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
