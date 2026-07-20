"""Phase 08: unit tests for the routing E2E smoke *logic* — no Docker required.

The smoke script's steps are exercised against a fake NAPlatform API implemented
with ``httpx.MockTransport``. The fake models exactly what the smoke script
depends on (health, admin login, idempotent signup + approval, agent status,
department-scoped chat with the routing flag, and cross-department denial), so we
can assert the script:

- reports ``hermes_invoked=true`` only when the stack has routing enabled and
  ``false`` in dry-run mode, and fails loudly on a mismatch;
- treats a duplicate signup (409) as success (idempotent);
- confirms the QC user is denied IT;
- never surfaces a bare exception for an expected 403/409.
"""
import json

import httpx
import pytest

from smoke_routing_e2e import RoutingSmoke, SmokeConfig, SmokeError


class FakeApi:
    """A minimal in-memory NAPlatform API for the smoke script to talk to.

    ``routing_enabled`` mirrors ``AGENT_ROUTING_ENABLED`` on the API: it drives
    both ``/admin/agents/status`` and the ``hermes_invoked`` flag on chat.
    """

    def __init__(self, *, routing_enabled: bool, base_url: str = "http://api:8080",
                 admin_password: str = "ChangeMe123!"):
        self.base_url = base_url
        self.routing_enabled = routing_enabled
        self.admin_password = admin_password
        self._next_id = 1
        self.signup_calls = 0
        self.users: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}
        self._add_user(email="admin@example.com", username="admin",
                       password=admin_password, status="active",
                       departments=["ER", "IT", "EHS", "QC"], is_admin=True)

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

    # --- transport handler -------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = json.loads(request.content.decode()) if request.content else {}
        headers = request.headers

        # health endpoints (api + any hermes-*/health absolute URL)
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "department": "QC"})

        if path == "/auth/login" and method == "POST":
            u = self._by_email(body.get("email", ""))
            if not u or u["password"] != body.get("password") or u["status"] != "active":
                return httpx.Response(401, json={"detail": "invalid credentials"})
            token = f"tok-{u['id']}"
            self._tokens[token] = u["id"]
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
            return httpx.Response(201, json={"id": u["id"], "status": "pending"})

        if path == "/admin/users" and method == "GET":
            if not (self._user_for_token(headers) or {}).get("is_admin"):
                return httpx.Response(403, json={"detail": "admin required"})
            return httpx.Response(200, json=[
                {k: v for k, v in u.items() if k != "password"} for u in self.users.values()])

        if path.startswith("/admin/users/") and method == "PATCH":
            if not (self._user_for_token(headers) or {}).get("is_admin"):
                return httpx.Response(403, json={"detail": "admin required"})
            uid = path.rsplit("/", 1)[-1]
            u = self.users.get(uid)
            if not u:
                return httpx.Response(404, json={"detail": "user not found"})
            if body.get("status"):
                u["status"] = body["status"]
            if body.get("departments") is not None:
                u["departments"] = [d.upper() for d in body["departments"]]
            return httpx.Response(200, json={k: v for k, v in u.items() if k != "password"})

        if path == "/admin/agents/status" and method == "GET":
            if not (self._user_for_token(headers) or {}).get("is_admin"):
                return httpx.Response(403, json={"detail": "admin required"})
            return httpx.Response(200, json={"enabled": self.routing_enabled,
                                             "dry_run": not self.routing_enabled})

        if path.startswith("/agents/") and path.endswith("/chat") and method == "POST":
            u = self._user_for_token(headers)
            if not u or u["status"] != "active":
                return httpx.Response(403, json={"detail": "user is not active"})
            dep = path.split("/")[2].upper()
            if dep not in u["departments"] and not u["is_admin"]:
                return httpx.Response(403, json={"detail": "not a member of department"})
            return httpx.Response(200, json={"department": dep, "user_id": u["id"],
                                             "username": u["username"], "hdfs_roots": [],
                                             "allowed_tools": [], "allowed_mcp_servers": [],
                                             "reply": f"[{dep}] ok",
                                             "hermes_invoked": self.routing_enabled,
                                             "request_id": "rid-smoke"})

        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})


def _smoke(fake: FakeApi, *, expect_routing: bool, logs: list | None = None) -> RoutingSmoke:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    config = SmokeConfig(api_base_url=fake.base_url, expect_routing=expect_routing,
                         health_retries=1, health_interval=0,
                         hermes_health_urls=["http://hermes-qc:8080/health"])
    log = (lambda m: logs.append(m)) if logs is not None else (lambda m: None)
    return RoutingSmoke(client, config, sleep=lambda _s: None, log=log)


def test_dry_run_stack_passes_with_hermes_not_invoked():
    fake = FakeApi(routing_enabled=False)
    result = _smoke(fake, expect_routing=False).run()
    assert result["passed"] is True
    assert result["hermes_invoked"] is False
    assert result["routing_enabled_reported"] is False


def test_enabled_stack_passes_with_hermes_invoked():
    fake = FakeApi(routing_enabled=True)
    result = _smoke(fake, expect_routing=True).run()
    assert result["passed"] is True
    assert result["hermes_invoked"] is True
    assert result["routing_enabled_reported"] is True


def test_expectation_mismatch_fails_loudly():
    # Stack is dry-run but the caller expected routing to be enabled.
    fake = FakeApi(routing_enabled=False)
    with pytest.raises(SmokeError) as exc:
        _smoke(fake, expect_routing=True).run()
    assert "status" in str(exc.value).lower() or "enabled" in str(exc.value).lower()


def test_signup_is_idempotent_across_runs():
    fake = FakeApi(routing_enabled=False)
    _smoke(fake, expect_routing=False).run()
    # Second run reuses the existing QC user (signup returns 409, treated as ok).
    _smoke(fake, expect_routing=False).run()
    assert fake.signup_calls == 2  # called twice...
    # ...but only one QC user exists (plus the seeded admin).
    qc_users = [u for u in fake.users.values() if u["username"] == "qcsmoke"]
    assert len(qc_users) == 1
    assert qc_users[0]["status"] == "active"
    assert qc_users[0]["departments"] == ["QC"]


def test_qc_user_denied_it_department():
    fake = FakeApi(routing_enabled=True)
    smoke = _smoke(fake, expect_routing=True)
    # Drive setup, then assert the IT denial step directly.
    smoke.wait_for_api()
    admin_token = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_qc_user(admin_token)
    qc_token = smoke.login("qc.smoke@example.com", "SmokePass123!")
    smoke.assert_department_denied(qc_token)  # must not raise (IT -> 403)


def test_it_allowed_would_fail_the_denial_assertion():
    # If the API wrongly let a QC user into IT, the smoke must fail.
    fake = FakeApi(routing_enabled=True)
    smoke = _smoke(fake, expect_routing=True)
    smoke.wait_for_api()
    admin_token = smoke.login("admin@example.com", fake.admin_password)
    smoke.ensure_active_qc_user(admin_token)
    # Grant the QC user IT as well, so IT chat now returns 200.
    qc = fake._by_email("qc.smoke@example.com")
    qc["departments"] = ["QC", "IT"]
    qc_token = smoke.login("qc.smoke@example.com", "SmokePass123!")
    with pytest.raises(SmokeError):
        smoke.assert_department_denied(qc_token)


def test_health_retry_then_success(monkeypatch):
    fake = FakeApi(routing_enabled=False)
    logs: list = []
    smoke = _smoke(fake, expect_routing=False, logs=logs)
    smoke.config.health_retries = 3

    calls = {"n": 0}
    real_handler = fake.handler

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("connection refused")
        return real_handler(request)

    smoke.client = httpx.Client(transport=httpx.MockTransport(flaky))
    assert smoke.wait_for_api() == {"status": "ok"}
    assert calls["n"] == 2  # first attempt failed, second succeeded


def test_never_logs_secrets():
    fake = FakeApi(routing_enabled=True)
    logs: list = []
    _smoke(fake, expect_routing=True, logs=logs).run()
    blob = "\n".join(logs)
    assert fake.admin_password not in blob
    assert "SmokePass123!" not in blob
    assert "tok-" not in blob  # session tokens never logged


def test_config_from_env_parses_flags_and_urls():
    env = {
        "SMOKE_API_BASE_URL": "http://api:8080/",
        "SMOKE_EXPECT_ROUTING": "true",
        "SMOKE_HERMES_HEALTH_URLS": "http://hermes-qc:8080/health, http://hermes-er:8080/health",
        "ADMIN_PASSWORD": "s3cret",
    }
    cfg = SmokeConfig.from_env(env)
    assert cfg.api_base_url == "http://api:8080"  # trailing slash stripped
    assert cfg.expect_routing is True
    assert cfg.hermes_health_urls == ["http://hermes-qc:8080/health",
                                      "http://hermes-er:8080/health"]
    assert cfg.admin_password == "s3cret"
