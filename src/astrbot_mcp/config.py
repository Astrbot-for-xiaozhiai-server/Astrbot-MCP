"""Configuration loading for astrbot-mcp.

Priority (highest first):
1. CLI arguments (applied in ``__main__``)
2. ``--config`` path
3. ``ASTRBOT_MCP_CONFIG`` environment variable
4. ``./config.yaml``, ``./config.yml``
5. ``~/.config/astrbot-mcp/config.yaml``
6. Built-in defaults

String values support ``${VAR}`` and ``${VAR:-default}`` environment expansion.
Relative paths are resolved against the configuration file directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_INSTRUCTIONS = """你是一个多能力工具服务器，为 AstrBot 提供通用服务接入能力。
工具按服务分组：http_*（通用 HTTP）、web_*（搜索与抓取）、get_* / convert_*（天气、时间、汇率）、
fs_*（文件沙箱）、sqlite_*（数据库）、github_*（GitHub）、mail_*（邮件）、calendar_*（日历）、
kb_*（可自学习知识库）。调用前先确认参数，涉及写操作时格外谨慎。"""

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Raised when the configuration cannot be loaded or validated."""


def _expand_string(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        return os.environ.get(name, default if default is not None else "")

    return _ENV_PATTERN.sub(repl, value)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_string(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def resolve_config_path(explicit: str | Path | None = None) -> Path | None:
    """Return the configuration file to load, or ``None`` for defaults."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path

    env_path = os.environ.get("ASTRBOT_MCP_CONFIG", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"ASTRBOT_MCP_CONFIG points to a missing file: {path}")
        return path

    candidates = [
        Path.cwd() / "config.yaml",
        Path.cwd() / "config.yml",
        Path.home() / ".config" / "astrbot-mcp" / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class ServerConfig(BaseModel):
    name: str = "astrbot-mcp"
    instructions: str = DEFAULT_INSTRUCTIONS
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    sse_path: str = "/sse"
    message_path: str = "/messages/"
    streamable_http_path: str = "/mcp"
    mount_path: str = "/"
    stateless_http: bool = False
    json_response: bool = False
    auth_token: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("transport", mode="before")
    @classmethod
    def _normalize_transport(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("_", "-")
            if normalized in {"http", "streamable-http", "streamablehttp"}:
                return "streamable-http"
            if normalized == "stdio":
                return "stdio"
            if normalized == "sse":
                return "sse"
        return value

    @field_validator("sse_path", "message_path", "streamable_http_path")
    @classmethod
    def _ensure_leading_slash(cls, value: str) -> str:
        if value and not value.startswith("/"):
            return "/" + value
        return value


class SecurityConfig(BaseModel):
    allow_private_networks: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(
        default_factory=lambda: ["localhost", "metadata.google.internal"]
    )
    request_timeout: float = Field(default=30.0, gt=0)
    max_response_bytes: int = Field(default=262_144, gt=0)
    max_redirects: int = Field(default=5, ge=0, le=20)
    user_agent: str = "astrbot-mcp/0.1"


class HttpApiConfig(BaseModel):
    enabled: bool = True
    allow_private_networks: bool | None = None


class WebSearchConfig(BaseModel):
    enabled: bool = True
    provider: Literal["duckduckgo", "tavily", "bing", "searxng"] = "duckduckgo"
    api_key: str = ""
    base_url: str = ""
    max_results: int = Field(default=8, ge=1, le=50)
    timeout: float = Field(default=20.0, gt=0)


class PublicApisConfig(BaseModel):
    enabled: bool = True
    weather_language: str = "zh"


class FilesConfig(BaseModel):
    enabled: bool = True
    root: str = "./data/files"
    max_read_bytes: int = Field(default=1_048_576, gt=0)
    max_write_bytes: int = Field(default=5_242_880, gt=0)
    allow_write: bool = False
    allow_delete: bool = False


class SqliteConfig(BaseModel):
    enabled: bool = True
    root: str = "./data/sqlite"
    allow_write: bool = False
    max_rows: int = Field(default=200, ge=1, le=5000)


class GitHubConfig(BaseModel):
    enabled: bool = True
    token: str = ""
    base_url: str = "https://api.github.com"
    allow_write: bool = False
    per_page: int = Field(default=20, ge=1, le=100)


class MailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    username: str = ""
    password: str = ""
    from_addr: str = ""
    imap_host: str = ""
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_ssl: bool = True
    max_messages: int = Field(default=20, ge=1, le=200)
    timeout: float = Field(default=30.0, gt=0)


class CalendarConfig(BaseModel):
    enabled: bool = False
    root: str = "./data/calendar"
    default_calendar: str = "default"
    allow_write: bool = True


class EmbeddingConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "text-embedding-3-small"
    dimensions: int = Field(default=0, ge=0)
    timeout: float = Field(default=30.0, gt=0)


class KnowledgeConfig(BaseModel):
    enabled: bool = True
    db_path: str = "./data/knowledge.db"
    chunk_size: int = Field(default=800, ge=100, le=10_000)
    chunk_overlap: int = Field(default=100, ge=0)
    max_results: int = Field(default=8, ge=1, le=50)
    max_content_bytes: int = Field(default=2_000_000, gt=0)
    max_chunks_per_doc: int = Field(default=500, ge=1, le=10_000)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

    @model_validator(mode="after")
    def _check_chunking(self) -> KnowledgeConfig:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("knowledge.chunk_overlap must be smaller than chunk_size")
        return self


class ServicesConfig(BaseModel):
    http_api: HttpApiConfig = Field(default_factory=HttpApiConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    public_apis: PublicApisConfig = Field(default_factory=PublicApisConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    sqlite: SqliteConfig = Field(default_factory=SqliteConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        """Load configuration from YAML plus environment overrides."""
        config_path = resolve_config_path(path)
        raw: dict[str, Any] = {}
        base_dir = Path.cwd()

        if config_path is not None:
            base_dir = config_path.parent
            try:
                loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise ConfigError(f"Failed to read {config_path}: {exc}") from exc
            if loaded is not None and not isinstance(loaded, dict):
                raise ConfigError(f"Top-level YAML in {config_path} must be a mapping")
            raw = loaded or {}

        try:
            config = cls.model_validate(_expand_env(raw))
        except Exception as exc:  # pydantic ValidationError and friends
            raise ConfigError(f"Invalid configuration: {exc}") from exc

        # Environment overrides for the most common deployment knobs.
        env_transport = os.environ.get("ASTRBOT_MCP_TRANSPORT", "").strip()
        if env_transport:
            normalized = ServerConfig._normalize_transport(env_transport)
            if normalized not in {"stdio", "sse", "streamable-http"}:
                raise ConfigError(f"Invalid ASTRBOT_MCP_TRANSPORT: {env_transport}")
            config.server.transport = normalized  # type: ignore[assignment]
        env_host = os.environ.get("ASTRBOT_MCP_HOST", "").strip()
        if env_host:
            config.server.host = env_host
        env_port = os.environ.get("ASTRBOT_MCP_PORT", "").strip()
        if env_port:
            try:
                config.server.port = int(env_port)
            except ValueError as exc:
                raise ConfigError(f"Invalid ASTRBOT_MCP_PORT: {env_port}") from exc

        config._resolve_paths(base_dir)
        return config

    def _resolve_paths(self, base_dir: Path) -> None:
        def absolute(value: str) -> str:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = base_dir / path
            return str(path.resolve())

        self.services.files.root = absolute(self.services.files.root)
        self.services.sqlite.root = absolute(self.services.sqlite.root)
        self.services.calendar.root = absolute(self.services.calendar.root)
        self.services.knowledge.db_path = absolute(self.services.knowledge.db_path)

    def masked_dump(self) -> dict[str, Any]:
        """Dump the configuration with secrets replaced by ``***``."""
        return _mask_secrets(self.model_dump())

    def enabled_services(self) -> list[str]:
        names: list[str] = []
        for name in (
            "http_api",
            "web_search",
            "public_apis",
            "files",
            "sqlite",
            "github",
            "mail",
            "calendar",
            "knowledge",
        ):
            if getattr(self.services, name).enabled:
                names.append(name)
        return names


_SECRET_KEYS = {"token", "password", "api_key", "auth_token", "secret"}


def _mask_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if lowered in _SECRET_KEYS or lowered.endswith("_token"):
                masked[key] = "***" if item else ""
            else:
                masked[key] = _mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value
