"""Phase 14: guards for the core-webui first-run preseed (suppress the setup screen).

These are *meta* tests — they read the repo (compose, config files, preseed script,
docs, Makefile) and assert the Phase 14 surface is present and internally
consistent:

  * ``docker-compose.yml`` runs a one-shot ``ui-preseed`` service that mounts the
    repo-controlled ``config/core-webui`` (and its ``preseed.sh``) and shares the
    ``ui-hermes-home`` volume, and the ``ui`` service waits for it to complete;
  * the preseed config declares first-run-disabled semantics
    (``first_run`` false / ``setup_completed`` true), the API base URL, and HMGMA
    branding;
  * NO secret (password / token / api key value) lives in any preseed file;
  * the docs tell the user localhost:3000 shows **no setup screen**, in both
    PowerShell and Bash; and
  * the ``main`` guard still holds (only ``release-dev-to-main`` moves ``main``).
"""
import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

CONFIG_DIR = "config/core-webui"
COMPOSE_FILE = "docker-compose.yml"
PRESEED_SERVICE = "ui-preseed"
UI_SERVICE = "ui"
HERMES_HOME_VOLUME = "ui-hermes-home"

# Concrete secret-looking substrings that must never appear in the preseed files.
SECRET_MARKERS = ("changeme123", "naplatform-password", "sk-", "bearer ",
                  '"password"', "password:", '"api_key"', "api_key:",
                  '"secret"', "secret:")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"expected {rel} at {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(_read(COMPOSE_FILE))


# --- config files exist -----------------------------------------------------
@pytest.mark.parametrize("rel", [
    f"{CONFIG_DIR}/branding.yaml",
    f"{CONFIG_DIR}/webui-settings.json",
    f"{CONFIG_DIR}/preseed.sh",
    f"{CONFIG_DIR}/README.md",
])
def test_preseed_config_files_exist(rel):
    assert (REPO_ROOT / rel).exists(), f"missing {rel}"


# --- compose: ui-preseed service -------------------------------------------
def test_compose_defines_ui_preseed_service(compose):
    services = compose["services"]
    assert PRESEED_SERVICE in services, f"{COMPOSE_FILE} missing {PRESEED_SERVICE}"
    pre = services[PRESEED_SERVICE]
    cmd = pre.get("command")
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    assert "preseed.sh" in cmd_str, "ui-preseed must run preseed.sh"
    assert "tr -d" in cmd_str and "\r" in cmd_str, \
        "ui-preseed must strip CRLF so Windows/Git Bash checkouts work with BusyBox sh"
    assert "cd /preseed" in cmd_str, \
        "ui-preseed must avoid absolute script args that MSYS rewrites on Git Bash"


def test_compose_ui_preseed_mounts_config_and_shares_volume(compose):
    pre = compose["services"][PRESEED_SERVICE]
    vols = pre.get("volumes") or []
    assert any(v.startswith(f"{HERMES_HOME_VOLUME}:") for v in vols), \
        "ui-preseed must share the ui-hermes-home volume with the ui service"
    assert any(CONFIG_DIR in v for v in vols), \
        "ui-preseed must mount ./config/core-webui"
    assert any(":ro" in v for v in vols), \
        "ui-preseed must mount the repo config read-only"
    assert any("preseed.sh" in v for v in vols), \
        "ui-preseed must mount preseed.sh"


def test_compose_ui_waits_for_preseed_completion(compose):
    ui = compose["services"][UI_SERVICE]
    dep = ui.get("depends_on")
    assert isinstance(dep, dict), "ui.depends_on must use the long (condition) form"
    assert PRESEED_SERVICE in dep, "ui must depend on ui-preseed"
    assert dep[PRESEED_SERVICE].get("condition") == "service_completed_successfully", \
        "ui must wait for ui-preseed to complete successfully before serving"
    # ui still reads from the same shared volume the preseed wrote to.
    assert any(v.startswith(f"{HERMES_HOME_VOLUME}:") for v in (ui.get("volumes") or []))


def test_compose_ui_carries_first_run_suppression_env(compose):
    env = compose["services"][UI_SERVICE].get("environment") or {}
    # Belt-and-suspenders env flags (env override still possible at run time).
    assert env.get("HERMES_WEBUI_SETUP_COMPLETED") in ("true", True), \
        "ui must declare the setup-completed env flag"
    assert env.get("HERMES_WEBUI_DISABLE_FIRST_RUN") in ("true", True)
    # Branding + API base URL remain wired.
    assert env.get("BRAND_NAME") == "HMGMA"
    assert env.get("NAPLATFORM_API_BASE_URL") == "http://api:8080"


def test_compose_env_override_possible(compose):
    """The preseed service passes the overridable branding/API envs through."""
    env = compose["services"][PRESEED_SERVICE].get("environment") or {}
    for key in ("BRAND_NAME", "BRAND_LOGO", "NAPLATFORM_API_BASE_URL"):
        assert key in env, f"ui-preseed must forward the overridable env {key}"


# --- config semantics: first-run disabled ----------------------------------
def test_settings_disable_first_run_and_carry_endpoints():
    text = _read(f"{CONFIG_DIR}/webui-settings.json")
    settings = json.loads(text)
    assert settings["first_run"] is False, "settings must set first_run=false"
    assert settings["setup_completed"] is True, "settings must set setup_completed=true"
    assert settings["onboarding_completed"] is True
    assert settings["initial_setup_completed"] is True
    # API base URL + branding + auth adapter present.
    assert "http://api:8080" in text
    assert settings["branding"]["name"] == "HMGMA"
    assert settings["auth"]["module"].endswith("naplatform-adapter.js")


def test_branding_yaml_sets_hmgma():
    branding = yaml.safe_load(_read(f"{CONFIG_DIR}/branding.yaml"))
    assert branding["name"] == "HMGMA"
    assert branding["logo"] == "/apptoo/branding/logo.jpg"


def test_preseed_script_writes_markers_and_targets_state_dir():
    sh = _read(f"{CONFIG_DIR}/preseed.sh")
    # It targets the documented state dir + branding path.
    assert "HERMES_WEBUI_STATE_DIR" in sh
    assert "branding.yaml" in sh
    assert "settings.json" in sh
    # It writes setup-completed markers so the first-run screen stays suppressed.
    assert "setup_completed" in sh
    assert ".setup-complete" in sh
    # Env override honored inside the script.
    assert "BRAND_NAME" in sh and "NAPLATFORM_API_BASE_URL" in sh


# --- no secrets -------------------------------------------------------------
@pytest.mark.parametrize("rel", [
    f"{CONFIG_DIR}/branding.yaml",
    f"{CONFIG_DIR}/webui-settings.json",
    f"{CONFIG_DIR}/preseed.sh",
])
def test_no_secrets_in_preseed_files(rel):
    low = _read(rel).lower()
    for marker in SECRET_MARKERS:
        assert marker not in low, f"possible secret {marker!r} in {rel}"


# --- docs: localhost:3000 shows no setup screen -----------------------------
def test_config_readme_documents_no_setup_screen_powershell_and_bash():
    readme = _read(f"{CONFIG_DIR}/README.md")
    assert "localhost:3000" in readme
    low = readme.lower()
    assert "no setup screen" in low or "no first-run setup screen" in low or "no setup" in low
    # Both shells documented for edit + start.
    assert "PowerShell" in readme and "docker compose up" in readme
    assert "$env:" in readme  # PowerShell env override idiom
    assert "export BRAND_NAME" in readme  # Bash env override idiom


@pytest.mark.parametrize("rel", ["README.md", "docs/CONTAINER_GUIDE.md", "docs/ROADMAP.md"])
def test_phase14_documented(rel):
    text = _read(rel)
    assert "Phase 14" in text, f"{rel} missing Phase 14"
    assert "localhost:3000" in text, f"{rel} should mention localhost:3000"
    low = text.lower()
    assert "setup" in low and ("no setup" in low or "first-run" in low or "first run" in low
                               or "preseed" in low), f"{rel} should describe suppressing the setup screen"


def test_runbook_mentions_preseed_and_no_setup_screen():
    runbook = _read("docs/POWERSHELL_RUNBOOK.md")
    assert "localhost:3000" in runbook
    assert PRESEED_SERVICE in runbook or "preseed" in runbook.lower()


# --- main guard remains -----------------------------------------------------
def test_only_release_target_updates_main():
    """Re-assert the invariant: only release-dev-to-main moves main."""
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


@pytest.mark.parametrize("rel", ["README.md", "docs/ROADMAP.md", "docs/CONTAINER_GUIDE.md"])
def test_phase14_docs_keep_main_stable_note(rel):
    text = _read(rel).lower()
    # The Phase 14 docs keep the "main stays stable / untouched" promise.
    assert "main" in text
    assert ("main untouched" in text or "leaves `main` unchanged" in text
            or "unchanged" in text or "stable" in text
            or "건드리지 않" in text), f"{rel} should reaffirm main stays stable"
