"""Phase 09/10: guard tests that keep the docs and Makefile in sync with the phase.

These are *meta* tests — they read the repository's docs and Makefile (not code)
and assert that the explicit phase upload/release options and the current phase
status are actually documented and wired up. They exist so a future change can't
silently drop the phase-workflow surface or forget to advance the roadmap, and so
the promise that ``main`` only moves at an explicit release stays enforceable.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

PHASE_UPLOAD_TARGETS = ("push-phase", "merge-phase-to-dev", "release-dev-to-main")
RESOURCE_SMOKE_TARGETS = ("smoke-resources", "smoke-all-dry-run", "smoke-all-routing")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"expected {rel} to exist at {path}"
    return path.read_text(encoding="utf-8")


# --- Makefile ---------------------------------------------------------------
def test_makefile_defines_phase_upload_targets():
    mk = _read("Makefile")
    for target in PHASE_UPLOAD_TARGETS:
        assert f"{target}:" in mk, f"Makefile missing target {target}"
    # The workflow is parameterised by PHASE_BRANCH.
    assert "PHASE_BRANCH" in mk


def test_makefile_defines_resource_smoke_targets():
    mk = _read("Makefile")
    for target in RESOURCE_SMOKE_TARGETS:
        assert f"{target}:" in mk, f"Makefile missing target {target}"
    assert "smoke_resources_e2e.py" in mk


def test_only_release_target_updates_main():
    """`main` must be touched by exactly one target: release-dev-to-main.

    We split the Makefile into target recipe blocks and assert the only block whose
    recipe checks out / merges into MAIN_BRANCH is `release-dev-to-main`.
    """
    mk = _read("Makefile")
    lines = mk.splitlines()
    current = None
    touchers = set()
    for line in lines:
        stripped = line.strip()
        # A target header looks like `name:` at column 0 (recipes are tab-indented).
        if line and not line[0].isspace() and ":" in line and not line.startswith("\t"):
            name = line.split(":", 1)[0].strip()
            # Skip variable assignments and .PHONY.
            if name and " " not in name and name != ".PHONY":
                current = name
        elif line.startswith("\t") and current:
            # Only git verbs that move a branch pointer count, not `@echo` mentions.
            if "$(MAIN_BRANCH)" in line and ("git checkout" in line
                                             or "git merge" in line
                                             or "git push" in line):
                touchers.add(current)
    assert touchers == {"release-dev-to-main"}, (
        f"only release-dev-to-main may update main, but these do: {sorted(touchers)}")


# --- ROADMAP ---------------------------------------------------------------
def test_roadmap_exists_and_tracks_phase_09():
    roadmap = _read("docs/ROADMAP.md")
    assert "Phase 09" in roadmap
    # Status vocabulary is present (completed / in progress / upcoming).
    lower = roadmap.lower()
    assert "in progress" in lower
    assert "completed" in lower
    assert "upcoming" in lower
    # main stays stable until the final release.
    assert "main" in lower and "stable" in lower
    # The upload/release options are documented in the roadmap too.
    for target in PHASE_UPLOAD_TARGETS:
        assert target in roadmap, f"ROADMAP missing upload option {target}"


def test_roadmap_lists_all_completed_phases_through_08():
    roadmap = _read("docs/ROADMAP.md")
    for n in range(0, 9):
        assert f"Phase {n:02d}" in roadmap or f"| {n:02d} " in roadmap, \
            f"ROADMAP missing Phase {n:02d}"


# --- README + container guide ----------------------------------------------
def test_readme_documents_phase_09_and_upload_options():
    readme = _read("README.md")
    assert "Phase 09" in readme
    assert "smoke-resources" in readme
    for target in PHASE_UPLOAD_TARGETS:
        assert target in readme, f"README missing upload option {target}"


def test_container_guide_documents_resource_smoke():
    guide = _read("docs/CONTAINER_GUIDE.md")
    assert "smoke_resources_e2e.py" in guide
    assert "Phase 09" in guide
    for target in PHASE_UPLOAD_TARGETS:
        assert target in guide, f"CONTAINER_GUIDE missing upload option {target}"


@pytest.mark.parametrize("rel", ["README.md", "docs/ROADMAP.md", "docs/CONTAINER_GUIDE.md"])
def test_docs_state_main_stays_stable(rel):
    text = _read(rel).lower()
    assert "main" in text
    # Each doc affirms main is not updated per-phase / stays stable until release.
    assert ("main untouched" in text or "leaves `main` unchanged" in text
            or "main은 건드리지 않는다" in text or "main 미변경" in text
            or "stable" in text)


# --- Phase 10: real Qdrant/Neo4j/HDFS backend adapters ----------------------
# The connection envs that select and configure the real backends. The default
# stays memory/dry-run, so these appearing in the docs must go with that note.
PHASE_10_BACKEND_ENVS = (
    "VECTOR_BACKEND", "GRAPH_BACKEND", "QDRANT_URL", "QDRANT_API_KEY",
    "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
)


def test_roadmap_tracks_phase_10_backends():
    roadmap = _read("docs/ROADMAP.md")
    assert "Phase 10" in roadmap
    # Phase 10 is the active/in-progress backend-adapter phase on this branch.
    assert "in progress" in roadmap.lower()
    for env in PHASE_10_BACKEND_ENVS:
        assert env in roadmap, f"ROADMAP missing Phase 10 backend env {env}"


@pytest.mark.parametrize("rel", ["README.md", "docs/ARCHITECTURE.md", "docs/CONTAINER_GUIDE.md"])
def test_docs_document_phase_10_backend_envs(rel):
    text = _read(rel)
    assert "Phase 10" in text, f"{rel} missing Phase 10 section"
    for env in PHASE_10_BACKEND_ENVS:
        assert env in text, f"{rel} missing Phase 10 backend env {env}"


@pytest.mark.parametrize("rel", ["README.md", "docs/ROADMAP.md", "docs/CONTAINER_GUIDE.md"])
def test_docs_document_backends_status_endpoint(rel):
    assert "/admin/backends/status" in _read(rel), \
        f"{rel} missing the backend status endpoint"


@pytest.mark.parametrize("rel", ["README.md", "docs/ARCHITECTURE.md", "docs/CONTAINER_GUIDE.md"])
def test_docs_note_memory_default_and_no_live_backend(rel):
    text = _read(rel).lower()
    # The default backend is memory/dry-run and needs no live Qdrant/Neo4j/HDFS.
    assert "memory" in text
    assert ("no live" in text or "live qdrant/neo4j/hdfs" in text
            or "live 백엔드" in text or "live qdrant" in text)
