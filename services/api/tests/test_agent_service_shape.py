"""Phase 07: the API router and the hermes-agent service speak the same shape.

Proves — without a live agent — that ``DepartmentAgentRouter``'s HTTP payload is
exactly what the hermes-agent ``/chat`` endpoint consumes, and that the router
correctly ingests the response shape the service returns. The service is
simulated with ``httpx.MockTransport`` whose handler applies the same
department-match / scope checks the real service does.
"""
import json

import httpx

from app.agent_router import DepartmentAgentRouter, HttpAgentClient
from app.models import AgentContext, User, UserStatus

# The exact set of keys the hermes-agent InvokeRequest reads from the payload.
REQUIRED_PAYLOAD_KEYS = {
    "message", "department", "request_id", "user", "allowed_tools",
    "allowed_mcp_servers", "hdfs_roots", "qdrant_filter", "neo4j_filter",
    "workspace_root",
}


def _user():
    return User(id="u1", email="u1@example.com", username="alice",
                status=UserStatus.active, password_hash="x", departments=["ER"])


def _ctx():
    return AgentContext(
        user_id="u1", username="alice", department="ER",
        hdfs_roots=["/naplatform/users/alice/workspace", "/naplatform/departments/ER/department_shared"],
        qdrant_filter={"should": []}, neo4j_filter={"owner_user_id": "u1"},
        allowed_tools=["hdfs_list", "incident_report"],
        allowed_mcp_servers=["mcp-er-filesystem"])


def _service_handler(seen: dict):
    """Mimic the hermes-agent service: validate then return its response shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen["body"] = body
        # department match — as the real service enforces.
        if body["department"].upper() != "ER":
            return httpx.Response(403, json={"detail": "department mismatch"})
        # deterministic response, mirroring hermes_agent.main._dry_run.
        return httpx.Response(200, json={
            "request_id": body["request_id"],
            "department": "ER",
            "hermes_invoked": False,
            "reply": f"[hermes:ER] received '{body['message']}'",
            "profile": "ER",
            "scope": {"allowed_tools": body["allowed_tools"]},
        })

    return handler


def test_router_payload_matches_service_contract():
    seen: dict = {}
    client = HttpAgentClient("/chat", 5.0,
                             transport=httpx.MockTransport(_service_handler(seen)))
    router = DepartmentAgentRouter(enabled=True, client=client)
    result = router.invoke(_user(), _ctx(), "hello", request_id="rid-1")

    # The router posted every field the service's InvokeRequest requires.
    assert REQUIRED_PAYLOAD_KEYS.issubset(seen["body"].keys())
    assert seen["body"]["department"] == "ER"
    assert seen["body"]["hdfs_roots"] == ["/naplatform/users/alice/workspace",
                                          "/naplatform/departments/ER/department_shared"]
    assert seen["body"]["workspace_root"] == "/naplatform/users/alice/workspace"

    # The router ingested the service's response shape.
    assert result["hermes_invoked"] is True  # HTTP call succeeded
    assert result["reply"] == "[hermes:ER] received 'hello'"
    assert result["upstream"]["department"] == "ER"
    assert result["upstream"]["request_id"] == "rid-1"
