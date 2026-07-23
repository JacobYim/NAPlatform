"""FastAPI service for a department Hermes agent.

Endpoints:
- ``GET /health`` — liveness + which department/profile this container serves.
- ``POST /chat`` / ``POST /invoke`` — accept the API-issued invocation payload
  (the exact shape ``DepartmentAgentRouter`` posts), validate that its
  ``department`` matches this container and that its scope is in-policy, then
  return a deterministic response (``hermes_invoked=false``) — or, when
  ``HERMES_AGENT_EXECUTION_ENABLED=true``, drive the real Hermes CLI.
"""
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .config import Settings
from .executor import (DirectOpenAICompatibleRunner, HermesRunner, SubprocessHermesRunner,
                       build_context_payload, build_hermes_argv,
                       scope_context_file)
from .profile import prepare_profile
from .validation import (ScopeValidationError, assert_hdfs_roots,
                        assert_safe_identifiers, assert_workspace_root)

# Guard against unbounded prompts hitting the model / CLI.
MAX_MESSAGE_CHARS = 20000


class InvokeRequest(BaseModel):
    """The payload posted by the API's ``DepartmentAgentRouter``.

    Unknown keys (e.g. ``endpoint``) are ignored; only the fields the agent
    needs are modelled.
    """
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    department: str = Field(min_length=1)
    request_id: str | None = None
    session_id: str | None = None
    user: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)
    allowed_tools: list = Field(default_factory=list)
    allowed_mcp_servers: list = Field(default_factory=list)
    hdfs_roots: list = Field(default_factory=list)
    qdrant_filter: dict = Field(default_factory=dict)
    neo4j_filter: dict = Field(default_factory=dict)
    workspace_root: str | None = None


def _validate_scope(settings: Settings, req: InvokeRequest) -> None:
    dep = req.department.strip().upper()
    if dep != settings.department:
        raise HTTPException(
            status_code=403,
            detail=f"department mismatch: request '{req.department}' != agent '{settings.department}'")
    try:
        assert_hdfs_roots(req.hdfs_roots)
        assert_safe_identifiers(req.allowed_tools, "allowed_tools")
        assert_safe_identifiers(req.allowed_mcp_servers, "allowed_mcp_servers")
        assert_workspace_root(req.workspace_root)
    except ScopeValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _username(req: InvokeRequest) -> str:
    return (req.user or {}).get("username") or "unknown"


def _execute(settings: Settings, runner: HermesRunner, profile_dir: str,
             req: InvokeRequest, request_id: str) -> dict:
    payload = build_context_payload(
        department=settings.department,
        user=req.user,
        hdfs_roots=req.hdfs_roots,
        allowed_tools=req.allowed_tools,
        allowed_mcp_servers=req.allowed_mcp_servers,
        qdrant_filter=req.qdrant_filter,
        neo4j_filter=req.neo4j_filter,
        workspace_root=req.workspace_root,
    )
    # The temp context file lives only for the runner call, then is removed.
    with scope_context_file(payload) as context_file:
        argv = build_hermes_argv(settings.hermes_bin, profile_dir,
                                 settings.department, req.message, context_file)
        result = runner.run(argv, timeout=settings.execution_timeout)
    if result.timed_out:
        raise HTTPException(status_code=504,
                            detail=f"hermes execution timed out after {settings.execution_timeout}s")
    if result.returncode not in (0, None):
        raise HTTPException(status_code=502,
                            detail=f"hermes exited {result.returncode}: {(result.stderr or '').strip()[:200]}")
    reply = (result.stdout or "").strip() or f"[hermes:{settings.department}] (no output)"
    return {
        "request_id": request_id,
        "department": settings.department,
        "hermes_invoked": True,
        "reply": reply,
        "returncode": result.returncode,
        "profile": settings.profile,
        "command": argv,
    }


def _dry_run(settings: Settings, req: InvokeRequest, request_id: str) -> dict:
    reply = (f"[hermes:{settings.department}:{settings.profile}] received "
             f"'{req.message}' from {_username(req)}; "
             f"tools={len(req.allowed_tools)} mcp={len(req.allowed_mcp_servers)} "
             f"roots={len(req.hdfs_roots)} (execution disabled)")
    return {
        "request_id": request_id,
        "department": settings.department,
        "hermes_invoked": False,
        "reply": reply,
        "profile": settings.profile,
        "scope": {
            "allowed_tools": req.allowed_tools,
            "allowed_mcp_servers": req.allowed_mcp_servers,
            "hdfs_roots": req.hdfs_roots,
            "workspace_root": req.workspace_root,
        },
    }


def _handle(request: Request, req: InvokeRequest) -> dict:
    settings: Settings = request.app.state.settings
    _validate_scope(settings, req)
    request_id = req.request_id or str(uuid4())
    if settings.execution_enabled:
        runner: HermesRunner = request.app.state.runner
        profile_dir: str = request.app.state.profile_dir or (
            f"{settings.hermes_home}/profiles/{settings.profile}")
        return _execute(settings, runner, profile_dir, req, request_id)
    return _dry_run(settings, req, request_id)


def _default_runner(settings: Settings) -> HermesRunner:
    if settings.execution_backend == "hermes-cli":
        return SubprocessHermesRunner()
    if not settings.model_runtime.configured:
        return SubprocessHermesRunner()
    return DirectOpenAICompatibleRunner(
        base_url=settings.model_runtime.base_url,
        model=settings.model_runtime.model,
        department=settings.department,
    )


def build_app(settings: Settings | None = None, *, runner: HermesRunner | None = None,
              prepare_on_startup: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if prepare_on_startup:
            app.state.profile_dir = prepare_profile(settings)
        yield

    app = FastAPI(title="NAPlatform Hermes Agent", lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner or _default_runner(settings)
    app.state.profile_dir = None

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "department": settings.department,
            "profile": settings.profile,
            "execution_enabled": settings.execution_enabled,
            "execution_backend": settings.execution_backend,
            "profile_ready": app.state.profile_dir is not None,
            # Phase 13: shared model runtime (secret-free); shows whether this
            # agent is pointed at the Docker Model Runner / OpenAI-compatible model.
            "model_runtime": settings.model_runtime.status(),
        }

    @app.post("/chat")
    def chat(req: InvokeRequest, request: Request) -> dict:
        return _handle(request, req)

    @app.post("/invoke")
    def invoke(req: InvokeRequest, request: Request) -> dict:
        return _handle(request, req)

    return app


# Module-level ASGI app used by uvicorn in the container.
app = build_app()
