"""Phase 19: Compose readiness guards for WebUI -> NAPlatform login.

The WebUI proxies login/register/department-option calls to ``http://api:8080``.
If the API container starts before Postgres is accepting TCP connections it can
crash during SQLAlchemy startup, leaving WebUI healthy but every login request
returning 502.  These meta tests keep the Compose dependency graph from
regressing.
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]


def test_postgres_has_healthcheck_before_api_starts():
    postgres = SERVICES["postgres"]
    health = postgres.get("healthcheck") or {}
    test = " ".join(health.get("test") or [])
    assert "pg_isready" in test
    assert "nap" in test and "naplatform" in test
    assert health.get("interval")
    assert health.get("timeout")
    assert health.get("retries", 0) >= 5


def test_api_waits_for_postgres_and_redis_health_and_restarts_on_transient_db_boot():
    api = SERVICES["api"]
    dep = api.get("depends_on")
    assert isinstance(dep, dict), "api.depends_on must use long-form health conditions"
    assert dep["postgres"]["condition"] == "service_healthy"
    assert dep["redis"]["condition"] == "service_healthy"
    assert api.get("restart") in {"on-failure", "unless-stopped", "always"}


def test_api_exposes_healthcheck_and_ui_waits_for_healthy_api():
    api = SERVICES["api"]
    health = api.get("healthcheck") or {}
    test = " ".join(health.get("test") or [])
    assert "http://localhost:8080/health" in test
    assert health.get("interval")
    assert health.get("timeout")
    assert health.get("retries", 0) >= 5

    ui_dep = SERVICES["ui"].get("depends_on")
    assert isinstance(ui_dep, dict), "ui.depends_on must use long-form conditions"
    assert ui_dep["api"]["condition"] == "service_healthy"
    assert ui_dep["ui-preseed"]["condition"] == "service_completed_successfully"


def test_webui_api_base_uses_compose_service_name():
    ui_env = SERVICES["ui"].get("environment") or {}
    assert ui_env["NAPLATFORM_API_BASE_URL"] == "http://api:8080"
