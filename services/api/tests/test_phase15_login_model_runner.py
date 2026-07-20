"""Phase 15: guards for (A) the core-webui login gate and (B) the env_file-driven
shared Docker Model Runner refactor.

These are *meta* tests — they read the repo (compose, config files, preseed script,
env file, docs, Makefile) and assert the Phase 15 surface is present and internally
consistent:

  (A) After the first-run setup screen is skipped, the UI REQUIRES LOGIN — it must
      not land directly in chat. webui-settings.json declares login_required /
      require_auth / start_page=login (and defaults.landing=login), the ui service
      wires HERMES_WEBUI_AUTH_MODE=naplatform plus the login env flags, and NO
      password is written into compose or any preseed file.

  (B) The shared endpoint + model (gemma4:31b) live in the repo-controlled,
      non-secret config/model-runner/model-runner.env. docker-compose.model-runner.yml
      loads it via env_file on the API + every agent, maps host.docker.internal via
      host-gateway, and embeds NO model-runner service (omitting the override => no
      model runner at all). The default docker-compose.yml stays model-less.
"""
import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

MODEL = "gemma4:31b"
COMPOSE = "docker-compose.yml"
MODEL_RUNNER_COMPOSE = "docker-compose.model-runner.yml"
MODEL_RUNNER_ENV = "config/model-runner/model-runner.env"
SETTINGS = "config/core-webui/webui-settings.json"
AGENTS = ("api", "hermes-er", "hermes-it", "hermes-ehs", "hermes-qc")

# No password / token / api key value may leak into any preseed / config file.
SECRET_MARKERS = ("changeme123", "naplatform-password", "sk-", "bearer ",
                  '"password"', "password:", '"api_key"', "api_key:",
                  '"secret"', "secret:")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"expected {rel} at {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(_read(COMPOSE))


@pytest.fixture(scope="module")
def model_runner_compose():
    return yaml.safe_load(_read(MODEL_RUNNER_COMPOSE))


def _parse_env(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip()
    return env


# ============================ (A) login required ============================
def test_settings_require_login_after_setup_skip():
    settings = json.loads(_read(SETTINGS))
    # setup screen still suppressed...
    assert settings["first_run"] is False
    assert settings["setup_completed"] is True
    # ...but login is now required (no direct-to-chat landing).
    assert settings["login_required"] is True, "settings must set login_required=true"
    assert settings["require_auth"] is True, "settings must set require_auth=true"
    assert settings["start_page"] == "login", "settings must set start_page=login"
    assert settings["defaults"]["landing"] == "login", \
        "landing must be 'login' (not 'workspace') so / does not open chat directly"


def test_settings_carry_no_password():
    """The login gate must be declared without embedding a secret in the JSON."""
    low = _read(SETTINGS).lower()
    for marker in SECRET_MARKERS:
        assert marker not in low, f"possible secret {marker!r} in {SETTINGS}"


def test_ui_service_wires_login_and_password(compose):
    env = compose["services"]["ui"].get("environment") or {}
    # Belt-and-suspenders login flags for versions reading the gate from env.
    assert env.get("HERMES_WEBUI_LOGIN_REQUIRED") in ("true", True)
    assert env.get("HERMES_WEBUI_REQUIRE_AUTH") in ("true", True)
    assert env.get("HERMES_WEBUI_START_PAGE") == "login"
    # First-run suppression is still present (skip setup, THEN require login).
    assert env.get("HERMES_WEBUI_SETUP_COMPLETED") in ("true", True)
    assert env.get("HERMES_WEBUI_DISABLE_FIRST_RUN") in ("true", True)
    # Phase 16: the WebUI gate delegates to NAPlatform accounts, so there must
    # be no password-only WebUI fallback configured in compose.
    assert env.get("HERMES_WEBUI_AUTH_MODE") == "naplatform"
    assert "HERMES_WEBUI_PASSWORD" not in env


def test_preseed_service_can_toggle_login_required(compose):
    env = compose["services"]["ui-preseed"].get("environment") or {}
    assert "CORE_WEBUI_LOGIN_REQUIRED" in env, \
        "ui-preseed must forward CORE_WEBUI_LOGIN_REQUIRED so login is repo/compose controlled"
    sh = _read("config/core-webui/preseed.sh")
    assert "CORE_WEBUI_LOGIN_REQUIRED" in sh and "login_required" in sh, \
        "preseed.sh must honor CORE_WEBUI_LOGIN_REQUIRED to flip login_required"
    # ...but the preseed still never writes a password.
    low = sh.lower()
    for marker in SECRET_MARKERS:
        assert marker not in low, f"possible secret {marker!r} in preseed.sh"


def test_config_readme_documents_login_and_password_override():
    readme = _read("config/core-webui/README.md")
    low = readme.lower()
    assert "login" in low and "/login" in readme, "README must document the login page behavior"
    assert "email + password" in readme, "README must document the email/password login form"
    assert "register" in low and "admin approval" in low, "README must document registration and approval"
    assert "HERMES_WEBUI_AUTH_MODE" in readme and "HERMES_WEBUI_PASSWORD" in readme
    assert "cannot fall back to the old password-only gate" in readme


# ====================== (B) model runner env_file refactor ==================
def test_model_runner_env_file_holds_shared_gemma_endpoint():
    env = _parse_env(_read(MODEL_RUNNER_ENV))
    assert env.get("DOCKER_MODEL_RUNNER_MODEL") == MODEL, \
        f"{MODEL_RUNNER_ENV} must pin the shared model {MODEL}"
    assert env.get("DOCKER_MODEL_RUNNER_BASE_URL"), \
        f"{MODEL_RUNNER_ENV} must set an external DOCKER_MODEL_RUNNER_BASE_URL"
    # non-secret editable file
    low = _read(MODEL_RUNNER_ENV).lower()
    for marker in ("password=", "api_key=", "sk-", "secret="):
        assert marker not in low, f"possible secret {marker!r} in {MODEL_RUNNER_ENV}"


def test_model_runner_override_uses_env_file_on_all_services(model_runner_compose):
    services = model_runner_compose.get("services") or {}
    for svc in AGENTS:
        assert svc in services, f"{MODEL_RUNNER_COMPOSE} missing {svc}"
        s = services[svc]
        assert any(MODEL_RUNNER_ENV in str(e) for e in (s.get("env_file") or [])), \
            f"{svc} must load the shared model-runner env_file (external endpoint)"


def test_model_runner_override_has_no_embedded_service(model_runner_compose):
    services = model_runner_compose.get("services") or {}
    assert "model-runner" not in services and "model_runner" not in services, \
        "no model-runner service by default — the stack must not start a runner itself"
    # Only the API + the four agents are touched by the override.
    assert set(services.keys()) == set(AGENTS), \
        f"override should only wire {AGENTS}, got {sorted(services)}"


def test_model_runner_override_maps_host_gateway(model_runner_compose):
    services = model_runner_compose.get("services") or {}
    for svc in AGENTS:
        eh = services[svc].get("extra_hosts") or []
        assert any("host.docker.internal:host-gateway" in str(h) for h in eh), \
            f"{svc} must map host.docker.internal:host-gateway so the host runner is reachable"


def test_all_agents_share_one_model_from_config():
    """Exactly one shared model line in the config => all agents share gemma4:31b."""
    lines = [ln for ln in _read(MODEL_RUNNER_ENV).splitlines()
             if ln.strip().startswith("DOCKER_MODEL_RUNNER_MODEL=")]
    assert len(lines) == 1 and MODEL in lines[0], \
        "the shared model must be defined exactly once (no per-agent drift)"


def test_default_stack_stays_model_less():
    base = _read(COMPOSE)
    assert "DOCKER_MODEL_RUNNER_MODEL" not in base
    assert MODEL not in base, "default compose must stay dry-run / model-less"


# ============================ docs coverage ================================
@pytest.mark.parametrize("rel", ["README.md", "docs/CONTAINER_GUIDE.md", "docs/ROADMAP.md"])
def test_docs_document_phase_15(rel):
    text = _read(rel)
    assert "Phase 15" in text, f"{rel} missing Phase 15"
    low = text.lower()
    assert "login" in low, f"{rel} must document the login requirement"
    assert MODEL_RUNNER_ENV in text, f"{rel} must reference {MODEL_RUNNER_ENV}"


@pytest.mark.parametrize("rel", ["README.md", "docs/CONTAINER_GUIDE.md",
                                 "docs/POWERSHELL_RUNBOOK.md", "docs/ROADMAP.md"])
def test_docs_document_password_override(rel):
    assert "CORE_WEBUI_PASSWORD" in _read(rel), f"{rel} must document CORE_WEBUI_PASSWORD"


@pytest.mark.parametrize("rel", ["README.md", "docs/CONTAINER_GUIDE.md", "docs/ROADMAP.md"])
def test_docs_document_two_model_runner_modes_and_omit(rel):
    low = _read(rel).lower()
    assert "external" in low, f"{rel} must document the external endpoint mode"
    assert "host.docker.internal" in low or "model-runner.docker.internal" in low, \
        f"{rel} must document the Docker Desktop local model runner mode"
    # Omitting the override => no model runner.
    assert "omit" in low or "without" in low or "don't pass" in low or "do not pass" in low, \
        f"{rel} must note omitting the override disables the model runner"
