#!/usr/bin/env python3
"""Phase 08: routing end-to-end smoke test against a live Compose stack.

This script drives the *real* HTTP surface of a running NAPlatform stack — it is
not a unit test. It:

1. waits for the API ``/health`` and (when given) each Hermes agent ``/health``;
2. logs in as the seeded admin, then idempotently creates **and approves** a QC
   user (signup is a no-op if the user already exists);
3. logs in as that QC user and calls ``POST /agents/QC/chat``;
4. asserts ``hermes_invoked`` is ``True`` when the routing override is enabled and
   ``False`` in the default dry-run stack (``SMOKE_EXPECT_ROUTING`` controls the
   expectation, and it is cross-checked against ``GET /admin/agents/status``);
5. asserts the QC user is **denied** IT (``POST /agents/IT/chat`` -> ``403``).

The logic lives in :class:`RoutingSmoke`, which takes an injected HTTP client, so
``services/api/tests/test_smoke_routing_e2e.py`` exercises every step with an
``httpx.MockTransport`` fake API — **no Docker required**.

Safety:
- The script is **idempotent** — re-running it against the same stack is fine.
- It **never prints secrets** (passwords or session tokens): only high-level step
  messages and a non-secret result summary are logged.

Environment (all optional; sensible defaults for a local Compose stack):
- ``SMOKE_API_BASE_URL``      (default ``http://localhost:8080``)
- ``SMOKE_ADMIN_EMAIL``       (default ``admin@example.com``)
- ``SMOKE_ADMIN_PASSWORD`` / ``ADMIN_PASSWORD`` (default ``ChangeMe123!``)
- ``SMOKE_QC_EMAIL``          (default ``qc.smoke@example.com``)
- ``SMOKE_QC_USERNAME``       (default ``qcsmoke``)
- ``SMOKE_QC_PASSWORD``       (default ``SmokePass123!``)
- ``SMOKE_EXPECT_ROUTING``    (default ``false`` — set ``true`` for the enabled stack)
- ``SMOKE_HERMES_HEALTH_URLS``(comma-separated; e.g. ``http://hermes-qc:8080/health``)
- ``SMOKE_HEALTH_RETRIES``    (default ``30``)
- ``SMOKE_HEALTH_INTERVAL``   (default ``2.0`` seconds)
- ``SMOKE_REQUEST_TIMEOUT``   (default ``30`` seconds)
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_API_BASE_URL = "http://localhost:8080"
DEFAULT_DEPARTMENT = "QC"
DEFAULT_DENIED_DEPARTMENT = "IT"
_TRUTHY = ("1", "true", "yes", "on")


class SmokeError(Exception):
    """A smoke assertion or precondition failed."""


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


@dataclass
class SmokeConfig:
    api_base_url: str = DEFAULT_API_BASE_URL
    admin_email: str = "admin@example.com"
    admin_password: str = "ChangeMe123!"      # overridden by env; never logged
    qc_email: str = "qc.smoke@example.com"
    qc_username: str = "qcsmoke"
    qc_password: str = "SmokePass123!"         # never logged
    department: str = DEFAULT_DEPARTMENT
    denied_department: str = DEFAULT_DENIED_DEPARTMENT
    expect_routing: bool = False
    hermes_health_urls: list[str] = field(default_factory=list)
    health_retries: int = 30
    health_interval: float = 2.0
    request_timeout: float = 30.0

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SmokeConfig":
        env = os.environ if env is None else env
        urls = [u.strip() for u in (env.get("SMOKE_HERMES_HEALTH_URLS") or "").split(",") if u.strip()]
        base = (env.get("SMOKE_API_BASE_URL") or DEFAULT_API_BASE_URL).strip() or DEFAULT_API_BASE_URL
        return cls(
            api_base_url=base.rstrip("/"),
            admin_email=(env.get("SMOKE_ADMIN_EMAIL") or "admin@example.com").strip(),
            admin_password=env.get("SMOKE_ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD") or "ChangeMe123!",
            qc_email=(env.get("SMOKE_QC_EMAIL") or "qc.smoke@example.com").strip(),
            qc_username=(env.get("SMOKE_QC_USERNAME") or "qcsmoke").strip(),
            qc_password=env.get("SMOKE_QC_PASSWORD") or "SmokePass123!",
            expect_routing=_as_bool(env.get("SMOKE_EXPECT_ROUTING")),
            hermes_health_urls=urls,
            health_retries=int(env.get("SMOKE_HEALTH_RETRIES") or "30"),
            health_interval=float(env.get("SMOKE_HEALTH_INTERVAL") or "2.0"),
            request_timeout=float(env.get("SMOKE_REQUEST_TIMEOUT") or "30"),
        )


def _default_log(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


class RoutingSmoke:
    """Runs the routing E2E smoke steps over an injected ``httpx``-style client.

    ``client`` only needs ``get``/``post``/``patch`` returning objects with
    ``status_code`` and ``json()`` (an ``httpx.Client`` or an ``httpx.Client``
    backed by ``httpx.MockTransport``). ``sleep`` is injected so tests run with no
    real delay.
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

    def _wait_for(self, url: str, label: str) -> dict:
        last = "no attempt"
        for attempt in range(1, self.config.health_retries + 1):
            try:
                resp = self.client.get(url, timeout=self.config.request_timeout)
                if resp.status_code == 200:
                    self.log(f"{label} healthy")
                    return self._json(resp)
                last = f"status {resp.status_code}"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            self.log(f"waiting for {label} ({attempt}/{self.config.health_retries}): {last}")
            self._sleep(self.config.health_interval)
        raise SmokeError(f"{label} not healthy after {self.config.health_retries} attempts: {last}")

    # --- steps -------------------------------------------------------------
    def wait_for_api(self) -> dict:
        return self._wait_for(self._url("/health"), "api")

    def wait_for_hermes(self) -> None:
        if not self.config.hermes_health_urls:
            self.log("no SMOKE_HERMES_HEALTH_URLS given; skipping direct hermes health checks")
            return
        for url in self.config.hermes_health_urls:
            body = self._wait_for(url, f"hermes ({url})")
            dep = body.get("department")
            if dep:
                self.log(f"hermes at {url} serves department {dep}")

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

    def ensure_active_qc_user(self, admin_token: str) -> str:
        """Idempotently create the QC user and approve it to active/[QC]."""
        cfg = self.config
        resp = self.client.post(self._url("/auth/signup"),
                                json={"email": cfg.qc_email, "username": cfg.qc_username,
                                      "password": cfg.qc_password, "department": cfg.department},
                                timeout=cfg.request_timeout)
        if resp.status_code == 201:
            self.log(f"created QC user {cfg.qc_username} (pending approval)")
        elif resp.status_code == 409:
            self.log(f"QC user {cfg.qc_username} already exists; reusing (idempotent)")
        else:
            raise SmokeError(f"signup for {cfg.qc_username} failed: status {resp.status_code}")

        user_id = self._find_user_id(admin_token, cfg.qc_email)
        if not user_id:
            raise SmokeError(f"could not locate QC user {cfg.qc_email} after signup")

        approve = self.client.patch(self._url(f"/admin/users/{user_id}"),
                                    headers=self._auth(admin_token),
                                    json={"status": "active", "departments": [cfg.department]},
                                    timeout=cfg.request_timeout)
        if approve.status_code != 200:
            raise SmokeError(f"approving QC user failed: status {approve.status_code}")
        self.log(f"QC user {cfg.qc_username} is active with departments=[{cfg.department}]")
        return user_id

    def routing_enabled_reported(self, admin_token: str) -> bool:
        resp = self.client.get(self._url("/admin/agents/status"), headers=self._auth(admin_token),
                               timeout=self.config.request_timeout)
        if resp.status_code != 200:
            raise SmokeError(f"agent status failed: status {resp.status_code}")
        return bool(self._json(resp).get("enabled"))

    def chat(self, token: str, department: str, message: str):
        return self.client.post(self._url(f"/agents/{department}/chat"),
                                headers=self._auth(token),
                                json={"message": message},
                                timeout=self.config.request_timeout)

    def assert_chat_routing(self, token: str) -> dict:
        cfg = self.config
        resp = self.chat(token, cfg.department, "smoke: quality trace status check")
        if resp.status_code != 200:
            raise SmokeError(f"QC chat failed: status {resp.status_code}")
        body = self._json(resp)
        invoked = bool(body.get("hermes_invoked"))
        if invoked != cfg.expect_routing:
            raise SmokeError(
                f"hermes_invoked={invoked} but expected {cfg.expect_routing} "
                f"(routing {'enabled' if cfg.expect_routing else 'dry-run'})")
        self.log(f"QC chat ok: hermes_invoked={invoked} (as expected)")
        return body

    def assert_department_denied(self, token: str) -> None:
        cfg = self.config
        resp = self.chat(token, cfg.denied_department, "smoke: cross-department probe")
        if resp.status_code != 403:
            raise SmokeError(
                f"{cfg.denied_department} chat for QC user returned {resp.status_code}, expected 403")
        self.log(f"QC user correctly denied {cfg.denied_department} (403)")

    def run(self) -> dict:
        cfg = self.config
        self.log(f"routing E2E smoke against {cfg.api_base_url} "
                 f"(expect_routing={cfg.expect_routing})")
        self.wait_for_api()
        self.wait_for_hermes()
        admin_token = self.login(cfg.admin_email, cfg.admin_password)
        self.log("admin authenticated")
        self.ensure_active_qc_user(admin_token)

        reported = self.routing_enabled_reported(admin_token)
        if reported != cfg.expect_routing:
            raise SmokeError(
                f"/admin/agents/status reports enabled={reported} "
                f"but SMOKE_EXPECT_ROUTING={cfg.expect_routing}")
        self.log(f"/admin/agents/status enabled={reported} matches expectation")

        qc_token = self.login(cfg.qc_email, cfg.qc_password)
        self.log("QC user authenticated")
        chat = self.assert_chat_routing(qc_token)
        self.assert_department_denied(qc_token)

        return {
            "api_base_url": cfg.api_base_url,
            "expect_routing": cfg.expect_routing,
            "hermes_invoked": bool(chat.get("hermes_invoked")),
            "routing_enabled_reported": reported,
            "denied_department": cfg.denied_department,
            "department": cfg.department,
            "passed": True,
        }


def main(argv: list[str] | None = None) -> int:
    config = SmokeConfig.from_env()
    try:
        with httpx.Client(timeout=config.request_timeout) as client:
            result = RoutingSmoke(client, config).run()
    except SmokeError as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"[smoke] PASSED: {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
