#!/usr/bin/env python3
"""Offline Phase 14 core-webui preseed wiring check.

This is intentionally small and dependency-light. It verifies the repo-controlled
first-run suppression files exist and that docker-compose.yml wires the one-shot
ui-preseed service before the ui service. It does not start Docker and does not
read/write secrets.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import yaml
except Exception as exc:  # pragma: no cover - operator dependency hint
    raise SystemExit(f"PyYAML is required for this check: {exc}")

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "core-webui"
COMPOSE = ROOT / "docker-compose.yml"
SECRET_MARKERS = (
    "changeme123",
    "naplatform-password",
    "sk-",
    "bearer ",
    '"password"',
    "password:",
    '"api_key"',
    "api_key:",
    '"secret"',
    "secret:",
)


def fail(message: str) -> None:
    raise SystemExit(f"[phase14-check] FAIL: {message}")


def require(path: Path) -> str:
    if not path.exists():
        fail(f"missing {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def main() -> int:
    settings_text = require(CONFIG / "webui-settings.json")
    branding_text = require(CONFIG / "branding.yaml")
    preseed_text = require(CONFIG / "preseed.sh")
    require(CONFIG / "README.md")

    settings = json.loads(settings_text)
    if settings.get("first_run") is not False:
        fail("webui-settings.json must set first_run=false")
    if settings.get("setup_completed") is not True:
        fail("webui-settings.json must set setup_completed=true")
    if "http://api:8080" not in settings_text:
        fail("webui-settings.json must point to the in-cluster API base URL")
    if "naplatform-adapter.js" not in settings_text:
        fail("webui-settings.json must reference the NAPlatform adapter")

    combined = "\n".join([settings_text, branding_text, preseed_text]).lower()
    for marker in SECRET_MARKERS:
        if marker in combined:
            fail(f"possible secret marker {marker!r} in preseed files")

    compose = yaml.safe_load(require(COMPOSE))
    services = compose.get("services") or {}
    if "ui-preseed" not in services:
        fail("docker-compose.yml must define ui-preseed")
    if "ui" not in services:
        fail("docker-compose.yml must define ui")

    pre = services["ui-preseed"]
    vols = pre.get("volumes") or []
    if not any("config/core-webui" in str(v) and ":ro" in str(v) for v in vols):
        fail("ui-preseed must mount ./config/core-webui read-only")
    if not any("ui-hermes-home:" in str(v) for v in vols):
        fail("ui-preseed must share ui-hermes-home with ui")
    if "preseed.sh" not in " ".join(map(str, pre.get("command") or [])):
        fail("ui-preseed command must run preseed.sh")

    ui = services["ui"]
    depends = ui.get("depends_on") or {}
    if not isinstance(depends, dict):
        fail("ui.depends_on must use long form with conditions")
    pre_dep = depends.get("ui-preseed") or {}
    if pre_dep.get("condition") != "service_completed_successfully":
        fail("ui must wait for ui-preseed service_completed_successfully")

    env = ui.get("environment") or {}
    if env.get("HERMES_WEBUI_SETUP_COMPLETED") not in ("true", True):
        fail("ui must carry HERMES_WEBUI_SETUP_COMPLETED=true")
    if env.get("HERMES_WEBUI_DISABLE_FIRST_RUN") not in ("true", True):
        fail("ui must carry HERMES_WEBUI_DISABLE_FIRST_RUN=true")

    print("[phase14-check] OK: core-webui first-run preseed wiring is present; localhost:3000 should skip setup screen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
