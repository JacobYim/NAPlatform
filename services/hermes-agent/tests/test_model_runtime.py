"""Phase 13: shared Docker Model Runner / gemma4:31b across all department agents.

These tests prove that when the LLM provider/model envs are set, every department
Hermes agent (ER/IT/EHS/QC) generates a profile ``config.yaml`` that points at the
*same* model (``gemma4:31b``) and the *same* OpenAI-compatible endpoint — i.e. no
agent-specific model drift — while its SOUL.md persona stays per-department. The
default (no envs) resolves to an unconfigured runtime, so the generated profile is
model-less and the stack stays dry-run safe. No live model runner is contacted;
resolution and profile text are pure functions.
"""
import pytest
from fastapi.testclient import TestClient

from hermes_agent.config import (LLM_API_KEY_ENV, ModelRuntime, Settings,
                                 resolve_model_runtime)
from hermes_agent.main import build_app
from hermes_agent.profile import build_config_yaml, prepare_profile

DEPARTMENTS = ("ER", "IT", "EHS", "QC")
MODEL = "gemma4:31b"
BASE_URL = "http://model-runner.docker.internal/engines/v1"


# --- env resolution -------------------------------------------------------
def _dmr_env(**over):
    env = {
        "HERMES_LLM_PROVIDER": "docker-model-runner",
        "DOCKER_MODEL_RUNNER_BASE_URL": BASE_URL,
        "DOCKER_MODEL_RUNNER_MODEL": MODEL,
    }
    env.update(over)
    return env


def test_default_env_is_unconfigured_and_safe():
    mr = resolve_model_runtime({})
    assert mr.configured is False
    assert mr.provider == "" and mr.base_url == "" and mr.model == ""
    assert mr.status()["configured"] is False
    assert mr.status()["model"] is None


def test_dmr_env_resolves_to_gemma():
    mr = resolve_model_runtime(_dmr_env())
    assert mr.configured is True
    assert mr.provider == "docker-model-runner"
    assert mr.model == MODEL
    assert mr.base_url == BASE_URL


@pytest.mark.parametrize("alias", ["docker_model_runner", "model-runner", "dmr", "MODELRUNNER"])
def test_provider_aliases_normalize_to_docker_model_runner(alias):
    mr = resolve_model_runtime(_dmr_env(HERMES_LLM_PROVIDER=alias))
    assert mr.provider == "docker-model-runner"


def test_openai_compatible_fallback_envs():
    env = {
        "LLM_PROVIDER": "openai",
        "OPENAI_BASE_URL": "https://oai.example.com/v1",
        "OPENAI_MODEL": MODEL,
    }
    mr = resolve_model_runtime(env)
    assert mr.configured is True
    assert mr.provider == "openai"
    assert mr.model == MODEL


def test_partial_config_is_not_configured():
    # provider + base but no model -> not configured (dry-run safe).
    mr = resolve_model_runtime({"HERMES_LLM_PROVIDER": "docker-model-runner",
                                "DOCKER_MODEL_RUNNER_BASE_URL": BASE_URL})
    assert mr.configured is False


# --- secret handling ------------------------------------------------------
def test_api_key_reported_as_boolean_never_value():
    secret = "sk-super-secret-key-value"
    mr = resolve_model_runtime(_dmr_env(**{LLM_API_KEY_ENV: secret}))
    assert mr.api_key_present is True
    assert mr.api_key_env == LLM_API_KEY_ENV
    blob = str(mr.status())
    assert secret not in blob  # the key value never appears in the status view


def test_base_url_userinfo_is_redacted():
    mr = resolve_model_runtime(_dmr_env(
        DOCKER_MODEL_RUNNER_BASE_URL="http://user:pw@model-runner.docker.internal/engines/v1"))
    status = mr.status()
    assert "pw" not in status["base_url"]
    assert "user" not in status["base_url"]
    assert "model-runner.docker.internal" in status["base_url"]


# --- profile generation: shared model, per-department persona -------------
def _settings(tmp_path, department, model_runtime):
    return Settings(
        department=department,
        profile=department,
        api_base_url="http://api:8080",
        hdfs_namenode="hdfs://hdfs-namenode:9000",
        hermes_home=str(tmp_path / ".hermes"),
        hermes_bin="hermes",
        execution_enabled=True,
        execution_timeout=5.0,
        model_runtime=model_runtime,
    )


def test_default_profile_has_no_llm_section(tmp_path):
    settings = _settings(tmp_path, "ER", ModelRuntime())
    text = build_config_yaml(settings)
    assert 'department: "ER"' in text
    assert "llm:" not in text  # model-less by default -> dry-run safe


def test_all_departments_share_gemma_no_drift(tmp_path):
    mr = resolve_model_runtime(_dmr_env())
    models_seen = set()
    for dep in DEPARTMENTS:
        settings = _settings(tmp_path, dep, mr)
        text = build_config_yaml(settings)
        # Department persona stays isolated...
        assert f'department: "{dep}"' in text
        # ...but the model + endpoint are identical across every agent.
        assert f'model: "{MODEL}"' in text
        assert f'base_url: "{BASE_URL}"' in text
        assert 'provider: "docker-model-runner"' in text
        assert "openai_compatible: true" in text
        # The key is referenced by env-var NAME, never its value.
        assert f'api_key_env: "{LLM_API_KEY_ENV}"' in text
        # Extract the model line to assert exactly one shared model value.
        for line in text.splitlines():
            if line.strip().startswith("model:"):
                models_seen.add(line.strip())
    assert models_seen == {f'model: "{MODEL}"'}, f"model drift across agents: {models_seen}"


def test_prepared_profile_files_written_per_department(tmp_path):
    mr = resolve_model_runtime(_dmr_env())
    for dep in DEPARTMENTS:
        settings = _settings(tmp_path, dep, mr)
        profile_dir = prepare_profile(settings)
        soul = (tmp_path / ".hermes" / "profiles" / dep / "SOUL.md").read_text(encoding="utf-8")
        config = (tmp_path / ".hermes" / "profiles" / dep / "config.yaml").read_text(encoding="utf-8")
        assert f"{dep} department Hermes agent" in soul  # persona isolation
        assert f'model: "{MODEL}"' in config              # shared model
        assert profile_dir.endswith(dep)


# --- health metadata (secret-free) ---------------------------------------
def test_health_reports_configured_model_runtime(tmp_path):
    mr = resolve_model_runtime(_dmr_env(**{LLM_API_KEY_ENV: "sk-secret"}))
    with TestClient(build_app(_settings(tmp_path, "QC", mr))) as c:
        body = c.get("/health").json()
        rt = body["model_runtime"]
        assert rt["configured"] is True
        assert rt["model"] == MODEL
        assert rt["provider"] == "docker-model-runner"
        assert rt["api_key_present"] is True
    assert "sk-secret" not in str(body)  # no secret leaks through /health


def test_health_default_runtime_is_unconfigured(tmp_path):
    with TestClient(build_app(_settings(tmp_path, "ER", ModelRuntime()))) as c:
        rt = c.get("/health").json()["model_runtime"]
        assert rt["configured"] is False
        assert rt["model"] is None
