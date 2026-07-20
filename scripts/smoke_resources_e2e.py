#!/usr/bin/env python3
"""Phase 09: resource end-to-end smoke test against a live Compose stack.

This script drives the *real* HTTP resource surface of a running NAPlatform stack
(HDFS workspace, HDFS provisioning, the Qdrant vector adapter, the Neo4j graph
adapter, and the audit log) — it is not a unit test. It:

1. waits for the API ``/health``;
2. logs in as the seeded admin, then idempotently creates **and approves** two
   department users — a QC user and an IT user (signup is a no-op if a user
   already exists);
3. asserts ``GET /workspace/hdfs`` for the QC user returns **only** that user's
   own personal root (``/naplatform/users/<username>``) and its own department
   root (``/naplatform/departments/QC``) — never another department's root;
4. asserts ``POST /admin/users/{id}/provision-hdfs`` is a **dry run** — it returns
   the planned ``hdfs dfs`` commands (``targets[].plan[].command`` non-empty) but
   executes nothing (``dry_run=true``, ``enabled=false``, every ``results`` empty);
5. inserts and searches **personal** and **department** vector points as the QC
   user, and proves scoping: the IT user (a different owner, in a different
   department) never sees the QC user's personal or department records;
6. does the same for the **graph** adapter (personal + department nodes);
7. asserts **cross-department denial** — the QC user is ``403`` on ``/vector/IT``,
   ``/graph/IT``, and ``/resources/IT``;
8. asserts the audit log (``GET /admin/audit``) contains the key resource events
   (``vector_insert``/``vector_search``/``graph_insert``/``graph_search``/
   ``hdfs_provision``/``workspace_view``/``admin_user_update``/``login``).

The logic lives in :class:`ResourceSmoke`, which takes an injected HTTP client, so
``services/api/tests/test_smoke_resources_e2e.py`` exercises every step with an
``httpx.MockTransport`` fake API — **no Docker required**.

Safety:
- The script is **idempotent** — re-running it against the same stack is fine.
  Users are reused (signup 409 is treated as success) and all vector/graph records
  are written with **fixed ids**, so a re-run overwrites/re-finds the same data
  instead of accumulating unbounded state.
- It **never prints secrets** (passwords or session tokens): only high-level step
  messages and a non-secret result summary are logged.

Environment (all optional; sensible defaults for a local Compose stack):
- ``SMOKE_API_BASE_URL``      (default ``http://localhost:8080``)
- ``SMOKE_ADMIN_EMAIL``       (default ``admin@example.com``)
- ``SMOKE_ADMIN_PASSWORD`` / ``ADMIN_PASSWORD`` (default ``ChangeMe123!``)
- ``SMOKE_QC_EMAIL``          (default ``qc.res.smoke@example.com``)
- ``SMOKE_QC_USERNAME``       (default ``qcressmoke``)
- ``SMOKE_QC_PASSWORD``       (default ``SmokePass123!``)
- ``SMOKE_IT_EMAIL``          (default ``it.res.smoke@example.com``)
- ``SMOKE_IT_USERNAME``       (default ``itressmoke``)
- ``SMOKE_IT_PASSWORD``       (default ``SmokePass123!``)
- ``SMOKE_VECTOR_COLLECTION`` (default ``smoke_resources``)
- ``SMOKE_GRAPH_LABEL``       (default ``SmokeResource``)
- ``SMOKE_HEALTH_RETRIES``    (default ``30``)
- ``SMOKE_HEALTH_INTERVAL``   (default ``2.0`` seconds)
- ``SMOKE_REQUEST_TIMEOUT``   (default ``30`` seconds)
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass

import httpx

DEFAULT_API_BASE_URL = "http://localhost:8080"
HDFS_BASE = "/naplatform"
# Audit actions the resource flow must have recorded by the time the smoke ends.
REQUIRED_AUDIT_ACTIONS = (
    "login", "admin_user_update", "workspace_view", "hdfs_provision",
    "vector_insert", "vector_search", "graph_insert", "graph_search",
)


class SmokeError(Exception):
    """A smoke assertion or precondition failed."""


@dataclass
class DeptUser:
    """A department user the smoke idempotently creates and approves."""
    email: str
    username: str
    password: str          # never logged
    department: str


@dataclass
class SmokeConfig:
    api_base_url: str = DEFAULT_API_BASE_URL
    admin_email: str = "admin@example.com"
    admin_password: str = "ChangeMe123!"       # overridden by env; never logged
    qc: DeptUser = None                         # type: ignore[assignment]
    it: DeptUser = None                         # type: ignore[assignment]
    vector_collection: str = "smoke_resources"
    graph_label: str = "SmokeResource"
    health_retries: int = 30
    health_interval: float = 2.0
    request_timeout: float = 30.0

    def __post_init__(self):
        if self.qc is None:
            self.qc = DeptUser("qc.res.smoke@example.com", "qcressmoke",
                               "SmokePass123!", "QC")
        if self.it is None:
            self.it = DeptUser("it.res.smoke@example.com", "itressmoke",
                               "SmokePass123!", "IT")

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SmokeConfig":
        env = os.environ if env is None else env
        base = (env.get("SMOKE_API_BASE_URL") or DEFAULT_API_BASE_URL).strip() or DEFAULT_API_BASE_URL
        qc = DeptUser(
            email=(env.get("SMOKE_QC_EMAIL") or "qc.res.smoke@example.com").strip(),
            username=(env.get("SMOKE_QC_USERNAME") or "qcressmoke").strip(),
            password=env.get("SMOKE_QC_PASSWORD") or "SmokePass123!",
            department="QC")
        it = DeptUser(
            email=(env.get("SMOKE_IT_EMAIL") or "it.res.smoke@example.com").strip(),
            username=(env.get("SMOKE_IT_USERNAME") or "itressmoke").strip(),
            password=env.get("SMOKE_IT_PASSWORD") or "SmokePass123!",
            department="IT")
        return cls(
            api_base_url=base.rstrip("/"),
            admin_email=(env.get("SMOKE_ADMIN_EMAIL") or "admin@example.com").strip(),
            admin_password=env.get("SMOKE_ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD") or "ChangeMe123!",
            qc=qc,
            it=it,
            vector_collection=(env.get("SMOKE_VECTOR_COLLECTION") or "smoke_resources").strip(),
            graph_label=(env.get("SMOKE_GRAPH_LABEL") or "SmokeResource").strip(),
            health_retries=int(env.get("SMOKE_HEALTH_RETRIES") or "30"),
            health_interval=float(env.get("SMOKE_HEALTH_INTERVAL") or "2.0"),
            request_timeout=float(env.get("SMOKE_REQUEST_TIMEOUT") or "30"),
        )


def _default_log(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


class ResourceSmoke:
    """Runs the resource E2E smoke steps over an injected ``httpx``-style client.

    ``client`` only needs ``get``/``post``/``patch`` returning objects with
    ``status_code`` and ``json()`` (an ``httpx.Client`` or one backed by
    ``httpx.MockTransport``). ``sleep`` is injected so tests run with no real
    delay.
    """

    def __init__(self, client, config: SmokeConfig, *, sleep=time.sleep, log=None):
        self.client = client
        self.config = config
        self._sleep = sleep
        self.log = log or _default_log

    # --- low-level helpers -------------------------------------------------
    def _url(self, path: str) -> str:
        return self.config.api_base_url + path

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return {}

    # --- health ------------------------------------------------------------
    def wait_for_api(self) -> dict:
        last = "no attempt"
        for attempt in range(1, self.config.health_retries + 1):
            try:
                resp = self.client.get(self._url("/health"), timeout=self.config.request_timeout)
                if resp.status_code == 200:
                    self.log("api healthy")
                    return self._json(resp)
                last = f"status {resp.status_code}"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            self.log(f"waiting for api ({attempt}/{self.config.health_retries}): {last}")
            self._sleep(self.config.health_interval)
        raise SmokeError(f"api not healthy after {self.config.health_retries} attempts: {last}")

    # --- auth / users ------------------------------------------------------
    def login(self, email: str, password: str) -> str:
        resp = self.client.post(self._url("/auth/login"),
                                json={"email": email, "password": password},
                                timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"login failed for {email}: status {resp.status_code}")
        token = self._json(resp).get("token")
        if not token:
            raise SmokeError(f"login for {email} returned no token")
        return token

    def _find_user_id(self, admin_token: str, email: str) -> str | None:
        resp = self.client.get(self._url("/admin/users"), headers=self._auth(admin_token),
                               timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"admin user list failed: status {resp.status_code}")
        for user in self._json(resp):
            if (user.get("email") or "").lower() == email.lower():
                return user.get("id")
        return None

    def ensure_active_user(self, admin_token: str, user: DeptUser) -> str:
        """Idempotently create ``user`` and approve it to active/[department]."""
        resp = self.client.post(self._url("/auth/signup"),
                                json={"email": user.email, "username": user.username,
                                      "password": user.password, "department": user.department},
                                timeout=self.config.request_timeout)
        if resp.status_code == 201:
            self.log(f"created {user.department} user {user.username} (pending approval)")
        elif resp.status_code == 409:
            self.log(f"{user.department} user {user.username} already exists; reusing (idempotent)")
        else:
            raise SmokeError(f"signup for {user.username} failed: status {resp.status_code}")

        user_id = self._find_user_id(admin_token, user.email)
        if not user_id:
            raise SmokeError(f"could not locate {user.department} user {user.email} after signup")

        approve = self.client.patch(self._url(f"/admin/users/{user_id}"),
                                    headers=self._auth(admin_token),
                                    json={"status": "active", "departments": [user.department]},
                                    timeout=self.config.request_timeout)
        if approve.status_code != 200:
            raise SmokeError(f"approving {user.username} failed: status {approve.status_code}")
        self.log(f"{user.department} user {user.username} is active with departments=[{user.department}]")
        return user_id

    # --- HDFS workspace / provisioning ------------------------------------
    def assert_workspace_scoped(self, token: str, user: DeptUser) -> dict:
        """QC user sees only its own personal root + its own department root."""
        resp = self.client.get(self._url("/workspace/hdfs"), headers=self._auth(token),
                               timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"/workspace/hdfs failed: status {resp.status_code}")
        body = self._json(resp)
        expected_personal = f"{HDFS_BASE}/users/{user.username}"
        if body.get("personal_root") != expected_personal:
            raise SmokeError(f"personal_root={body.get('personal_root')!r} "
                             f"expected {expected_personal!r}")
        dept_roots = body.get("department_roots") or []
        expected_dept = f"{HDFS_BASE}/departments/{user.department}"
        if dept_roots != [expected_dept]:
            raise SmokeError(f"department_roots={dept_roots!r} expected [{expected_dept!r}] "
                             f"(must expose only the user's own department root)")
        # Defensive: another department's root must never leak in.
        foreign = f"{HDFS_BASE}/departments/{self.config.it.department}"
        if user.department != self.config.it.department and foreign in dept_roots:
            raise SmokeError(f"foreign department root {foreign!r} leaked into workspace")
        self.log(f"/workspace/hdfs for {user.username} scoped to own personal + "
                 f"[{user.department}] roots only")
        return body

    def assert_provision_dry_run(self, admin_token: str, user_id: str, username: str) -> dict:
        resp = self.client.post(self._url(f"/admin/users/{user_id}/provision-hdfs"),
                                headers=self._auth(admin_token),
                                timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"provision-hdfs failed: status {resp.status_code}")
        body = self._json(resp)
        if body.get("dry_run") is not True or body.get("enabled") is not False:
            raise SmokeError(f"provision-hdfs not a dry run: "
                             f"dry_run={body.get('dry_run')} enabled={body.get('enabled')}")
        targets = body.get("targets") or []
        if not targets:
            raise SmokeError("provision-hdfs returned no targets")
        for t in targets:
            plan = t.get("plan") or []
            if not plan or not all(step.get("command") for step in plan):
                raise SmokeError(f"target {t.get('path')!r} has no planned commands")
            if t.get("results"):
                raise SmokeError(f"target {t.get('path')!r} was executed (results non-empty) "
                                 f"but dry run must execute nothing")
        self.log(f"provision-hdfs for {username} returned {len(targets)} planned dirs, "
                 f"executed nothing (dry run)")
        return body

    # --- vector adapter ----------------------------------------------------
    def _vector_insert(self, token: str, department: str, scope: str, record_id: str):
        return self.client.post(
            self._url(f"/vector/{department}/records"), headers=self._auth(token),
            json={"collection": self.config.vector_collection, "scope": scope,
                  "id": record_id, "payload": {"marker": record_id}},
            timeout=self.config.request_timeout)

    def _vector_search_ids(self, token: str, department: str) -> set[str]:
        resp = self.client.post(
            self._url(f"/vector/{department}/search"), headers=self._auth(token),
            json={"collection": self.config.vector_collection},
            timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"vector search ({department}) failed: status {resp.status_code}")
        return {r.get("id") for r in (self._json(resp).get("results") or [])}

    def assert_vector_scoped(self, qc_token: str, it_token: str) -> None:
        qc_personal, qc_dept = "smoke-qc-vec-personal", "smoke-qc-vec-department"
        it_personal, it_dept = "smoke-it-vec-personal", "smoke-it-vec-department"
        for token, dep, scope, rid in (
                (qc_token, "QC", "personal", qc_personal),
                (qc_token, "QC", "department", qc_dept),
                (it_token, "IT", "personal", it_personal),
                (it_token, "IT", "department", it_dept)):
            resp = self._vector_insert(token, dep, scope, rid)
            if resp.status_code != 201:
                raise SmokeError(f"vector insert {dep}/{scope} failed: status {resp.status_code}")

        qc_hits = self._vector_search_ids(qc_token, "QC")
        if not {qc_personal, qc_dept} <= qc_hits:
            raise SmokeError(f"QC vector search missing own records: {qc_hits}")
        it_hits = self._vector_search_ids(it_token, "IT")
        leaked = it_hits & {qc_personal, qc_dept}
        if leaked:
            raise SmokeError(f"IT vector search leaked QC-scoped records: {leaked}")
        if not {it_personal, it_dept} <= it_hits:
            raise SmokeError(f"IT vector search missing own records: {it_hits}")
        self.log("vector personal/department scope enforced (QC records never visible to IT)")

    # --- graph adapter -----------------------------------------------------
    def _graph_insert(self, token: str, department: str, scope: str, node_id: str):
        return self.client.post(
            self._url(f"/graph/{department}/nodes"), headers=self._auth(token),
            json={"label": self.config.graph_label, "scope": scope,
                  "id": node_id, "properties": {"marker": node_id}},
            timeout=self.config.request_timeout)

    def _graph_search_ids(self, token: str, department: str) -> set[str]:
        resp = self.client.post(
            self._url(f"/graph/{department}/nodes/search"), headers=self._auth(token),
            json={"label": self.config.graph_label},
            timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"graph search ({department}) failed: status {resp.status_code}")
        return {n.get("id") for n in (self._json(resp).get("results") or [])}

    def assert_graph_scoped(self, qc_token: str, it_token: str) -> None:
        qc_personal, qc_dept = "smoke-qc-graph-personal", "smoke-qc-graph-department"
        it_personal, it_dept = "smoke-it-graph-personal", "smoke-it-graph-department"
        for token, dep, scope, nid in (
                (qc_token, "QC", "personal", qc_personal),
                (qc_token, "QC", "department", qc_dept),
                (it_token, "IT", "personal", it_personal),
                (it_token, "IT", "department", it_dept)):
            resp = self._graph_insert(token, dep, scope, nid)
            if resp.status_code != 201:
                raise SmokeError(f"graph insert {dep}/{scope} failed: status {resp.status_code}")

        qc_hits = self._graph_search_ids(qc_token, "QC")
        if not {qc_personal, qc_dept} <= qc_hits:
            raise SmokeError(f"QC graph search missing own nodes: {qc_hits}")
        it_hits = self._graph_search_ids(it_token, "IT")
        leaked = it_hits & {qc_personal, qc_dept}
        if leaked:
            raise SmokeError(f"IT graph search leaked QC-scoped nodes: {leaked}")
        if not {it_personal, it_dept} <= it_hits:
            raise SmokeError(f"IT graph search missing own nodes: {it_hits}")
        self.log("graph personal/department scope enforced (QC nodes never visible to IT)")

    # --- cross-department denial ------------------------------------------
    def assert_cross_department_denied(self, qc_token: str) -> None:
        """The QC user must be 403 on IT's vector, graph, and resource routes."""
        foreign = self.config.it.department
        vec = self.client.post(
            self._url(f"/vector/{foreign}/records"), headers=self._auth(qc_token),
            json={"collection": self.config.vector_collection, "scope": "department",
                  "payload": {"marker": "probe"}},
            timeout=self.config.request_timeout)
        if vec.status_code != 403:
            raise SmokeError(f"QC user vector insert into {foreign} returned "
                             f"{vec.status_code}, expected 403")
        gr = self.client.post(
            self._url(f"/graph/{foreign}/nodes"), headers=self._auth(qc_token),
            json={"label": self.config.graph_label, "scope": "department",
                  "properties": {"marker": "probe"}},
            timeout=self.config.request_timeout)
        if gr.status_code != 403:
            raise SmokeError(f"QC user graph insert into {foreign} returned "
                             f"{gr.status_code}, expected 403")
        res = self.client.get(
            self._url(f"/resources/{foreign}"), headers=self._auth(qc_token),
            timeout=self.config.request_timeout)
        if res.status_code != 403:
            raise SmokeError(f"QC user /resources/{foreign} returned "
                             f"{res.status_code}, expected 403")
        self.log(f"QC user correctly denied {foreign} vector/graph/resource (403)")

    # --- audit -------------------------------------------------------------
    def assert_audit_has_key_events(self, admin_token: str) -> list[str]:
        resp = self.client.get(self._url("/admin/audit?limit=500"),
                               headers=self._auth(admin_token),
                               timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"audit list failed: status {resp.status_code}")
        actions = {e.get("action") for e in (self._json(resp) or [])}
        missing = [a for a in REQUIRED_AUDIT_ACTIONS if a not in actions]
        if missing:
            raise SmokeError(f"audit log missing key events: {missing}")
        self.log(f"audit log contains all key events: {', '.join(REQUIRED_AUDIT_ACTIONS)}")
        return sorted(a for a in actions if a)

    # --- orchestration -----------------------------------------------------
    def run(self) -> dict:
        cfg = self.config
        self.log(f"resource E2E smoke against {cfg.api_base_url}")
        self.wait_for_api()

        admin_token = self.login(cfg.admin_email, cfg.admin_password)
        self.log("admin authenticated")
        qc_id = self.ensure_active_user(admin_token, cfg.qc)
        self.ensure_active_user(admin_token, cfg.it)

        qc_token = self.login(cfg.qc.email, cfg.qc.password)
        it_token = self.login(cfg.it.email, cfg.it.password)
        self.log("QC and IT users authenticated")

        self.assert_workspace_scoped(qc_token, cfg.qc)
        self.assert_provision_dry_run(admin_token, qc_id, cfg.qc.username)
        self.assert_vector_scoped(qc_token, it_token)
        self.assert_graph_scoped(qc_token, it_token)
        self.assert_cross_department_denied(qc_token)
        audit_actions = self.assert_audit_has_key_events(admin_token)

        return {
            "api_base_url": cfg.api_base_url,
            "qc_department": cfg.qc.department,
            "it_department": cfg.it.department,
            "vector_collection": cfg.vector_collection,
            "graph_label": cfg.graph_label,
            "audit_actions": audit_actions,
            "passed": True,
        }


def main(argv: list[str] | None = None) -> int:
    config = SmokeConfig.from_env()
    try:
        with httpx.Client(timeout=config.request_timeout) as client:
            result = ResourceSmoke(client, config).run()
    except SmokeError as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"[smoke] PASSED: {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
