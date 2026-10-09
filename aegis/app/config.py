"""Configuration loaded from environment variables and an optional .env file.

Precedence: real environment > .env file > defaults. Secrets are held only in
memory; ``Settings.redacted()`` is the only form that may be logged or shown.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

APP_ROOT = Path(__file__).resolve().parents[1]

SECRET_FIELDS = {
    "openai_api_key",
    "anthropic_api_key",
    "local_model_api_key",
    "session_secret",
    "telegram_bot_token",
    "smtp_password",
    "api_token_hash",
    "admin_password_hash",
    "brave_search_api_key",
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (no interpolation, no command execution)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]  # quoted: take literally up to the closing quote
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # strip inline comments
        values[key] = value
    return values


def _csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v and v.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseModel):
    # --- paths -------------------------------------------------------------
    data_dir: Path = Path("/var/lib/aegis")
    workspace_dir: Path | None = None  # default: <data_dir>/workspace
    backup_dir: Path | None = None  # default: <data_dir>/backups
    log_dir: Path | None = None  # default: <data_dir>/logs
    migrations_dir: Path = Path(__file__).resolve().parent / "migrations"

    # --- web ---------------------------------------------------------------
    bind_host: str = "127.0.0.1"
    bind_port: int = 8600
    session_secret: str = ""
    admin_username: str = "admin"
    admin_password_hash: str = ""
    api_token_hash: str = ""
    cookie_secure: bool = False
    session_ttl_hours: int = 12
    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])

    # --- models ------------------------------------------------------------
    model_provider: Literal["anthropic", "openai", "local", "mock", "none"] = "none"
    model_name: str = ""
    model_name_light: str = ""  # optional cheaper model for light roles
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    local_model_base_url: str = ""
    local_model_api_key: str = ""
    local_only: bool = False
    model_timeout_seconds: float = 120.0
    model_max_retries: int = 3
    # Explicitly authorised fallback provider (never implicit).
    fallback_provider: Literal["anthropic", "openai", "local", "none"] = "none"

    # --- budgets -----------------------------------------------------------
    daily_spend_limit_usd: float = 2.00
    spend_warning_fraction: float = 0.8
    max_tokens_per_objective: int = 200_000
    max_requests_per_hour: int = 120
    research_daily_spend_limit_usd: float = 0.50
    # "input_per_mtok:output_per_mtok" per model, e.g. "my-model=3:15"
    model_prices: list[str] = Field(default_factory=list)

    # --- agent -------------------------------------------------------------
    max_steps_per_objective: int = 40
    max_replans: int = 4
    max_step_attempts: int = 3
    objective_time_budget_minutes: int = 120
    worker_poll_seconds: float = 5.0
    concurrent_objectives: int = 1
    approval_ttl_minutes: int = 60

    # --- sandbox -----------------------------------------------------------
    sandbox_backend: Literal["auto", "bwrap", "rlimit", "disabled"] = "auto"
    sandbox_allow_weak: bool = False
    sandbox_timeout_seconds: int = 120
    sandbox_memory_mb: int = 512
    sandbox_max_processes: int = 128
    sandbox_max_file_mb: int = 50
    sandbox_python: str = ""  # default: interpreter running AEGIS
    package_index_url: str = "https://pypi.org/simple"

    # --- research ----------------------------------------------------------
    research_enabled: bool = True
    research_allowed_domains: list[str] = Field(default_factory=list)
    research_blocked_domains: list[str] = Field(default_factory=list)
    research_max_bytes: int = 3_000_000
    research_connect_timeout: float = 10.0
    research_read_timeout: float = 20.0
    research_max_redirects: int = 5
    research_user_agent: str = "AEGIS-Research/1.0 (+self-hosted research agent)"
    research_per_domain_delay_seconds: float = 2.0
    research_max_docs_per_run: int = 25
    searxng_url: str = ""  # optional self-hosted metasearch
    research_use_model_extraction: bool = False
    # Contact (e-mail or URL) sent to public APIs (OpenAlex "polite pool", User-Agent), per their policies.
    research_contact: str = ""
    brave_search_api_key: str = ""  # optional web-scale discovery for deep research
    primary_source_domains: list[str] = Field(
        default_factory=lambda: [
            "docs.python.org", "python.org", "kernel.org", "ubuntu.com",
            "raspberrypi.com", "docs.docker.com", "github.com", "arxiv.org",
            "nist.gov", "owasp.org", "debian.org", "sqlite.org",
            "fastapi.tiangolo.com", "docs.anthropic.com", "platform.openai.com",
        ]
    )

    # --- resources ---------------------------------------------------------
    max_cpu_temp_c: float = 78.0
    min_free_disk_mb: int = 2048
    max_memory_percent: float = 90.0
    max_load_per_cpu: float = 2.5

    # --- operations --------------------------------------------------------
    backup_interval_hours: int = 24
    backup_keep: int = 7
    log_max_mb: int = 10
    log_backups: int = 5
    metrics_interval_seconds: int = 60
    metrics_retention_days: int = 30
    tool_output_retention_days: int = 90
    approved_services: list[str] = Field(default_factory=list)
    approved_log_files: list[str] = Field(default_factory=list)
    approved_containers: list[str] = Field(default_factory=list)

    # --- notifications -----------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""

    @field_validator(
        "allowed_hosts", "research_allowed_domains", "research_blocked_domains",
        "model_prices", "primary_source_domains", "approved_services",
        "approved_log_files", "approved_containers", mode="before",
    )
    @classmethod
    def _split_csv(cls, v):  # noqa: N805
        return _csv(v)

    # --- derived -----------------------------------------------------------
    @property
    def workspace(self) -> Path:
        return (self.workspace_dir or self.data_dir / "workspace").resolve()

    @property
    def backups(self) -> Path:
        return (self.backup_dir or self.data_dir / "backups").resolve()

    @property
    def logs(self) -> Path:
        return (self.log_dir or self.data_dir / "logs").resolve()

    @property
    def db_path(self) -> Path:
        return (self.data_dir / "aegis.db").resolve()

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.workspace, self.backups, self.logs):
            p.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o750)
        except PermissionError:
            pass

    def secret_values(self) -> list[str]:
        out = []
        for name in SECRET_FIELDS:
            v = getattr(self, name, "")
            if isinstance(v, str) and len(v) >= 6:
                out.append(v)
        return out

    def redacted(self) -> dict:
        data = self.model_dump(mode="json")
        for name in SECRET_FIELDS:
            if data.get(name):
                data[name] = "***set***"
        return data

    def model_price(self, model: str) -> tuple[float, float] | None:
        for entry in self.model_prices:
            name, _, prices = entry.partition("=")
            if name.strip() == model:
                try:
                    inp, out = prices.split(":")
                    return float(inp), float(out)
                except ValueError:
                    return None
        return None


def load_settings(env_file: str | Path | None = None, **overrides) -> Settings:
    path = Path(env_file or os.environ.get("AEGIS_ENV_FILE", ".env"))
    raw: dict[str, str] = parse_env_file(path)
    for key, value in os.environ.items():
        raw[key] = value
    fields = Settings.model_fields
    values: dict = {}
    for key, value in raw.items():
        k = key.lower()
        if k.startswith("aegis_"):
            k = k[len("aegis_"):]
        if k in fields:
            values[k] = value
    values.update(overrides)
    return Settings(**values)
