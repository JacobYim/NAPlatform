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

# Phase 13: appended only when a shared model runtime is configured. Every
# department writes the *same* provider/base_url/model here (Docker Model Runner /
# OpenAI-compatible), so all agents share one model while their SOUL.md persona
# stays per-department. The API key is referenced by env-var NAME (``api_key_env``)
# — never its value — so no secret is written to disk.
LLM_CONFIG_TEMPLATE = """llm:
  provider: "{provider}"
  base_url: "{base_url}"
  model: "{model}"
  openai_compatible: {openai_compatible}
  api_key_env: "{api_key_env}"
"""


def build_config_yaml(settings: Settings) -> str:
    """Render ``config.yaml`` text, appending the LLM block when configured.

    Kept as a pure function so tests can assert the generated text (shared model,
    no per-department drift) without touching the filesystem.
    """
    text = CONFIG_TEMPLATE.format(department=settings.department,
                                  api_base_url=settings.api_base_url,
                                  hdfs_namenode=settings.hdfs_namenode)
    mr = settings.model_runtime
    if mr.configured:
        text += LLM_CONFIG_TEMPLATE.format(
            provider=mr.provider,
            base_url=mr.base_url,
            model=mr.model,
            openai_compatible=str(mr.openai_compatible).lower(),
            api_key_env=mr.api_key_env)
    return text


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
        build_config_yaml(settings), encoding="utf-8")
    return str(profile_dir)
