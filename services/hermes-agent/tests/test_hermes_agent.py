"""Phase 07: department Hermes agent HTTP service.

The service is exercised end-to-end with FastAPI's TestClient and no live Hermes
CLI: the deterministic default never spawns a subprocess, and the optional
execution path is driven through a ``FakeHermesRunner``. Covers health,
chat/invoke, department-mismatch denial, invalid-root denial, invalid-tool
denial, and both execution-disabled and execution-enabled behaviour.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from hermes_agent.config import Settings
from hermes_agent.executor import HermesRunner, RunResult, build_hermes_argv
from hermes_agent.main import MAX_MESSAGE_CHARS, build_app


# --- helpers -------------------------------------------------------------
def _settings(tmp_path, department="ER", execution_enabled=False, **over):
    base = dict(
        department=department,
        profile=department,
        api_base_url="http://api:8080",
        hdfs_namenode="hdfs://hdfs-namenode:9000",
        hermes_home=str(tmp_path / ".hermes"),
        hermes_bin="hermes",
        execution_enabled=execution_enabled,
        execution_timeout=5.0,
    )
    base.update(over)
    return Settings(**base)


def _payload(department="ER", **over):
    body = {
        "request_id": "rid-1",
        "session_id": "sess-1",
        "message": "hello world",
        "department": department,
        "user": {"user_id": "u1", "username": "alice", "email": "a@b.c"},
        "context": {"department": department},
        "allowed_tools": ["hdfs_list", "incident_report"],
        "allowed_mcp_servers": ["mcp-er-filesystem", "mcp-er-knowledge"],
        "hdfs_roots": ["/naplatform/users/alice", "/naplatform/departments/ER"],
        "qdrant_filter": {"should": []},
        "neo4j_filter": {"owner_user_id": "u1"},
        "workspace_root": "/naplatform/users/alice",
    }
    body.update(over)
    return body


def _context_file_from_argv(argv):
    return argv[argv.index("--context-file") + 1]


class FakeHermesRunner(HermesRunner):
    """Records the argv it was handed and returns a canned result — no subprocess.

    While it "runs", it snapshots the ``--context-file`` the handler wrote: that
    the file exists at call time and its parsed contents. This lets tests assert
    the scope file is present *during* the runner call (and gone afterwards).
    """

    def __init__(self, result: RunResult | None = None):
        self.calls: list[tuple[list[str], float]] = []
        self._result = result or RunResult(0, "fake hermes reply", "")
        self.context_path: str | None = None
        self.context_existed: bool | None = None
        self.context_payload: dict | None = None

    def run(self, argv, timeout):
        self.calls.append((argv, timeout))
        self.context_path = _context_file_from_argv(argv)
        self.context_existed = os.path.exists(self.context_path)
        if self.context_existed:
            with open(self.context_path, encoding="utf-8") as fh:
                self.context_payload = json.load(fh)
        return self._result


def _client(settings, runner=None):
    return TestClient(build_app(settings, runner=runner))


# --- health --------------------------------------------------------------
def test_health_reports_department_and_profile(tmp_path):
    with _client(_settings(tmp_path)) as c:
        r = c.get("/health")
        assert r.status_code == 200
        b = r.json()
        assert b["status"] == "ok"
        assert b["department"] == "ER"
        assert b["profile"] == "ER"
        assert b["execution_enabled"] is False
        assert b["profile_ready"] is True  # lifespan prepared the profile


def test_startup_prepares_profile_files(tmp_path):
    settings = _settings(tmp_path)
    with _client(settings):
        pass
    profile_dir = tmp_path / ".hermes" / "profiles" / "ER"
    soul = (profile_dir / "SOUL.md").read_text(encoding="utf-8")
    config = (profile_dir / "config.yaml").read_text(encoding="utf-8")
    assert "ER department Hermes agent" in soul
    assert 'department: "ER"' in config
    assert "redact_secrets: true" in config


# --- chat / invoke deterministic default --------------------------------
@pytest.mark.parametrize("path", ["/chat", "/invoke"])
def test_chat_and_invoke_deterministic_default(tmp_path, path):
    with _client(_settings(tmp_path)) as c:
        r = c.post(path, json=_payload())
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["hermes_invoked"] is False
        assert b["department"] == "ER"
        assert b["request_id"] == "rid-1"
        assert "hello world" in b["reply"]
        assert b["scope"]["allowed_tools"] == ["hdfs_list", "incident_report"]


def test_chat_generates_request_id_when_missing(tmp_path):
    with _client(_settings(tmp_path)) as c:
        r = c.post("/chat", json=_payload(request_id=None))
        assert r.status_code == 200, r.text
        assert r.json()["request_id"]


# --- department mismatch -------------------------------------------------
def test_department_mismatch_denied(tmp_path):
    # This container serves ER; a payload addressed to IT must be rejected.
    with _client(_settings(tmp_path, department="ER")) as c:
        r = c.post("/chat", json=_payload(department="IT"))
        assert r.status_code == 403, r.text
        assert "department mismatch" in r.json()["detail"]


# --- invalid hdfs roots --------------------------------------------------
@pytest.mark.parametrize("bad_root", [
    "/etc/passwd",                       # outside /naplatform
    "/naplatform/../etc",                # traversal
    "relative/path",                     # not absolute
])
def test_invalid_roots_denied(tmp_path, bad_root):
    with _client(_settings(tmp_path)) as c:
        r = c.post("/chat", json=_payload(hdfs_roots=["/naplatform/users/alice", bad_root]))
        assert r.status_code == 400, r.text


# --- message length validation ------------------------------------------
def test_oversized_message_rejected_422(tmp_path):
    with _client(_settings(tmp_path)) as c:
        big = "x" * (MAX_MESSAGE_CHARS + 1)
        r = c.post("/chat", json=_payload(message=big))
        assert r.status_code == 422, r.text


def test_max_length_message_accepted(tmp_path):
    with _client(_settings(tmp_path)) as c:
        at_limit = "x" * MAX_MESSAGE_CHARS
        r = c.post("/chat", json=_payload(message=at_limit))
        assert r.status_code == 200, r.text


# --- invalid workspace_root ---------------------------------------------
@pytest.mark.parametrize("bad_ws", [
    "/etc/passwd",                       # outside /naplatform
    "/naplatform/../etc/secret",         # traversal
    "relative/workspace",                # not absolute
])
def test_invalid_workspace_root_rejected(tmp_path, bad_ws):
    with _client(_settings(tmp_path)) as c:
        r = c.post("/chat", json=_payload(workspace_root=bad_ws))
        assert r.status_code == 400, r.text


def test_absent_workspace_root_allowed(tmp_path):
    with _client(_settings(tmp_path)) as c:
        r = c.post("/chat", json=_payload(workspace_root=None))
        assert r.status_code == 200, r.text


# --- invalid tool / mcp identifier --------------------------------------
@pytest.mark.parametrize("field", ["allowed_tools", "allowed_mcp_servers"])
@pytest.mark.parametrize("bad", ["rm -rf /", "tool;drop", "../escape", "UPPER"])
def test_invalid_tool_denied(tmp_path, field, bad):
    with _client(_settings(tmp_path)) as c:
        r = c.post("/chat", json=_payload(**{field: ["hdfs_list", bad]}))
        assert r.status_code == 400, r.text


# --- execution disabled is deterministic (no subprocess) -----------------
def test_execution_disabled_never_calls_runner(tmp_path):
    runner = FakeHermesRunner()
    with _client(_settings(tmp_path, execution_enabled=False), runner=runner) as c:
        r = c.post("/chat", json=_payload())
        assert r.status_code == 200, r.text
        assert r.json()["hermes_invoked"] is False
    assert runner.calls == []  # runner was never invoked


# --- execution enabled uses the (fake) runner ----------------------------
def test_execution_enabled_uses_fake_runner(tmp_path):
    runner = FakeHermesRunner(RunResult(0, "pong from real-ish hermes", ""))
    with _client(_settings(tmp_path, execution_enabled=True), runner=runner) as c:
        r = c.post("/chat", json=_payload(message="ping"))
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["hermes_invoked"] is True
        assert b["reply"] == "pong from real-ish hermes"
        assert b["returncode"] == 0
    # Safe command construction: one call, argv list, message passed as data.
    assert len(runner.calls) == 1
    argv, timeout = runner.calls[0]
    assert argv[0] == "hermes" and "chat" in argv
    assert argv[argv.index("--query") + 1] == "ping"
    assert "--context-file" in argv
    assert timeout == 5.0


# --- execution enabled writes a scope/context file for the runner --------
def test_execution_enabled_writes_scope_context_file(tmp_path):
    runner = FakeHermesRunner()
    with _client(_settings(tmp_path, execution_enabled=True), runner=runner) as c:
        r = c.post("/chat", json=_payload(message="ping"))
        assert r.status_code == 200, r.text
    # The context file existed at runner-call time and carried the scope.
    assert runner.context_existed is True
    payload = runner.context_payload
    assert payload["allowed_tools"] == ["hdfs_list", "incident_report"]
    assert payload["hdfs_roots"] == ["/naplatform/users/alice",
                                     "/naplatform/departments/ER"]
    assert payload["department"] == "ER"
    assert payload["allowed_mcp_servers"] == ["mcp-er-filesystem", "mcp-er-knowledge"]
    assert payload["workspace_root"] == "/naplatform/users/alice"
    assert set(payload) >= {"user", "qdrant_filter", "neo4j_filter"}
    # And it is cleaned up once the request completes.
    assert runner.context_path is not None
    assert not os.path.exists(runner.context_path)


def test_execution_enabled_timeout_maps_to_504(tmp_path):
    runner = FakeHermesRunner(RunResult(None, "", "", timed_out=True))
    with _client(_settings(tmp_path, execution_enabled=True), runner=runner) as c:
        r = c.post("/chat", json=_payload())
        assert r.status_code == 504, r.text


def test_execution_enabled_nonzero_exit_maps_to_502(tmp_path):
    runner = FakeHermesRunner(RunResult(2, "", "boom"))
    with _client(_settings(tmp_path, execution_enabled=True), runner=runner) as c:
        r = c.post("/chat", json=_payload())
        assert r.status_code == 502, r.text


# --- command construction is shell-safe ----------------------------------
def test_build_hermes_argv_keeps_message_as_single_arg():
    argv = build_hermes_argv("hermes", "/p/dir", "ER", "hi; rm -rf /", "/tmp/ctx.json")
    assert argv[0] == "hermes"
    assert "hi; rm -rf /" in argv          # present verbatim as one element
    # the message is the value of --query, never spliced into a shell string
    assert argv[argv.index("--query") + 1] == "hi; rm -rf /"
    assert argv[argv.index("--context-file") + 1] == "/tmp/ctx.json"
