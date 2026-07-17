"""Prepare the on-disk Hermes profile (``SOUL.md`` + ``config.yaml``).

This mirrors what the old ``bootstrap-agent.sh`` did before it tailed forever —
the difference is it now runs on service startup and the process then serves
HTTP instead of blocking on ``tail -f``.
"""
from pathlib import Path

from .config import Settings

SOUL_TEMPLATE = (
    "You are the {department} department Hermes agent for NAPlatform. "
    "Only use API-supplied AgentContext allow-lists.\n"
)

CONFIG_TEMPLATE = """security:
  redact_secrets: true
naplatform:
  department: "{department}"
  api_base_url: "{api_base_url}"
  hdfs_namenode: "{hdfs_namenode}"
  optional_tools:
    nemoclaw: true
    openshell: true
"""


def prepare_profile(settings: Settings) -> str:
    """Create ``<HERMES_HOME>/profiles/<PROFILE>/{SOUL.md,config.yaml}``.

    Returns the profile directory path. Idempotent — safe to call on every
    startup.
    """
    profile_dir = Path(settings.hermes_home) / "profiles" / settings.profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "SOUL.md").write_text(
        SOUL_TEMPLATE.format(department=settings.department), encoding="utf-8")
    (profile_dir / "config.yaml").write_text(
        CONFIG_TEMPLATE.format(department=settings.department,
                               api_base_url=settings.api_base_url,
                               hdfs_namenode=settings.hdfs_namenode),
        encoding="utf-8")
    return str(profile_dir)
