"""Phase 06: department Hermes agent routing.

The router is exercised without any live Hermes: the dry-run default never
touches the network, and the enabled HTTP path is driven through an injected
`httpx.MockTransport`. Endpoint tests confirm the deterministic dry-run default,
timeout/upstream error mapping to 504/502, the admin status endpoint, audit
events, and that no user-supplied URL is ever dialed (SSRF).
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent_router as ar_mod
from app import main
from app.agent_router import (AgentRoutingError, AgentTimeout, AgentUpstreamError,
                              DepartmentAgentRouter, DryRunAgentClient,
                              HttpAgentClient, department_endpoints)
from app.main import app, store
from app.models import AgentContext, User, UserStatus

client = TestClient(app)


# --- fixtures / helpers --------------------------------------------------
def _user():
    return User(id="u1", email="u1@example.com", username="alice",
                password_hash="x", status=UserStatus.active, departments=["ER"])


def _ctx():
    return AgentContext(
        user_id="u1", username="alice", department="ER",
        hdfs_roots=["/naplatform/users/alice", "/naplatform/departments/ER"],
        qdrant_filter={"should": [{"key": "department", "match": {"value": "ER"}}]},
        neo4j_filter={"owner_user_id": "u1", "department": "ER"},
        allowed_tools=["hdfs_list", "incident_report"],
        allowed_mcp_servers=["mcp-er-filesystem", "mcp-er-knowledge"])


def _mock_client(handler, invoke_path="/chat", timeout=5.0):
    return HttpAgentClient(invoke_path, timeout, transport=httpx.MockTransport(handler))


def admin_token():
    r = client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'ChangeMe123!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def make_active_user(email, username, departments):
    client.post('/auth/signup', json={'email': email, 'username': username,
                                       'password': 'Password123!', 'department': departments[0]})
    u = store.get_user_by_email(email)
    t = admin_token()
    r = client.patch(f'/admin/users/{u.id}', headers={'Authorization': f'Bearer {t}'},
                     json={'status': 'active', 'departments': departments})
    assert r.status_code == 200, r.text
    return client.post('/auth/login', json={'email': email, 'password': 'Password123!'}).json()['token']


# --- dry-run payload / context ------------------------------------------
def test_dry_run_default_does_not_invoke_hermes():
    router = DepartmentAgentRouter(enabled=False)
    result = router.invoke(_user(), _ctx(), "hello world")
    assert result["hermes_invoked"] is False
    assert result["department"] == "ER"
    assert result["request_id"]  # present and non-empty
    assert "hello world" in result["reply"]
    summary = result["context_summary"]
    assert summary["allowed_tools"] == ["hdfs_list", "incident_report"]
    assert summary["workspace_root"] == "/naplatform/users/alice"
    assert summary["allowed_mcp_servers"] == ["mcp-er-filesystem", "mcp-er-knowledge"]


def test_dry_run_client_never_hits_network():
    # DryRunAgentClient does not import/use httpx at all.
    out = DryRunAgentClient().invoke("http://hermes-er:8080", {
        "request_id": "rid", "message": "hi", "department": "ER",
        "user": {"user_id": "u1", "username": "alice", "email": "a@b.c"},
        "allowed_tools": [], "allowed_mcp_servers": [], "hdfs_roots": [],
        "qdrant_filter": {}, "neo4j_filter": {}, "workspace_root": "/naplatform/users/alice"})
    assert out["hermes_invoked"] is False and out["request_id"] == "rid"


def test_invocation_payload_carries_full_scope():
    router = DepartmentAgentRouter(enabled=False)
    payload = router.build_payload(_user(), _ctx(), "msg", "rid-1", "sess-1",
                                   "http://hermes-er:8080")
    for key in ("message", "allowed_tools", "allowed_mcp_servers", "hdfs_roots",
                "qdrant_filter", "neo4j_filter", "workspace_root", "context"):
        assert key in payload
    assert payload["request_id"] == "rid-1" and payload["session_id"] == "sess-1"
    assert payload["user"] == {"user_id": "u1", "username": "alice", "email": "u1@example.com"}
    assert payload["workspace_root"] == "/naplatform/users/alice"
    assert payload["message"] == "msg"


# --- enabled HTTP path (fake transport) ---------------------------------
def test_enabled_http_client_posts_payload_and_marks_invoked():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"reply": "pong from ER"})

    router = DepartmentAgentRouter(enabled=True, client=_mock_client(handler))
    result = router.invoke(_user(), _ctx(), "ping", request_id="rid-9")
    assert result["hermes_invoked"] is True
    assert result["reply"] == "pong from ER"
    assert seen["url"].endswith("/chat")
    # The posted body carries the RBAC scope the agent must honor.
    assert seen["body"]["message"] == "ping"
    assert seen["body"]["allowed_tools"] == ["hdfs_list", "incident_report"]
    assert seen["body"]["workspace_root"] == "/naplatform/users/alice"
    assert seen["body"]["request_id"] == "rid-9"


def test_http_client_falls_back_from_chat_to_invoke_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat"):
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"reply": "ok via invoke"})

    router = DepartmentAgentRouter(enabled=True, client=_mock_client(handler))
    result = router.invoke(_user(), _ctx(), "hi")
    assert result["hermes_invoked"] is True
    assert result["reply"] == "ok via invoke"
    assert result["endpoint"].endswith("/invoke")


# --- timeout / upstream error mapping -----------------------------------
def test_timeout_raises_agent_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    router = DepartmentAgentRouter(enabled=True, client=_mock_client(handler))
    with pytest.raises(AgentTimeout):
        router.invoke(_user(), _ctx(), "hi")


def test_upstream_5xx_raises_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    router = DepartmentAgentRouter(enabled=True, client=_mock_client(handler))
    with pytest.raises(AgentUpstreamError):
        router.invoke(_user(), _ctx(), "hi")


def test_connection_error_raises_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    router = DepartmentAgentRouter(enabled=True, client=_mock_client(handler))
    with pytest.raises(AgentUpstreamError):
        router.invoke(_user(), _ctx(), "hi")


# --- endpoint resolution / SSRF -----------------------------------------
def test_default_endpoints_match_compose_service_names():
    eps = department_endpoints()
    assert eps["ER"] == "http://hermes-er:8080"
    assert eps["IT"] == "http://hermes-it:8080"
    assert eps["EHS"] == "http://hermes-ehs:8080"
    assert eps["QC"] == "http://hermes-qc:8080"


def test_env_override_endpoint(monkeypatch):
    monkeypatch.setenv("HERMES_ER_URL", "http://custom-er:9000")
    assert department_endpoints()["ER"] == "http://custom-er:9000"


def test_unknown_department_rejected():
    router = DepartmentAgentRouter(enabled=False)
    with pytest.raises(ValueError):
        router.endpoint_for("HR")


def test_only_configured_departments_resolve():
    # An empty env override disables routing for that department (dry-run).
    router = DepartmentAgentRouter(enabled=True, endpoints={"ER": ""})
    assert router.endpoint_for("ER") is None
    # A disabled/unconfigured department still dry-runs rather than dialing.
    result = router.invoke(_user(), _ctx(), "hi")
    assert result["hermes_invoked"] is False


def test_non_http_scheme_rejected():
    router = DepartmentAgentRouter(enabled=True, endpoints={"ER": "file:///etc/passwd"})
    with pytest.raises(AgentRoutingError):
        router.endpoint_for("ER")


def test_router_never_takes_a_user_supplied_url():
    # The router API only accepts a department string; the URL is chosen solely
    # from the configured map. Passing a URL-looking department is rejected.
    router = DepartmentAgentRouter(enabled=True)
    with pytest.raises(ValueError):
        router.endpoint_for("http://evil.example.com")


# --- endpoint: deterministic dry-run default -----------------------------
def test_chat_endpoint_dry_run_default():
    ut = make_active_user('p6chat@example.com', 'p6chatuser', ['ER'])
    r = client.post('/agents/ER/chat', headers={'Authorization': f'Bearer {ut}'},
                    json={'message': 'hello'})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b['hermes_invoked'] is False
    assert b['request_id']
    assert 'hello' in b['reply']


# --- endpoint: enabled HTTP path via monkeypatched router ----------------
def test_chat_endpoint_enabled_http(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"reply": "live reply"})

    router = DepartmentAgentRouter(enabled=True, client=_mock_client(handler))
    monkeypatch.setattr(main, "agent_router", router)
    ut = make_active_user('p6live@example.com', 'p6liveuser', ['IT'])
    r = client.post('/agents/IT/chat', headers={'Authorization': f'Bearer {ut}'},
                    json={'message': 'ping'})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b['hermes_invoked'] is True and b['reply'] == "live reply"


# --- endpoint: timeout/upstream error mapping ----------------------------
class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc

    def invoke(self, url, payload):
        raise self._exc


def test_chat_endpoint_timeout_maps_to_504(monkeypatch):
    router = DepartmentAgentRouter(enabled=True, client=_RaisingClient(AgentTimeout("slow")))
    monkeypatch.setattr(main, "agent_router", router)
    ut = make_active_user('p6to@example.com', 'p6touser', ['EHS'])
    r = client.post('/agents/EHS/chat', headers={'Authorization': f'Bearer {ut}'},
                    json={'message': 'hi'})
    assert r.status_code == 504, r.text


def test_chat_endpoint_upstream_maps_to_502(monkeypatch):
    router = DepartmentAgentRouter(enabled=True, client=_RaisingClient(AgentUpstreamError("boom")))
    monkeypatch.setattr(main, "agent_router", router)
    ut = make_active_user('p6up@example.com', 'p6upuser', ['QC'])
    r = client.post('/agents/QC/chat', headers={'Authorization': f'Bearer {ut}'},
                    json={'message': 'hi'})
    assert r.status_code == 502, r.text


# --- admin status endpoint ----------------------------------------------
def test_admin_agents_status_reports_config():
    t = admin_token()
    r = client.get('/admin/agents/status', headers={'Authorization': f'Bearer {t}'})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b['enabled'] is False and b['dry_run'] is True
    assert set(b['departments']) == {"ER", "IT", "EHS", "QC"}
    assert b['departments']['ER']['url'] == "http://hermes-er:8080"
    assert b['departments']['ER']['configured'] is True
    assert 'invoke_path' in b and 'timeout_seconds' in b


def test_admin_agents_status_requires_admin():
    ut = make_active_user('p6notadmin@example.com', 'p6notadmin', ['ER'])
    r = client.get('/admin/agents/status', headers={'Authorization': f'Bearer {ut}'})
    assert r.status_code == 403


# --- audit ---------------------------------------------------------------
def test_chat_success_and_failure_audited(monkeypatch):
    # A successful (dry-run) chat is audited.
    ut = make_active_user('p6aud@example.com', 'p6auduser', ['ER'])
    client.post('/agents/ER/chat', headers={'Authorization': f'Bearer {ut}'},
                json={'message': 'hello'})
    # A timeout failure is audited too.
    router = DepartmentAgentRouter(enabled=True, client=_RaisingClient(AgentTimeout("slow")))
    monkeypatch.setattr(main, "agent_router", router)
    client.post('/agents/ER/chat', headers={'Authorization': f'Bearer {ut}'},
                json={'message': 'again'})
    t = admin_token()
    audit = client.get('/admin/audit', headers={'Authorization': f'Bearer {t}'}).json()
    chat_events = [e for e in audit if e['action'] == 'agent_chat']
    assert any(e['success'] for e in chat_events)
    assert any(not e['success'] and 'timeout' in (e['detail'] or '') for e in chat_events)
