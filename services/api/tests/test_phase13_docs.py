"""Phase 13: doc / compose / Makefile guards for the shared Docker Model Runner.

These *meta* tests read the repo (docs, compose files, Makefile) — not code — and
assert the Phase 13 surface is present and internally consistent: the model-runner
compose override exists and pins `gemma4:31b` for every agent, the Makefile wires
the new targets, the docs document the shared model / model-runner envs, and the
PowerShell runbook is genuine PowerShell (not Bash) that mentions `gemma4:31b`,
`docker-compose.model-runner.yml`, PowerShell syntax, and does **not** run
`release-dev-to-main` (so `main` stays untouched).
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

MODEL = "gemma4:31b"
COMPOSE_FILE = "docker-compose.model-runner.yml"
# Phase 15: the shared endpoint + model moved out of the compose file into a
# repo-controlled, non-secret env file loaded via `env_file:`.
MODEL_RUNNER_ENV_FILE = "config/model-runner/model-runner.env"
RUNBOOK = "docs/POWERSHELL_RUNBOOK.md"
MODEL_RUNNER_TARGETS = ("compose-config-model-runner", "up-model-runner", "smoke-model-runner")
MODEL_ENVS = ("HERMES_LLM_PROVIDER", "DOCKER_MODEL_RUNNER_BASE_URL", "DOCKER_MODEL_RUNNER_MODEL")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"expected {rel} at {path}"
    return path.read_text(encoding="utf-8")


# --- compose override + env file (Phase 15 refactor) ------------------------
def test_model_runner_env_file_pins_gemma_external_endpoint():
    """Phase 15: the shared endpoint + model live in a repo-controlled env file."""
    env_text = _read(MODEL_RUNNER_ENV_FILE)
    assert f"DOCKER_MODEL_RUNNER_MODEL={MODEL}" in env_text, \
        f"{MODEL_RUNNER_ENV_FILE} must pin DOCKER_MODEL_RUNNER_MODEL={MODEL}"
    assert "DOCKER_MODEL_RUNNER_BASE_URL=" in env_text, \
        f"{MODEL_RUNNER_ENV_FILE} must set an external DOCKER_MODEL_RUNNER_BASE_URL"
    # No secret belongs in this editable, non-secret file.
    low = env_text.lower()
    for marker in ('"password"', "password=", "api_key=", "sk-", "secret="):
        assert marker not in low, f"possible secret {marker!r} in {MODEL_RUNNER_ENV_FILE}"


def test_model_runner_compose_uses_env_file_and_no_embedded_service():
    """The override loads the env file on every service, maps the host gateway, and
    does NOT embed a model-runner service (the stack only connects to a runner)."""
    import yaml as _yaml
    mr = _yaml.safe_load(_read(COMPOSE_FILE))
    services = mr.get("services") or {}
    assert "model-runner" not in services and "model_runner" not in services, \
        "override must not embed a model-runner service (no runner started with the stack)"
    for svc in ("api", "hermes-er", "hermes-it", "hermes-ehs", "hermes-qc"):
        assert svc in services, f"{COMPOSE_FILE} missing service {svc}"
        s = services[svc]
        env_file = s.get("env_file") or []
        assert any(MODEL_RUNNER_ENV_FILE in str(e) for e in env_file), \
            f"{svc} must load {MODEL_RUNNER_ENV_FILE} via env_file"
        extra_hosts = s.get("extra_hosts") or []
        assert any("host.docker.internal:host-gateway" in str(h) for h in extra_hosts), \
            f"{svc} must map host.docker.internal:host-gateway"
        assert (s.get("environment") or {}).get("HERMES_LLM_PROVIDER") == "docker-model-runner", \
            f"{svc} must declare the shared docker-model-runner provider"


def test_model_runner_all_agents_share_model_from_config():
    """All agents + api share ONE gemma4:31b, sourced from the single env file
    (no per-agent model divergence)."""
    env_text = _read(MODEL_RUNNER_ENV_FILE)
    model_lines = [ln for ln in env_text.splitlines()
                   if ln.strip().startswith("DOCKER_MODEL_RUNNER_MODEL=")]
    assert len(model_lines) == 1, "the shared model must be declared exactly once in config"
    assert MODEL in model_lines[0], f"the single shared model must be {MODEL}"


def test_default_compose_stays_model_less():
    # The base compose must NOT hardwire the model runner (default stays dry-run safe).
    base = _read("docker-compose.yml")
    assert "DOCKER_MODEL_RUNNER_MODEL" not in base
    assert MODEL not in base


# --- Makefile ---------------------------------------------------------------
def test_makefile_defines_model_runner_targets():
    mk = _read("Makefile")
    for target in MODEL_RUNNER_TARGETS:
        assert f"{target}:" in mk, f"Makefile missing target {target}"
    assert COMPOSE_FILE in mk, f"Makefile must reference {COMPOSE_FILE}"


def test_only_release_target_updates_main_with_phase13_targets():
    """Re-assert the invariant with the Phase 13 targets present."""
    mk = _read("Makefile")
    current = None
    touchers = set()
    for line in mk.splitlines():
        if line and not line[0].isspace() and ":" in line and not line.startswith("\t"):
            name = line.split(":", 1)[0].strip()
            if name and " " not in name and name != ".PHONY":
                current = name
        elif line.startswith("\t") and current:
            if "$(MAIN_BRANCH)" in line and ("git checkout" in line
                                             or "git merge" in line
                                             or "git push" in line):
                touchers.add(current)
    assert touchers == {"release-dev-to-main"}


# --- PowerShell runbook -----------------------------------------------------
def test_runbook_mentions_gemma_and_compose_file():
    text = _read(RUNBOOK)
    assert MODEL in text, "runbook must mention gemma4:31b"
    assert COMPOSE_FILE in text, "runbook must mention docker-compose.model-runner.yml"


def test_runbook_uses_powershell_syntax_not_bash():
    text = _read(RUNBOOK)
    # Genuine PowerShell idioms present...
    assert "$env:" in text, "runbook must use PowerShell env-var syntax ($env:NAME)"
    assert "Invoke-RestMethod" in text, "runbook must use PowerShell HTTP calls"
    assert "PowerShell" in text
    # ...and it explicitly flags where Git Bash differs.
    assert "Git Bash" in text, "runbook must note that Git Bash commands differ"


def test_runbook_covers_required_flows():
    text = _read(RUNBOOK).lower()
    for needle in ("clone", "checkout", "dev", "release-check", "smoke",
                   "approval", "cleanup", "adapter"):
        assert needle in text, f"runbook missing the '{needle}' flow"
    # All three stacks are documented.
    assert "docker-compose.override.routing.yml" in _read(RUNBOOK)
    assert COMPOSE_FILE in _read(RUNBOOK)


def test_runbook_does_not_run_release_to_main():
    text = _read(RUNBOOK)
    assert "release-dev-to-main" in text, "runbook should reference the release step..."
    lower = text.lower()
    # ...only to state it is NOT run here / main stays untouched.
    assert ("never runs `release-dev-to-main`" in text
            or "no** `release-dev-to-main`" in text
            or "never run" in lower
            or "not run" in lower), "runbook must state release-dev-to-main is not run"
    assert ("main stays stable" in lower or "leaves `main`" in text.lower()
            or "main untouched" in lower or "stays stable" in lower)


# --- README / ARCHITECTURE / CONTAINER_GUIDE / ROADMAP ----------------------
@pytest.mark.parametrize("rel", ["README.md", "docs/ARCHITECTURE.md",
                                 "docs/CONTAINER_GUIDE.md", "docs/ROADMAP.md"])
def test_docs_document_phase_13(rel):
    text = _read(rel)
    assert "Phase 13" in text, f"{rel} missing Phase 13"
    assert MODEL in text, f"{rel} missing {MODEL}"
    assert COMPOSE_FILE in text, f"{rel} missing {COMPOSE_FILE}"


@pytest.mark.parametrize("rel", ["README.md", "docs/ARCHITECTURE.md", "docs/ROADMAP.md"])
def test_docs_document_model_runner_envs(rel):
    text = _read(rel)
    for env in MODEL_ENVS:
        assert env in text, f"{rel} missing model-runner env {env}"


@pytest.mark.parametrize("rel", ["README.md", "docs/ROADMAP.md", "docs/CONTAINER_GUIDE.md"])
def test_docs_reference_the_runbook(rel):
    assert "POWERSHELL_RUNBOOK" in _read(rel), f"{rel} should link the PowerShell runbook"


@pytest.mark.parametrize("rel", ["README.md", "docs/ARCHITECTURE.md",
                                 "docs/CONTAINER_GUIDE.md", "docs/ROADMAP.md"])
def test_docs_note_default_stays_safe(rel):
    text = _read(rel).lower()
    assert "dry-run" in text or "model-less" in text, \
        f"{rel} should note the default stays dry-run/model-less"


def test_roadmap_marks_phase_13_in_progress_and_main_stable():
    roadmap = _read("docs/ROADMAP.md")
    assert "Phase 13" in roadmap
    lower = roadmap.lower()
    assert "in progress" in lower
    assert "main" in lower and "stable" in lower
