"""Phase 16 guards for NAPlatform email/password WebUI auth."""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
SETTINGS = json.loads((REPO_ROOT / "config" / "core-webui" / "webui-settings.json").read_text(encoding="utf-8"))


def test_compose_enables_naplatform_webui_auth_mode():
    assert "HERMES_WEBUI_AUTH_MODE: naplatform" in COMPOSE
    assert "context: ${CORE_WEBUI_CONTEXT:-../core-webui}" in COMPOSE
    assert "email + password" in COMPOSE
    assert "admin-approved active user" in COMPOSE
    assert "HERMES_WEBUI_PASSWORD:" not in COMPOSE


def test_preseed_declares_email_password_register_approval_contract():
    auth = SETTINGS["auth"]
    assert auth["adapter"] == "naplatform"
    assert auth["mode"] == "external"
    assert auth["provider"] == "naplatform"
    assert auth["login_form"] == "email_password"
    assert auth["register_enabled"] is True
    assert auth["approval_required"] is True
    assert SETTINGS["endpoints"]["register"] == "/auth/signup"
    assert SETTINGS["login_required"] is True
    assert SETTINGS["defaults"]["landing"] == "login"
