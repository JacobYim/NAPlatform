"""Phase 13: API-side shared Docker Model Runner (gemma4:31b) status + safety.

Covers the secret-free ``config.model_runtime_status()`` view and that it surfaces
through the admin-only ``GET /admin/agents/status`` so an operator can confirm the
shared model runner is configured without any secret leaving the process. The
default (no envs) reports ``configured=false`` and stays dry-run safe.
"""
from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)
MODEL = "gemma4:31b"


def admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def _make_active_user(email, username, departments):
    client.post('/auth/signup', json={'email': email, 'username': username,
                                       'password': 'Password123!', 'department': departments[0]})
    from app.main import store
    u = store.get_user_by_email(email)
    t = admin_token()
    r = client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'},
                     json={'status': 'active', 'departments': departments})
    assert r.status_code == 200, r.text
    token = client.post('/auth/login', json={'email': email, 'password': 'Password123!'}).json()['token']
    return u, token


# --- config.model_runtime_status ------------------------------------------
def test_default_runtime_is_unconfigured(monkeypatch):
    for var in ("HERMES_LLM_PROVIDER", "LLM_PROVIDER", "DOCKER_MODEL_RUNNER_BASE_URL",
                "OPENAI_BASE_URL", "DOCKER_MODEL_RUNNER_MODEL", "OPENAI_MODEL",
                "OPENAI_API_KEY", "DOCKER_MODEL_RUNNER_DEFAULT_INDEX",
                "DOCKER_MODEL_RUNNER_0_BASE_URL", "DOCKER_MODEL_RUNNER_0_MODEL",
                "DOCKER_MODEL_RUNNER_1_BASE_URL", "DOCKER_MODEL_RUNNER_1_MODEL"):
        monkeypatch.delenv(var, raising=False)
    status = config.model_runtime_status()
    assert status["configured"] is False
    assert status["provider"] is None and status["model"] is None


def test_dmr_runtime_reports_gemma(monkeypatch):
    monkeypatch.setenv("HERMES_LLM_PROVIDER", "docker-model-runner")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_BASE_URL", "http://model-runner.docker.internal/engines/v1")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_MODEL", MODEL)
    status = config.model_runtime_status()
    assert status["configured"] is True
    assert status["provider"] == "docker-model-runner"
    assert status["model"] == MODEL
    assert status["openai_compatible"] is True


def test_dmr_runtime_uses_selected_indexed_candidate(monkeypatch):
    for var in ("DOCKER_MODEL_RUNNER_BASE_URL", "DOCKER_MODEL_RUNNER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_LLM_PROVIDER", "docker-model-runner")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_DEFAULT_INDEX", "1")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_0_NAME", "local")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_0_BASE_URL", "http://host.docker.internal:12434/engines/v1")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_0_MODEL", MODEL)
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_1_NAME", "workstation")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_1_BASE_URL", "http://192.168.100.10:12434/engines/v1")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_1_MODEL", MODEL)

    status = config.model_runtime_status()

    assert status["configured"] is True
    assert status["base_url"] == "http://192.168.100.10:12434/engines/v1"
    assert status["selected_runner_index"] == 1
    assert [runner["selected"] for runner in status["runners"]] == [False, True]


def test_runtime_status_redacts_secrets(monkeypatch):
    monkeypatch.setenv("HERMES_LLM_PROVIDER", "docker-model-runner")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_BASE_URL", "http://user:sekret@model-runner.docker.internal/engines/v1")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_MODEL", MODEL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
    blob = str(config.model_runtime_status())
    assert "sekret" not in blob
    assert "sk-super-secret" not in blob
    status = config.model_runtime_status()
    assert status["api_key_present"] is True
    assert "model-runner.docker.internal" in status["base_url"]


# --- /admin/agents/status surfaces it (admin only) ------------------------
def test_agents_status_includes_model_runtime(monkeypatch):
    monkeypatch.setenv("HERMES_LLM_PROVIDER", "docker-model-runner")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_BASE_URL", "http://model-runner.docker.internal/engines/v1")
    monkeypatch.setenv("DOCKER_MODEL_RUNNER_MODEL", MODEL)
    t = admin_token()
    r = client.get('/admin/agents/status', headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "model_runtime" in body
    assert body["model_runtime"]["model"] == MODEL
    assert body["model_runtime"]["configured"] is True


def test_agents_status_admin_only():
    assert client.get('/admin/agents/status').status_code == 401
    _, token = _make_active_user('mr1@example.com', 'mruser1', ['ER'])
    assert client.get('/admin/agents/status',
                      headers={'Authorization': f'Bearer {token}'}).status_code == 403
