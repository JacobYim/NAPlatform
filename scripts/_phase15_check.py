#!/usr/bin/env python3
"""Offline Phase 15 wiring check (no Docker).

Verifies two things without starting Docker or reading/writing any secret:

  (A) core-webui login gate — after the first-run setup screen is skipped, the UI
      requires login (does NOT land directly in chat). The repo config declares
      login_required/require_auth/start_page=login and the ui service wires the
      HERMES_WEBUI_PASSWORD from CORE_WEBUI_PASSWORD (dev-only default), with NO
      password written into any preseed file.

  (B) Docker Model Runner refactor — the shared endpoint + model live in the
      repo-controlled, non-secret config/model-runner/model-runner.env (gemma4:31b),
      docker-compose.model-runner.yml loads it via env_file on the API + every agent,
      maps host.docker.internal via host-gateway, and defines NO embedded model-runner
      service. The default docker-compose.yml stays model-less.
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
MODEL = "gemma4:31b"
AGENTS = ("api", "hermes-er", "hermes-it", "hermes-ehs", "hermes-qc")
SECRET_MARKERS = ("changeme123", "naplatform-password", "sk-", "bearer ",
                  '"password"', "password:", '"api_key"', "api_key:",
                  '"secret"', "secret:")


def fail(message: str) -> None:
    raise SystemExit(f"[phase15-check] FAIL: {message}")


def read(rel: str) -> str:
    path = ROOT / rel
    if not path.exists():
        fail(f"missing {rel}")
    return path.read_text(encoding="utf-8")


def parse_env(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip()
    return env


def check_login() -> None:
    settings_text = read("config/core-webui/webui-settings.json")
    settings = json.loads(settings_text)
    for key in ("login_required", "require_auth"):
        if settings.get(key) is not True:
            fail(f"webui-settings.json must set {key}=true")
    if settings.get("start_page") != "login":
        fail("webui-settings.json must set start_page='login'")
    if settings.get("defaults", {}).get("landing") != "login":
        fail("webui-settings.json defaults.landing must be 'login' (not 'workspace')")
    # first-run still skipped
    if settings.get("first_run") is not False or settings.get("setup_completed") is not True:
        fail("webui-settings.json must still skip first-run (first_run=false/setup_completed=true)")
    # no secret in the preseed files
    combined = "\n".join(read(f"config/core-webui/{f}")
                         for f in ("webui-settings.json", "branding.yaml", "preseed.sh")).lower()
    for marker in SECRET_MARKERS:
        if marker in combined:
            fail(f"possible secret marker {marker!r} in a preseed file")

    compose = yaml.safe_load(read("docker-compose.yml"))
    ui_env = compose["services"]["ui"].get("environment") or {}
    for key in ("HERMES_WEBUI_LOGIN_REQUIRED", "HERMES_WEBUI_REQUIRE_AUTH"):
        if ui_env.get(key) not in ("true", True):
            fail(f"ui service must set {key}=true")
    if ui_env.get("HERMES_WEBUI_START_PAGE") != "login":
        fail("ui service must set HERMES_WEBUI_START_PAGE=login")
    pw = str(ui_env.get("HERMES_WEBUI_PASSWORD") or "")
    if "CORE_WEBUI_PASSWORD" not in pw:
        fail("ui HERMES_WEBUI_PASSWORD must be overridable via ${CORE_WEBUI_PASSWORD}")


def check_model_runner() -> None:
    env = parse_env(read("config/model-runner/model-runner.env"))
    if env.get("DOCKER_MODEL_RUNNER_MODEL") != MODEL:
        fail(f"model-runner.env must set DOCKER_MODEL_RUNNER_MODEL={MODEL}")
    if "DOCKER_MODEL_RUNNER_BASE_URL" not in env:
        fail("model-runner.env must set DOCKER_MODEL_RUNNER_BASE_URL")

    mr = yaml.safe_load(read("docker-compose.model-runner.yml"))
    services = mr.get("services") or {}
    if "model-runner" in services or "model_runner" in services:
        fail("docker-compose.model-runner.yml must NOT embed a model-runner service")
    for svc in AGENTS:
        if svc not in services:
            fail(f"docker-compose.model-runner.yml missing {svc}")
        s = services[svc]
        ef = s.get("env_file") or []
        if not any("config/model-runner/model-runner.env" in str(e) for e in ef):
            fail(f"{svc} must load the shared model-runner env_file")
        eh = s.get("extra_hosts") or []
        if not any("host.docker.internal:host-gateway" in str(h) for h in eh):
            fail(f"{svc} must map host.docker.internal:host-gateway")
        if (s.get("environment") or {}).get("HERMES_LLM_PROVIDER") != "docker-model-runner":
            fail(f"{svc} must set HERMES_LLM_PROVIDER=docker-model-runner")

    base = read("docker-compose.yml")
    if "DOCKER_MODEL_RUNNER_MODEL" in base or MODEL in base:
        fail("default docker-compose.yml must stay model-less (no gemma4:31b)")


def main() -> int:
    check_login()
    check_model_runner()
    print("[phase15-check] OK: login-required core-webui + env_file-driven shared "
          "gemma4:31b model runner (no embedded service); default stack stays safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
