"""Department Hermes agent routing scaffold (Phase 06).

This module turns an `AgentContext` into a concrete agent invocation. It routes
by department to a *configured* endpoint and either performs a real HTTP JSON
call to the department's Hermes agent or returns a deterministic dry-run
response — no live Hermes is required for tests or offline development.

Safety / SSRF model:
- Endpoints are resolved **only** from a per-department map (env override
  `HERMES_{DEP}_URL` or a default matching the Compose service name). No
  user-supplied URL ever reaches the HTTP client, and the department key is
  validated via `rbac.normalize_department` before lookup.
- Every configured/default URL is re-validated to be an `http(s)://host` URL, so
  a stray `file://`/`gopher://` value can never be dialed.
- Nothing calls a live agent unless `AGENT_ROUTING_ENABLED=true` **and** a URL is
  configured for the department. The default is a deterministic dry run.

The invocation payload carries the full RBAC scope the agent must honor
(allow-listed tools, MCP servers, HDFS roots, Qdrant/Neo4j filters, workspace
root) so the agent never has to re-derive access rules.
"""
import os
from urllib.parse import urlparse
from uuid import uuid4

from .models import DEPARTMENTS, AgentContext, User
from .rbac import normalize_department, user_personal_root

# Default port the department Hermes agent containers are expected to serve on.
DEFAULT_AGENT_PORT = os.environ.get("AGENT_PORT", "8080")

# Env var -> department, and default endpoint per department. The defaults match
# the Compose service names (`hermes-er`, `hermes-it`, ...).
ENV_URL_KEYS = {"ER": "HERMES_ER_URL", "IT": "HERMES_IT_URL",
                "EHS": "HERMES_EHS_URL", "QC": "HERMES_QC_URL"}
DEFAULT_ENDPOINTS = {
    "ER": f"http://hermes-er:{DEFAULT_AGENT_PORT}",
    "IT": f"http://hermes-it:{DEFAULT_AGENT_PORT}",
    "EHS": f"http://hermes-ehs:{DEFAULT_AGENT_PORT}",
    "QC": f"http://hermes-qc:{DEFAULT_AGENT_PORT}",
}

DEFAULT_INVOKE_PATH = "/chat"
# When the primary path 404s, fall back to this one (agents may expose either).
FALLBACK_INVOKE_PATH = "/invoke"
DEFAULT_TIMEOUT_SECONDS = 30.0


class AgentError(Exception):
    """Base class for routing/invocation failures."""


class AgentRoutingError(AgentError):
    """Configuration/resolution failure (unknown or unconfigured department)."""


class AgentTimeout(AgentError):
    """The agent did not respond within the configured timeout (-> 504)."""


class AgentUpstreamError(AgentError):
    """The agent returned an error or was unreachable (-> 502)."""


def routing_enabled() -> bool:
    return os.environ.get("AGENT_ROUTING_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def request_timeout() -> float:
    try:
        return float(os.environ.get("AGENT_REQUEST_TIMEOUT_SECONDS",
                                    str(DEFAULT_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def department_endpoints() -> dict[str, str]:
    """Resolve each department's endpoint: env override or Compose default.

    An env override set to the empty string is treated as *unconfigured* so it
    can be used to explicitly disable routing for a single department.
    """
    out: dict[str, str] = {}
    for dep, default in DEFAULT_ENDPOINTS.items():
        override = os.environ.get(ENV_URL_KEYS[dep])
        out[dep] = default if override is None else override.strip()
    return out


def _validate_endpoint(url: str) -> str:
    """Only allow `http(s)://host[...]` — never a user-controlled/odd scheme."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AgentRoutingError(f"invalid agent endpoint: {url!r}")
    return url.rstrip("/")


def _context_summary(payload: dict) -> dict:
    """A secret-free echo of the routing scope, used by the dry-run response."""
    return {
        "message": payload["message"],
        "username": payload["user"]["username"],
        "department": payload["department"],
        "endpoint": payload.get("endpoint"),
        "workspace_root": payload["workspace_root"],
        "allowed_tools": payload["allowed_tools"],
        "allowed_mcp_servers": payload["allowed_mcp_servers"],
        "hdfs_roots": payload["hdfs_roots"],
        "qdrant_filter": payload["qdrant_filter"],
        "neo4j_filter": payload["neo4j_filter"],
    }


class AgentClient:
    """Abstraction over an agent transport: turn a payload into a reply dict."""

    def invoke(self, url: str, payload: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class DryRunAgentClient(AgentClient):
    """Deterministic fallback: never touches the network."""

    def invoke(self, url: str, payload: dict) -> dict:
        dep = payload["department"]
        summary = _context_summary({**payload, "endpoint": url})
        reply = (f"[dry-run:{dep}] would route '{payload['message']}' for "
                 f"{payload['user']['username']} to {url or 'unconfigured'}; "
                 f"tools={len(payload['allowed_tools'])} "
                 f"mcp={len(payload['allowed_mcp_servers'])} "
                 f"roots={len(payload['hdfs_roots'])}")
        return {"request_id": payload["request_id"], "department": dep,
                "hermes_invoked": False, "reply": reply, "endpoint": url,
                "context_summary": summary}


class HttpAgentClient(AgentClient):
    """HTTP JSON client: POST the payload to `/chat` (falling back to `/invoke`).

    `httpx` is imported lazily so importing this module never hard-requires it,
    and an injected `transport` (e.g. `httpx.MockTransport`) makes the enabled
    path fully testable without a live agent.
    """

    def __init__(self, invoke_path: str = DEFAULT_INVOKE_PATH,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS, *, transport=None):
        self.invoke_path = invoke_path
        self.timeout = timeout
        self.transport = transport

    def invoke(self, url: str, payload: dict) -> dict:
        import httpx  # lazy: keep module import light and optional

        paths = [self.invoke_path]
        if FALLBACK_INVOKE_PATH not in paths:
            paths.append(FALLBACK_INVOKE_PATH)
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                resp = None
                for path in paths:
                    resp = client.post(url + path, json=payload)
                    if resp.status_code != 404:
                        break  # only retry the next path on a hard 404
        except httpx.TimeoutException as e:
            raise AgentTimeout(f"agent request timed out after {self.timeout}s: {e}") from e
        except httpx.HTTPError as e:  # connection/transport errors
            raise AgentUpstreamError(f"agent unreachable at {url}: {e}") from e
        if resp is None or resp.status_code >= 400:
            code = resp.status_code if resp is not None else "no-response"
            raise AgentUpstreamError(f"agent returned {code} from {url}")
        try:
            data = resp.json()
        except ValueError as e:
            raise AgentUpstreamError(f"agent returned non-JSON body from {url}") from e
        reply = data.get("reply") or data.get("message") or ""
        return {"request_id": payload["request_id"], "department": payload["department"],
                "hermes_invoked": True, "reply": reply,
                "endpoint": str(resp.request.url), "upstream": data}


class DepartmentAgentRouter:
    """Routes an `AgentContext` to the configured department Hermes endpoint."""

    def __init__(self, *, enabled: bool | None = None, timeout: float | None = None,
                 endpoints: dict[str, str] | None = None,
                 invoke_path: str | None = None, client: AgentClient | None = None):
        self.enabled = routing_enabled() if enabled is None else enabled
        self.timeout = request_timeout() if timeout is None else timeout
        self.endpoints = endpoints if endpoints is not None else department_endpoints()
        self.invoke_path = invoke_path or os.environ.get(
            "AGENT_INVOKE_PATH", DEFAULT_INVOKE_PATH)
        self._client = client  # explicit override wins over enabled/dry-run selection

    # --- endpoint resolution (SSRF-safe) -----------------------------------
    def endpoint_for(self, department: str) -> str | None:
        """Resolve a validated endpoint for a *known* department, or None.

        Raises `ValueError` for an unknown department (never trusts the caller's
        string as a URL) and returns None when the department has no URL.
        """
        dep = normalize_department(department)  # ValueError on unknown department
        url = (self.endpoints.get(dep) or "").strip()
        if not url:
            return None
        return _validate_endpoint(url)

    def _select_client(self, url: str | None) -> AgentClient:
        if self._client is not None:
            return self._client
        if not self.enabled or not url:
            return DryRunAgentClient()
        return HttpAgentClient(self.invoke_path, self.timeout)

    def build_payload(self, user: User, ctx: AgentContext, message: str,
                      request_id: str, session_id: str | None,
                      endpoint: str | None) -> dict:
        return {
            "request_id": request_id,
            "session_id": session_id,
            "message": message,
            "department": ctx.department,
            "endpoint": endpoint,
            "user": {"user_id": user.id, "username": user.username, "email": user.email},
            "context": ctx.model_dump(),
            "allowed_tools": ctx.allowed_tools,
            "allowed_mcp_servers": ctx.allowed_mcp_servers,
            "hdfs_roots": ctx.hdfs_roots,
            "qdrant_filter": ctx.qdrant_filter,
            "neo4j_filter": ctx.neo4j_filter,
            "workspace_root": user_personal_root(user),
        }

    def invoke(self, user: User, ctx: AgentContext, message: str, *,
               session_id: str | None = None, request_id: str | None = None) -> dict:
        request_id = request_id or str(uuid4())
        endpoint = self.endpoint_for(ctx.department)
        payload = self.build_payload(user, ctx, message, request_id, session_id, endpoint)
        client = self._select_client(endpoint)
        return client.invoke(endpoint or "", payload)

    # --- introspection -----------------------------------------------------
    def status(self) -> dict:
        """Secret-free routing config for the admin status endpoint."""
        departments = {}
        for dep in sorted(DEPARTMENTS):
            url = (self.endpoints.get(dep) or "").strip()
            departments[dep] = {
                "url": url or None,
                "configured": bool(url),
                "source": "env" if os.environ.get(ENV_URL_KEYS[dep]) is not None else "default",
            }
        return {
            "enabled": self.enabled,
            "dry_run": not self.enabled,
            "timeout_seconds": self.timeout,
            "invoke_path": self.invoke_path,
            "fallback_invoke_path": FALLBACK_INVOKE_PATH,
            "departments": departments,
        }


# Shared router instance used by the API endpoints.
agent_router = DepartmentAgentRouter()
