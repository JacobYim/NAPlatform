"""Phase 09: unit tests for the resource E2E smoke *logic* — no Docker required.

The smoke script's steps are exercised against a fake NAPlatform API implemented
with ``httpx.MockTransport``. The fake models exactly what the resource smoke
depends on: admin login, idempotent signup + approval, HDFS workspace scoping,
dry-run HDFS provisioning, the metadata-scoped vector and graph adapters (personal
vs department), cross-department denial, and the audit log. It reproduces the real
scope rule (a record is visible iff the caller owns it, is allowed on it, it
belongs to the active department, or that department is allowed on it), so we can
assert the script:

- reads only the QC user's own personal + department HDFS roots;
- treats provisioning as a dry run (commands planned, nothing executed);
- never surfaces another user's personal record or another department's record;
- gets 403 across ``/vector/IT``, ``/graph/IT``, and ``/resources/IT`` for a QC user;
- finds the key resource events in the audit log;
- is idempotent across re-runs and never logs secrets.
"""
import json

import httpx
import pytest

from smoke_resources_e2e import DeptUser, ResourceSmoke, SmokeConfig, SmokeError

HDFS_BASE = "/naplatform"


def _scope_meta(scope: str, user_id: str, dep: str) -> dict:
    if scope == "personal":
        return {"owner_user_id": user_id}
    return {"department": dep, "allowed_departments": [dep]}


def _visible(meta: dict, user_id: str, dep: str) -> bool:
    return (meta.get("owner_user_id") == user_id
            or user_id in (meta.get("allowed_users") or [])
            or meta.get("department") == dep
            or dep in (meta.get("allowed_departments") or []))


class FakeApi:
    """A minimal in-memory NAPlatform API covering the resource surface.

    ``leak_cross_scope`` (test hook) makes vector/graph search ignore scope so we
    can prove the smoke *fails* when isolation is broken. ``execute_provisioning``
    makes provisioning report executed results so we can prove the dry-run
    assertion catches it.
    """

    def __init__(self, *, admin_password: str = "ChangeMe123!",
                 leak_cross_scope: bool = False, execute_provisioning: bool = False):
        self.admin_password = admin_password
        self.leak_cross_scope = leak_cross_scope
        self.execute_provisioning = execute_provisioning
        self._next_id = 1
        self._audit_id = 0
        self.signup_calls = 0
        self.users: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}
        self.audit: list[dict] = []
        self.vectors: dict[str, list[dict]] = {}   # collection -> points
        self.nodes: dict[str, list[dict]] = {}      # label -> nodes
        self._add_user(email="admin@example.com", username="admin",
                       password=admin_password, status="active",
                       departments=["ER", "IT", "EHS", "QC"], is_admin=True)

    # --- helpers -----------------------------------------------------------
    def _add_user(self, **kw) -> dict:
        uid = f"u{self._next_id}"
        self._next_id += 1
        user = {"id": uid, "is_admin": False, **kw}
        self.users[uid] = user
        return user

    def _by_email(self, email: str) -> dict | None:
        for u in self.users.values():
            if u["email"].lower() == email.lower():
                return u
        return None

    def _user_for_token(self, headers) -> dict | None:
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        uid = self._tokens.get(auth.removeprefix("Bearer ").strip())
        return self.users.get(uid) if uid else None

    def _record(self, action: str, user_id=None, actor=None, success=True):
        self._audit_id += 1
        self.audit.append({"id": self._audit_id, "action": action, "user_id": user_id,
                           "actor": actor, "success": success, "detail": None,
                           "created_at": "2026-07-20T00:00:00Z"})

    def _member(self, user: dict, dep: str) -> bool:
        return user["is_admin"] or dep in user["departments"]

    def _provision_targets(self, user: dict) -> list[dict]:
        results = [{"command": "hdfs dfs -ls /", "executed": True, "returncode": 0}] \
            if self.execute_provisioning else []
        targets = [{"path": f"{HDFS_BASE}/users/{user['username']}", "kind": "personal",
                    "plan": [{"command": f"hdfs dfs -mkdir -p {HDFS_BASE}/users/{user['username']}"},
                             {"command": "hdfs dfs -chmod 700 ..."}],
                    "results": list(results)}]
        for dep in user["departments"]:
            targets.append({"path": f"{HDFS_BASE}/departments/{dep}", "kind": "department",
                            "plan": [{"command": f"hdfs dfs -chmod 770 {HDFS_BASE}/departments/{dep}"}],
                            "results": list(results)})
        return targets

    # --- transport handler -------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = json.loads(request.content.decode()) if request.content else {}
        headers = request.headers

        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})

        if path == "/auth/login" and method == "POST":
            u = self._by_email(body.get("email", ""))
            if not u or u["password"] != body.get("password") or u["status"] != "active":
                self._record("login", actor=body.get("email"), success=False)
                return httpx.Response(401, json={"detail": "invalid credentials"})
            token = f"tok-{u['id']}"
            self._tokens[token] = u["id"]
            self._record("login", user_id=u["id"], actor=u["email"])
            return httpx.Response(200, json={"token": token, "user_id": u["id"],
                                             "is_admin": u["is_admin"]})

        if path == "/auth/signup" and method == "POST":
            self.signup_calls += 1
            if self._by_email(body["email"]) or any(
                    u["username"] == body["username"] for u in self.users.values()):
                return httpx.Response(409, json={"detail": "email already exists"})
            u = self._add_user(email=body["email"], username=body["username"],
                               password=body["password"], status="pending",
                               departments=[body["department"].upper()])
            self._record("signup", user_id=u["id"], actor=u["email"])
            return httpx.Response(201, json={"id": u["id"], "status": "pending",
                                             "message": "admin approval required"})

        # everything below needs a bearer token
        caller = self._user_for_token(headers)

        if path == "/admin/users" and method == "GET":
            if not (caller or {}).get("is_admin"):
                return httpx.Response(403, json={"detail": "admin required"})
            return httpx.Response(200, json=[
                {k: v for k, v in u.items() if k != "password"} for u in self.users.values()])

        if path.startswith("/admin/users/") and path.endswith("/provision-hdfs") and method == "POST":
            if not (caller or {}).get("is_admin"):
                return httpx.Response(403, json={"detail": "admin required"})
            uid = path.split("/")[3]
            u = self.users.get(uid)
            if not u:
                return httpx.Response(404, json={"detail": "user not found"})
            self._record("hdfs_provision", user_id=uid, actor=caller["email"])
            return httpx.Response(200, json={
                "user_id": uid, "username": u["username"],
                "enabled": self.execute_provisioning,
                "dry_run": not self.execute_provisioning,
                "targets": self._provision_targets(u)})

        if path.startswith("/admin/users/") and method == "PATCH":
            if not (caller or {}).get("is_admin"):
                return httpx.Response(403, json={"detail": "admin required"})
            uid = path.rsplit("/", 1)[-1]
            u = self.users.get(uid)
            if not u:
                return httpx.Response(404, json={"detail": "user not found"})
            if body.get("status"):
                u["status"] = body["status"]
            if body.get("departments") is not None:
                u["departments"] = [d.upper() for d in body["departments"]]
            self._record("admin_user_update", user_id=uid, actor=caller["email"])
            return httpx.Response(200, json={k: v for k, v in u.items() if k != "password"})

        if path == "/admin/audit" and method == "GET":
            if not (caller or {}).get("is_admin"):
                return httpx.Response(403, json={"detail": "admin required"})
            return httpx.Response(200, json=list(reversed(self.audit)))

        if path == "/workspace/hdfs" and method == "GET":
            if not caller or caller["status"] != "active":
                return httpx.Response(403, json={"detail": "user is not active"})
            self._record("workspace_view", user_id=caller["id"], actor=caller["email"])
            return httpx.Response(200, json={
                "user_id": caller["id"], "username": caller["username"],
                "personal_root": f"{HDFS_BASE}/users/{caller['username']}",
                "department_roots": [f"{HDFS_BASE}/departments/{d}" for d in caller["departments"]],
                "enabled": False, "dry_run": True, "provisioning_status": "dry_run",
                "plan": self._provision_targets(caller)})

        if path.startswith("/resources/") and method == "GET":
            if not caller or caller["status"] != "active":
                return httpx.Response(403, json={"detail": "user is not active"})
            dep = path.split("/")[2].upper()
            if not self._member(caller, dep):
                return httpx.Response(403, json={"detail": "user is not a member of requested department"})
            return httpx.Response(200, json={"department": dep, "path": "",
                                             "allowed_roots": [], "entries": []})

        # --- vector adapter ------------------------------------------------
        if path.startswith("/vector/") and method == "POST":
            if not caller or caller["status"] != "active":
                return httpx.Response(403, json={"detail": "user is not active"})
            dep = path.split("/")[2].upper()
            if not self._member(caller, dep):
                return httpx.Response(403, json={"detail": "department vector scope denied"})
            if path.endswith("/records"):
                meta = _scope_meta(body["scope"], caller["id"], dep)
                rec = {"id": body.get("id") or f"v{len(self.vectors)}",
                       "collection": body["collection"], "scope": body["scope"],
                       "payload": body.get("payload") or {}, "metadata": meta}
                self.vectors.setdefault(body["collection"], []).append(rec)
                self._record("vector_insert", user_id=caller["id"], actor=caller["email"])
                return httpx.Response(201, json={"department": dep, "collection": body["collection"],
                                                 "record": rec})
            if path.endswith("/search"):
                points = self.vectors.get(body["collection"], [])
                hits = points if self.leak_cross_scope else [
                    p for p in points if _visible(p["metadata"], caller["id"], dep)]
                self._record("vector_search", user_id=caller["id"], actor=caller["email"])
                return httpx.Response(200, json={"department": dep, "collection": body["collection"],
                                                 "filter": {}, "count": len(hits), "results": hits})

        # --- graph adapter -------------------------------------------------
        if path.startswith("/graph/") and method == "POST":
            if not caller or caller["status"] != "active":
                return httpx.Response(403, json={"detail": "user is not active"})
            dep = path.split("/")[2].upper()
            if not self._member(caller, dep):
                return httpx.Response(403, json={"detail": "department graph scope denied"})
            if path.endswith("/nodes"):
                meta = _scope_meta(body["scope"], caller["id"], dep)
                node = {"id": body.get("id") or f"n{len(self.nodes)}", "label": body["label"],
                        "scope": body["scope"], "properties": body.get("properties") or {},
                        "metadata": meta}
                self.nodes.setdefault(body["label"], []).append(node)
                self._record("graph_insert", user_id=caller["id"], actor=caller["email"])
                return httpx.Response(201, json={"department": dep, "label": body["label"],
                                                 "node": node})
            if path.endswith("/nodes/search"):
                nodes = self.nodes.get(body["label"], [])
                hits = nodes if self.leak_cross_scope else [
                    n for n in nodes if _visible(n["metadata"], caller["id"], dep)]
                self._record("graph_search", user_id=caller["id"], actor=caller["email"])
                return httpx.Response(200, json={"department": dep, "label": body["label"],
                                                 "cypher": "MATCH (n) RETURN n", "params": {},
                                                 "count": len(hits), "results": hits})

        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})


def _smoke(fake: FakeApi, *, logs: list | None = None) -> ResourceSmoke:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    config = SmokeConfig(api_base_url="http://api:8080", health_retries=1, health_interval=0,
                         admin_password=fake.admin_password)
    log = (lambda m: logs.append(m)) if logs is not None else (lambda m: None)
    return ResourceSmoke(client, config, sleep=lambda _s: None, log=log)


def test_full_resource_smoke_passes():
    fake = FakeApi()
    result = _smoke(fake).run()
    assert result["passed"] is True
    assert result["qc_department"] == "QC"
    assert result["it_department"] == "IT"
    # All key events landed in the audit log.
    for action in ("vector_insert", "vector_search", "graph_insert", "graph_search",
                   "hdfs_provision", "workspace_view", "admin_user_update", "login"):
        assert action in result["audit_actions"]


def test_workspace_exposes_only_own_roots():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    qc_token = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    body = smoke.assert_workspace_scoped(qc_token, smoke.config.qc)
    assert body["personal_root"] == f"{HDFS_BASE}/users/{smoke.config.qc.username}"
    assert body["department_roots"] == [f"{HDFS_BASE}/departments/QC"]
    assert f"{HDFS_BASE}/departments/IT" not in body["department_roots"]


def test_workspace_scope_fails_if_foreign_root_leaks():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    # Corrupt the QC user so the API reports an IT root too.
    fake._by_email(smoke.config.qc.email)["departments"] = ["QC", "IT"]
    qc_token = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    with pytest.raises(SmokeError):
        smoke.assert_workspace_scoped(qc_token, smoke.config.qc)


def test_provision_is_dry_run_only():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    qc_id = smoke.ensure_active_user(admin, smoke.config.qc)
    body = smoke.assert_provision_dry_run(admin, qc_id, smoke.config.qc.username)
    assert body["dry_run"] is True and body["enabled"] is False
    assert all(not t["results"] for t in body["targets"])


def test_provision_fails_if_commands_executed():
    fake = FakeApi(execute_provisioning=True)
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    qc_id = smoke.ensure_active_user(admin, smoke.config.qc)
    with pytest.raises(SmokeError):
        smoke.assert_provision_dry_run(admin, qc_id, smoke.config.qc.username)


def test_vector_scope_isolates_departments_and_users():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    smoke.ensure_active_user(admin, smoke.config.it)
    qc = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    it = smoke.login(smoke.config.it.email, smoke.config.it.password)
    smoke.assert_vector_scoped(qc, it)  # must not raise


def test_vector_scope_fails_when_isolation_broken():
    fake = FakeApi(leak_cross_scope=True)
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    smoke.ensure_active_user(admin, smoke.config.it)
    qc = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    it = smoke.login(smoke.config.it.email, smoke.config.it.password)
    with pytest.raises(SmokeError):
        smoke.assert_vector_scoped(qc, it)


def test_graph_scope_isolates_departments_and_users():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    smoke.ensure_active_user(admin, smoke.config.it)
    qc = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    it = smoke.login(smoke.config.it.email, smoke.config.it.password)
    smoke.assert_graph_scoped(qc, it)  # must not raise


def test_graph_scope_fails_when_isolation_broken():
    fake = FakeApi(leak_cross_scope=True)
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    smoke.ensure_active_user(admin, smoke.config.it)
    qc = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    it = smoke.login(smoke.config.it.email, smoke.config.it.password)
    with pytest.raises(SmokeError):
        smoke.assert_graph_scoped(qc, it)


def test_cross_department_denied_for_vector_graph_resource():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    qc = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    smoke.assert_cross_department_denied(qc)  # must not raise (all IT routes -> 403)


def test_cross_department_denial_fails_if_it_allowed():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_user(admin, smoke.config.qc)
    # Wrongly grant the QC user IT as well: IT routes now return 200, so the
    # denial assertion must fail.
    fake._by_email(smoke.config.qc.email)["departments"] = ["QC", "IT"]
    qc = smoke.login(smoke.config.qc.email, smoke.config.qc.password)
    with pytest.raises(SmokeError):
        smoke.assert_cross_department_denied(qc)


def test_audit_missing_event_fails():
    fake = FakeApi()
    smoke = _smoke(fake)
    smoke.wait_for_api()
    admin = smoke.login("admin@example.com", fake.admin_password)
    # No resource activity performed, so the audit lacks vector/graph/hdfs events.
    with pytest.raises(SmokeError):
        smoke.assert_audit_has_key_events(admin)


def test_signup_is_idempotent_across_runs():
    fake = FakeApi()
    _smoke(fake).run()
    _smoke(fake).run()  # second run reuses both users (signup 409 treated as ok)
    assert fake.signup_calls == 4  # QC + IT, twice
    for username in ("qcressmoke", "itressmoke"):
        matches = [u for u in fake.users.values() if u["username"] == username]
        assert len(matches) == 1
        assert matches[0]["status"] == "active"


def test_never_logs_secrets():
    fake = FakeApi()
    logs: list = []
    _smoke(fake, logs=logs).run()
    blob = "\n".join(logs)
    assert fake.admin_password not in blob
    assert "SmokePass123!" not in blob
    assert "tok-" not in blob  # session tokens never logged


def test_config_from_env_parses_users():
    env = {
        "SMOKE_API_BASE_URL": "http://api:8080/",
        "SMOKE_QC_EMAIL": "q@example.com", "SMOKE_QC_USERNAME": "quser",
        "SMOKE_IT_EMAIL": "i@example.com", "SMOKE_IT_USERNAME": "iuser",
        "SMOKE_VECTOR_COLLECTION": "custom_col", "SMOKE_GRAPH_LABEL": "CustomLabel",
        "ADMIN_PASSWORD": "s3cret",
    }
    cfg = SmokeConfig.from_env(env)
    assert cfg.api_base_url == "http://api:8080"  # trailing slash stripped
    assert cfg.qc == DeptUser("q@example.com", "quser", "SmokePass123!", "QC")
    assert cfg.it.email == "i@example.com" and cfg.it.department == "IT"
    assert cfg.vector_collection == "custom_col" and cfg.graph_label == "CustomLabel"
    assert cfg.admin_password == "s3cret"
