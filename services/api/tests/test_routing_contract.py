"""Phase 08: live HTTP contract between the API router and the real hermes app.

Unlike ``test_agent_service_shape.py`` (which hand-writes the service handler),
this test wires the API's ``DepartmentAgentRouter`` HTTP path to the **actual**
``hermes_agent`` FastAPI app. A ``MockTransport`` forwards the router's HTTP POST
to the real hermes ``TestClient``, so the request travels the genuine service
code: ``InvokeRequest`` parsing, ``_validate_scope`` (department/roots/identifier
checks), and ``_dry_run``.

It proves the Phase 08 stack invariant: with ``AGENT_ROUTING_ENABLED=true`` on the
API but ``HERMES_AGENT_EXECUTION_ENABLED=false`` on the agent, the API sees
``hermes_invoked=true`` (the HTTP hop succeeded) while the agent's own payload
reports ``hermes_invoked=false`` (no real Hermes CLI ran) — a genuine dry-run
agent response over a real HTTP path.
"""
import httpx
from fastapi.testclient import TestClient

from app.agent_router import DepartmentAgentRouter, HttpAgentClient
from app.models import AgentContext, User, UserStatus
from hermes_agent.config import Settings
from hermes_agent.main import build_app


def _qc_user():
    return User(id="u-qc", email="qc@example.com", username="quinn",
                status=UserStatus.active, password_hash="x", departments=["QC"])


def _qc_ctx():
    return AgentContext(
        user_id="u-qc", username="quinn", department="QC",
        hdfs_roots=["/naplatform/users/quinn", "/naplatform/departments/QC"],
        qdrant_filter={"should": []}, neo4j_filter={"owner_user_id": "u-qc"},
        allowed_tools=["hdfs_list", "quality_trace"],
        allowed_mcp_servers=["mcp-qc-filesystem", "mcp-qc-knowledge"])


def _hermes_settings(tmp_path, department="QC", execution_enabled=False):
    return Settings(
        department=department, profile=department, api_base_url="http://api:8080",
        hdfs_namenode="hdfs://hdfs-namenode:9000", hermes_home=str(tmp_path / ".hermes"),
        hermes_bin="hermes", execution_enabled=execution_enabled, execution_timeout=5.0)


def _router_to_hermes(tmp_path, *, department="QC", execution_enabled=False):
    """A router with routing enabled, whose HTTP client hits the real hermes app."""
    hermes_app = build_app(_hermes_settings(tmp_path, department, execution_enabled),
                           prepare_on_startup=False)
    hermes_client = TestClient(hermes_app)

    def forward(request: httpx.Request) -> httpx.Response:
        resp = hermes_client.post(request.url.path, content=request.content,
                                  headers={"content-type": "application/json"})
        return httpx.Response(resp.status_code, content=resp.content,
                              headers={"content-type": resp.headers.get("content-type",
                                                                        "application/json")})

    client = HttpAgentClient("/chat", 5.0, transport=httpx.MockTransport(forward))
    return DepartmentAgentRouter(enabled=True, client=client,
                                 endpoints={"QC": "http://hermes-qc:8080"})


def test_enabled_routing_execution_disabled_returns_dry_run_agent_response(tmp_path):
    router = _router_to_hermes(tmp_path, execution_enabled=False)
    result = router.invoke(_qc_user(), _qc_ctx(), "smoke: quality trace", request_id="rid-c1")

    # API layer: the real HTTP hop to the agent succeeded.
    assert result["hermes_invoked"] is True
    # Agent layer: execution is disabled, so the agent itself did a dry run.
    assert result["upstream"]["hermes_invoked"] is False
    assert result["upstream"]["department"] == "QC"
    assert result["upstream"]["request_id"] == "rid-c1"
    assert "quality trace" in result["upstream"]["reply"]
    # Agent echoed back the scope it was handed (deterministic dry-run body).
    assert result["upstream"]["scope"]["allowed_tools"] == ["hdfs_list", "quality_trace"]


def test_department_mismatch_is_enforced_by_the_real_agent(tmp_path):
    # Point the "QC" endpoint at an agent container that actually serves IT.
    router = _router_to_hermes(tmp_path, department="IT", execution_enabled=False)
    try:
        router.invoke(_qc_user(), _qc_ctx(), "cross-department", request_id="rid-c2")
    except Exception as exc:  # the router maps a 4xx to an upstream error
        assert "403" in str(exc) or "mismatch" in str(exc).lower()
    else:
        raise AssertionError("expected the agent to reject a department mismatch")
