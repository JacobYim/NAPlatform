"""Service configuration loaded from the environment.

The container passes ``DEPARTMENT``/``HERMES_PROFILE``/``API_BASE_URL``/
``HDFS_NAMENODE`` (as before), plus the Phase 07 execution knobs. Everything is
read once into an immutable ``Settings`` so the request handlers and tests can
inject a specific configuration instead of reaching into ``os.environ``.

Phase 13 adds a shared LLM/Docker Model Runner runtime (:class:`ModelRuntime`).
Every department agent resolves the *same* provider/base-URL/model from shared
envs (``HERMES_LLM_PROVIDER``, ``DOCKER_MODEL_RUNNER_BASE_URL``,
``DOCKER_MODEL_RUNNER_MODEL`` — OpenAI-compatible fallbacks accepted), so all
agents point at one model (``gemma4:31b``) while their department persona/profile
stays isolated. The default (no envs) resolves to an *unconfigured* runtime, so
the default stack stays dry-run safe.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_TRUTHY = ("1", "true", "yes", "on")

# Provider labels that all mean "Docker Model Runner" (OpenAI-compatible engine).
_DMR_PROVIDER_ALIASES = ("docker-model-runner", "docker_model_runner",
                         "model-runner", "modelrunner", "dmr")
# The env var that (optionally) holds an OpenAI-compatible API key. Docker Model
# Runner needs no real key, but OpenAI-compatible clients often pass one; we only
# ever record its *name* and presence — never its value — so no secret is written
# to the on-disk profile or echoed by /health.
LLM_API_KEY_ENV = "OPENAI_API_KEY"


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _redact_url(url: str) -> str:
    """Strip userinfo/query from a URL so it can be echoed safely."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable>"
    if not parsed.scheme:
        return url.split("?", 1)[0]
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    return f"{parsed.scheme}://{host}{port}{path}".rstrip("/") or f"{parsed.scheme}://"


@dataclass(frozen=True)
class ModelRuntime:
    """Shared LLM endpoint config resolved from the environment (secret-free).

    ``configured`` is true only when a provider **and** a base URL **and** a model
    are all present; otherwise the runtime is treated as unset and the profile
    stays model-less (dry-run safe). The API key is never stored here — only its
    env-var name and a presence boolean.
    """
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key_env: str = LLM_API_KEY_ENV
    api_key_present: bool = False
    openai_compatible: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.base_url and self.model)

    def status(self) -> dict:
        """Redacted, secret-free view for /health and admin status endpoints."""
        return {
            "provider": self.provider or None,
            "configured": self.configured,
            "base_url": _redact_url(self.base_url) or None,
            "model": self.model or None,
            "openai_compatible": self.openai_compatible,
            "api_key_present": self.api_key_present,
        }


def resolve_model_runtime(env: dict | None = None) -> ModelRuntime:
    """Resolve the shared model runtime from env, normalizing DMR aliases.

    Docker Model Runner envs win; OpenAI-compatible envs are accepted as a
    fallback so a plain OpenAI-compatible endpoint also works. An unset provider
    (or a missing base URL/model) yields an unconfigured, dry-run-safe runtime.
    """
    env = os.environ if env is None else env
    provider = (env.get("HERMES_LLM_PROVIDER") or env.get("LLM_PROVIDER") or "").strip().lower()
    if provider in _DMR_PROVIDER_ALIASES:
        provider = "docker-model-runner"
    base_url = (env.get("DOCKER_MODEL_RUNNER_BASE_URL")
                or env.get("OPENAI_BASE_URL") or "").strip()
    model = (env.get("DOCKER_MODEL_RUNNER_MODEL")
             or env.get("OPENAI_MODEL") or "").strip()
    return ModelRuntime(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=LLM_API_KEY_ENV,
        api_key_present=bool((env.get(LLM_API_KEY_ENV) or "").strip()),
    )


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
    model_runtime: ModelRuntime = field(default_factory=ModelRuntime)

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
            model_runtime=resolve_model_runtime(env),
        )
