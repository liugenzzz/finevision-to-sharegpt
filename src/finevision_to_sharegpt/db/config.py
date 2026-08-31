from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

ON_CONNECT_ERROR_CHOICES = ("fallback", "fail")


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"
    collation: str = "utf8mb4_0900_ai_ci"
    batch_size: int = 200
    flush_interval_seconds: float = 5.0
    claim_ttl_seconds: int = 3600
    on_connect_error: str = "fallback"
    connect_timeout: int = 10

    @property
    def fail_fast(self) -> bool:
        return self.on_connect_error == "fail"


def expand_env(value: Any) -> str:
    """Expand ``${VAR}`` references so secrets stay out of the config files."""

    text = str(value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        found = os.environ.get(name)
        if found is None:
            raise ValueError(f"environment variable {name} referenced by mysql config is not set")
        return found

    return _ENV_PATTERN.sub(replace, text)


def load_mysql_config(data: dict[str, Any] | None) -> MysqlConfig | None:
    """Build a :class:`MysqlConfig` from a task config's ``mysql`` section.

    Returns ``None`` when the section is absent, which keeps the caller in
    file mode.
    """

    if not data:
        return None
    if not isinstance(data, dict):
        raise ValueError("mysql config section must be an object")
    on_connect_error = str(data.get("on_connect_error", "fallback"))
    if on_connect_error not in ON_CONNECT_ERROR_CHOICES:
        raise ValueError(
            f"mysql.on_connect_error must be one of {ON_CONNECT_ERROR_CHOICES}, got {on_connect_error!r}"
        )
    for key in ("host", "user", "database"):
        if not data.get(key):
            raise ValueError(f"mysql config requires {key}")
    return MysqlConfig(
        host=expand_env(data["host"]),
        port=int(data.get("port", 3306)),
        user=expand_env(data["user"]),
        password=expand_env(data.get("password", "")),
        database=expand_env(data["database"]),
        charset=str(data.get("charset", "utf8mb4")),
        collation=str(data.get("collation", "utf8mb4_0900_ai_ci")),
        batch_size=max(1, int(data.get("batch_size", 200))),
        flush_interval_seconds=max(0.0, float(data.get("flush_interval_seconds", 5.0))),
        claim_ttl_seconds=max(1, int(data.get("claim_ttl_seconds", 3600))),
        on_connect_error=on_connect_error,
        connect_timeout=max(1, int(data.get("connect_timeout", 10))),
    )
