"""Infraestrutura compartilhada do FyNex Quant AI (SQLite, .env, cache TTL e utilitários)."""

from core.infra.cache import CooldownTracker, TTLCache
from core.infra.env_store import (
    ENV_WRITE_LOCK,
    atomic_write_text,
    get_env_value,
    mask_secret,
    read_env_file,
    scrub_secrets,
    update_env_keys,
)
from core.infra.sqlite import (
    SCHEMA_REGISTRY,
    SchemaRegistry,
    connect_sqlite,
    ensure_column,
    sqlite_connection,
    sqlite_readonly_optional,
    table_exists,
)
from core.infra.utils import (
    finite_float,
    parse_utc_str,
    sanitize_token,
    tail_lines,
    to_float,
    utc_now_str,
)

__all__ = [
    "CooldownTracker",
    "TTLCache",
    "ENV_WRITE_LOCK",
    "atomic_write_text",
    "get_env_value",
    "mask_secret",
    "read_env_file",
    "scrub_secrets",
    "update_env_keys",
    "SCHEMA_REGISTRY",
    "SchemaRegistry",
    "connect_sqlite",
    "ensure_column",
    "sqlite_connection",
    "sqlite_readonly_optional",
    "table_exists",
    "finite_float",
    "parse_utc_str",
    "sanitize_token",
    "tail_lines",
    "to_float",
    "utc_now_str",
]
