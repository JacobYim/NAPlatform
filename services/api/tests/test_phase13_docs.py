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
RUNBOOK = "docs/POWERSHELL_RUNBOOK.md"
MODEL_RUNNER_TARGETS = ("compose-config-model-runner", "up-model-runner", "smoke-model-runner")
MODEL_ENVS = ("HERMES_LLM_PROVIDER", "DOCKER_MODEL_RUNNER_BASE_URL", "DOCKER_MODEL_RUNNER_MODEL")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"expected {rel} at {path}"
    return path.read_text(encoding="utf-8")


# --- compose override -------------------------------------------------------
def test_model_runner_compose_file_exists_and_pins_gemma():
    text = _read(COMPOSE_FILE)
    assert MODEL in text, f"{COMPOSE_FILE} must pin {MODEL}"
    for env in MODEL_ENVS:
        assert env in text, f"{COMPOSE_FILE} missing {env}"


def test_model_runner_compose_covers_all_agents_and_api():
    text = _read(COMPOSE_FILE)
    for svc in ("api:", "hermes-er:", "hermes-it:", "hermes-ehs:", "hermes-qc:"):
        assert svc in text, f"{COMPOSE_FILE} missing service {svc}"
    # The shared model is declared once per service — one value, no per-agent drift.
    occurrences = text.count(f"DOCKER_MODEL_RUNNER_MODEL:")
    assert occurrences >= 5, "expected the shared model env on api + all four agents"
    # Every model line uses the same gemma4:31b default (no divergent model names).
    model_lines = [ln for ln in text.splitlines() if "DOCKER_MODEL_RUNNER_MODEL:" in ln]
    assert model_lines and all(MODEL in ln for ln in model_lines), \
        f"all agents must share {MODEL}: {model_lines}"


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
