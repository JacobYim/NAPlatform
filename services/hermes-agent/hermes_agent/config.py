"""Service configuration loaded from the environment.

The container passes ``DEPARTMENT``/``HERMES_PROFILE``/``API_BASE_URL``/
``HDFS_NAMENODE`` (as before), plus the Phase 07 execution knobs. Everything is
read once into an immutable ``Settings`` so the request handlers and tests can
inject a specific configuration instead of reaching into ``os.environ``.
"""
import os
from dataclasses import dataclass
from pathlib import Path

_TRUTHY = ("1", "true", "yes", "on")


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class Settings:
    department: str
    profile: str
    api_base_url: str
    hdfs_namenode: str
    hermes_home: str
    hermes_bin: str
    execution_enabled: bool
    execution_timeout: float

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env
        try:
            timeout = float(env.get("HERMES_AGENT_EXECUTION_TIMEOUT_SECONDS", "60"))
        except (TypeError, ValueError):
            timeout = 60.0
        return cls(
            department=(env.get("DEPARTMENT") or "UNSET").strip().upper(),
            profile=(env.get("HERMES_PROFILE") or "UNSET").strip(),
            api_base_url=(env.get("API_BASE_URL") or "").strip(),
            hdfs_namenode=(env.get("HDFS_NAMENODE") or "").strip(),
            hermes_home=(env.get("HERMES_HOME") or str(Path.home() / ".hermes")).strip(),
            hermes_bin=(env.get("HERMES_BIN") or "hermes").strip(),
            execution_enabled=_as_bool(env.get("HERMES_AGENT_EXECUTION_ENABLED")),
            execution_timeout=timeout,
        )
