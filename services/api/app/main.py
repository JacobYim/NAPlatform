import json
import os
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from .models import AgentContext, AdminUserUpdate, AuditEvent, ChatRequest, ChatResponse, CoreWebUILaunchConfig, DepartmentOptionsResponse, GraphNode, GraphNodeInsertRequest, GraphNodeInsertResponse, GraphNodeSearchRequest, GraphNodeSearchResponse, HdfsCheckRequest, HdfsProvisionReport, LoginRequest, LogoutResponse, PendingApproval, ResourceListResponse, SelectDepartmentRequest, SelectedDepartmentResponse, SessionBootstrapResponse, SessionStatusResponse, SignupRequest, User, UserStatus, VectorInsertRequest, VectorInsertResponse, VectorRecord, VectorSearchRequest, VectorSearchResponse, WorkspaceHdfsResponse
from .hdfs import HdfsProvisioner, ProvisioningError, backend_status as hdfs_backend_status
from .rbac import AccessDenied, allowed_hdfs_roots, allowed_mcp_servers, allowed_tools, assert_hdfs_path_allowed, neo4j_filter, normalize_department, qdrant_filter, user_history_root
from .vector import VectorScopeError, vector_adapter
from .graph import GraphScopeError, graph_adapter
from .agent_router import AgentTimeout, AgentUpstreamError, AgentError, agent_router
from .security import hash_password, verify_password
from . import config, webui
from .store import store
app=FastAPI(title="NAPlatform API",version="0.1.0"); store.seed_admin(password=os.environ.get("ADMIN_PASSWORD",config.DEFAULT_ADMIN_PASSWORD))
# Phase 12: CORS trusted-origin allow-list (safe dev defaults; TRUSTED_ORIGINS in
# production). Credentials are allowed only for a concrete origin list — never for
# a bare wildcard, which browsers reject with credentials anyway.
_ORIGINS=config.trusted_origins()
app.add_middleware(CORSMiddleware,allow_origins=_ORIGINS,allow_credentials="*" not in _ORIGINS,allow_methods=["*"],allow_headers=["*"])
# Phase 12: baseline security headers on every response. HSTS is added only in
# production (it is meaningless/harmful over plain-HTTP local dev).
SECURITY_HEADERS={"X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer","Cross-Origin-Opener-Policy":"same-origin"}
@app.middleware("http")
async def security_headers(request:Request,call_next):
    resp=await call_next(request)
    for k,v in SECURITY_HEADERS.items(): resp.headers.setdefault(k,v)
    if config.production_mode(): resp.headers.setdefault("Strict-Transport-Security","max-age=63072000; includeSubDomains")
    return resp
def bearer_token(authorization:str|None=Header(default=None))->str:
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(401,"missing bearer token")
    return authorization.removeprefix("Bearer ").strip()
def current_user(token:str=Depends(bearer_token))->User:
    u=store.get_session_user(token)
    if not u: raise HTTPException(401,"invalid session")
    return u
def require_active(user:User=Depends(current_user))->User:
    if user.status!=UserStatus.active: raise HTTPException(403,"user is not active")
    return user
def require_admin(user:User=Depends(require_active))->User:
    if not user.is_admin: raise HTTPException(403,"admin required")
    return user
@app.get('/health')
def health(): return {"status":"ok"}
@app.post('/auth/signup',status_code=201)
def signup(req:SignupRequest):
    dep=normalize_department(req.department); u=User(id=str(uuid4()),email=req.email,username=req.username,password_hash=hash_password(req.password),status=UserStatus.pending,departments=[dep])
    try: store.add_user(u)
    except ValueError as e: store.add_audit_event("signup",actor=req.email,success=False,detail=str(e)); raise HTTPException(409,str(e))
    store.add_audit_event("signup",user_id=u.id,actor=u.email,success=True,detail=f"department={dep}")
    return {"id":u.id,"status":u.status,"message":"admin approval required"}
@app.post('/auth/login')
def login(req:LoginRequest):
    u=store.get_user_by_email(req.email)
    if not u or not verify_password(req.password,u.password_hash): store.add_audit_event("login",actor=req.email,success=False,detail="invalid credentials"); raise HTTPException(401,"invalid credentials")
    if u.status!=UserStatus.active: store.add_audit_event("login",user_id=u.id,actor=u.email,success=False,detail="user is not active"); raise HTTPException(403,"user is not active")
    store.add_audit_event("login",user_id=u.id,actor=u.email,success=True)
    return {"token":store.create_session(u.id),"user_id":u.id,"is_admin":u.is_admin}
@app.post('/auth/logout',response_model=LogoutResponse)
def logout(token:str=Depends(bearer_token)):
    # Phase 11: invalidate the session in Redis/memory (idempotent safe logout).
    # Any bearer token is accepted; an unknown/expired token is a no-op that still
    # reports success so the UI can clear its local state. A real session is audited.
    u=store.get_session_user(token); store.delete_session(token)
    if u: store.add_audit_event("logout",user_id=u.id,actor=u.email,success=True)
    return LogoutResponse(message="logged out",invalidated=bool(u))
@app.get('/auth/departments/options',response_model=DepartmentOptionsResponse)
def departments_options():
    # Public: the signup / department-selector dropdown needs the option list
    # before a session exists. No secrets, no per-user data.
    return DepartmentOptionsResponse(departments=webui.department_options())
@app.post('/auth/password-reset/request')
def password_reset(email:str):
    u=store.get_user_by_email(email)
    if u: store.create_reset_token(u.id)
    store.add_audit_event("password_reset_request",user_id=u.id if u else None,actor=email,success=bool(u))
    return {"message":"if the email exists, a reset email will be sent"}
@app.get('/admin/users')
def users(_:User=Depends(require_admin)): return [u.model_dump(exclude={'password_hash'}) for u in store.list_users()]
@app.patch('/admin/users/{user_id}')
def update_user(user_id:str, update:AdminUserUpdate, admin:User=Depends(require_admin)):
    try:
        u=store.update_user(user_id,email=str(update.email) if update.email is not None else None,username=update.username,status=update.status,departments=[normalize_department(d) for d in update.departments] if update.departments is not None else None,password_hash=hash_password(update.password) if update.password is not None else None,is_admin=update.is_admin)
    except ValueError as e:
        store.add_audit_event("admin_user_update",user_id=user_id,actor=admin.email,success=False,detail=str(e)); raise HTTPException(409,str(e))
    if not u: raise HTTPException(404,"user not found")
    sessions_invalidated=store.delete_user_sessions(user_id) if update.reset_sessions else 0
    store.add_audit_event("admin_user_update",user_id=user_id,actor=admin.email,success=True,detail=f"email={update.email} username={update.username} status={update.status} departments={update.departments} is_admin={update.is_admin} password_changed={update.password is not None} reset_sessions={update.reset_sessions} sessions_invalidated={sessions_invalidated}")
    data=u.model_dump(exclude={'password_hash'}); data['sessions_invalidated']=sessions_invalidated
    return data
@app.get('/admin/audit',response_model=list[AuditEvent])
def audit(limit:int=100,_:User=Depends(require_admin)): return store.list_audit_events(limit=limit)
@app.get('/admin/audit/export')
def audit_export(limit:int=Query(default=1000,ge=1,le=10000),action:str|None=None,actor:str|None=None,user_id:str|None=None,success:bool|None=None,format:str=Query(default="json",pattern="^(json|jsonl)$"),admin:User=Depends(require_admin)):
    # Phase 12: admin-only, read-only audit export with optional filters. Returns
    # structured records (default) or JSON-lines (?format=jsonl). Retention is
    # advisory (documented, non-destructive) — this endpoint never deletes rows.
    events=store.export_audit_events(limit=limit,action=action,actor=actor,user_id=user_id,success=success)
    records=[e.model_dump(mode="json") for e in events]
    store.add_audit_event("audit_export",user_id=admin.id,actor=admin.email,success=True,detail=f"count={len(records)} action={action} actor={actor} format={format}")
    if format=="jsonl":
        body="\n".join(json.dumps(r,default=str) for r in records)
        return PlainTextResponse(body,media_type="application/x-ndjson")
    return {"count":len(records),"filters":{"action":action,"actor":actor,"user_id":user_id,"success":success,"limit":limit},"retention":config.retention_policy(),"events":records}
@app.get('/admin/release/readiness')
def release_readiness(_:User=Depends(require_admin)):
    # Phase 12: admin-only, redacted production-readiness + final release checklist.
    # Purely informational — it never promotes `main` and leaks no secrets (checks
    # report booleans / redacted URLs only).
    return config.release_checklist_status()
def build_agent_context(user:User,department:str)->AgentContext:
    dep=normalize_department(department)
    return AgentContext(user_id=user.id,username=user.username,department=dep,hdfs_roots=allowed_hdfs_roots(user,dep),qdrant_filter=qdrant_filter(user,dep),neo4j_filter=neo4j_filter(user,dep),allowed_tools=allowed_tools(user,dep),allowed_mcp_servers=allowed_mcp_servers(user,dep))
def core_webui_config()->CoreWebUILaunchConfig:
    return CoreWebUILaunchConfig(brand_name=os.environ.get("BRAND_NAME","HMGMA"),brand_logo=os.environ.get("BRAND_LOGO","/apptoo/branding/logo.jpg"),api_base_url=os.environ.get("NAPLATFORM_API_BASE_URL","http://api:8080"),webui_url=os.environ.get("CORE_WEBUI_URL","http://localhost:3000"))
@app.get('/agents/{department}/context',response_model=AgentContext)
def context(department:str,user:User=Depends(require_active)):
    try: return build_agent_context(user,department)
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
def build_session_bootstrap(user:User)->SessionBootstrapResponse:
    # Phase 11: the session bootstrap model the core-webui adapter consumes. It
    # carries the department routes so the UI can route chat to
    # /agents/{department}/chat, plus the approval-waiting UX contract.
    deps=[normalize_department(d) for d in user.departments]
    return SessionBootstrapResponse(user_id=user.id,username=user.username,email=user.email,is_admin=user.is_admin,status=user.status,departments=deps,default_department=deps[0] if deps else None,core_webui=core_webui_config(),session_status=user.status.value,department_routes=webui.user_department_routes(user),approval=webui.approval_ux(user))
@app.get('/core-webui/session',response_model=SessionBootstrapResponse)
def core_webui_session(user:User=Depends(require_active)):
    return build_session_bootstrap(user)
@app.get('/auth/me',response_model=SessionBootstrapResponse)
def auth_me(user:User=Depends(require_active)):
    # Alias for /core-webui/session: same bootstrap shape under an auth-namespaced
    # path the adapter can use interchangeably. Preserves the existing contract.
    return build_session_bootstrap(user)
@app.get('/core-webui/session/status',response_model=SessionStatusResponse)
def core_webui_session_status(user:User=Depends(current_user)):
    # Unlike /core-webui/session (active-only, 403 for pending), this resolves for
    # any valid session and reports status so the UI can render the approval-
    # waiting screen for a pending/disabled account. An expired/invalid session is
    # still 401 (via current_user), which the UI treats as "logged out".
    ux=webui.approval_ux(user)
    return SessionStatusResponse(user_id=user.id,username=user.username,email=user.email,is_admin=user.is_admin,status=user.status,can_access=bool(ux["can_access"]),pending=user.status==UserStatus.pending,approval=ux,departments=[normalize_department(d) for d in user.departments])
@app.post('/core-webui/session/select-department',response_model=SelectedDepartmentResponse)
def core_webui_select_department(req:SelectDepartmentRequest,user:User=Depends(require_active)):
    # Validate the department the UI selected for the session and return its chat/
    # context/resource routes. A non-member is denied (403); an unknown department
    # is a bad request (400). Admins may select any department.
    try: route=webui.assert_department_selectable(user,req.department)
    except AccessDenied as e:
        store.add_audit_event("department_select",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(403,str(e))
    except ValueError as e:
        store.add_audit_event("department_select",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(400,str(e))
    store.add_audit_event("department_select",user_id=user.id,actor=user.email,success=True,detail=f"department={route.department}")
    return SelectedDepartmentResponse(department=route.department,is_member=True,route=route)
@app.post('/agents/{department}/chat',response_model=ChatResponse)
def agent_chat(department:str,req:ChatRequest,user:User=Depends(require_active)):
    try: ctx=build_agent_context(user,department)
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
    # Phase 06: route via DepartmentAgentRouter. Default is a deterministic
    # dry run; a real HTTP call happens only when AGENT_ROUTING_ENABLED=true and
    # the department has a configured URL. Timeouts/upstream errors -> 504/502.
    try:
        result=agent_router.invoke(user,ctx,req.message,session_id=req.session_id)
    except AgentTimeout as e:
        store.add_audit_event("agent_chat",user_id=user.id,actor=user.email,success=False,detail=f"department={ctx.department} timeout"); raise HTTPException(504,str(e))
    except AgentUpstreamError as e:
        store.add_audit_event("agent_chat",user_id=user.id,actor=user.email,success=False,detail=f"department={ctx.department} upstream_error"); raise HTTPException(502,str(e))
    except AgentError as e:
        store.add_audit_event("agent_chat",user_id=user.id,actor=user.email,success=False,detail=f"department={ctx.department} routing_error"); raise HTTPException(502,str(e))
    store.add_audit_event("agent_chat",user_id=user.id,actor=user.email,success=True,detail=f"department={ctx.department} hermes_invoked={result['hermes_invoked']} request_id={result['request_id']}")
    return ChatResponse(department=ctx.department,user_id=ctx.user_id,username=ctx.username,hdfs_roots=ctx.hdfs_roots,allowed_tools=ctx.allowed_tools,allowed_mcp_servers=ctx.allowed_mcp_servers,reply=result['reply'],hermes_invoked=result['hermes_invoked'],request_id=result['request_id'])
@app.get('/admin/agents/status')
def agents_status(_:User=Depends(require_admin)): return agent_router.status()
@app.get('/admin/backends/status')
def backends_status(_:User=Depends(require_admin)):
    # Admin-only view of the active vector/graph/hdfs backends. Each adapter's
    # status() is secret-free by construction: URLs are redacted (userinfo/query
    # stripped) and api keys / passwords are only reported as booleans.
    return {"vector":vector_adapter.status(),"graph":graph_adapter.status(),"hdfs":hdfs_backend_status()}
@app.get('/resources/{department}',response_model=ResourceListResponse)
def list_resources(department:str,path:str|None=None,user:User=Depends(require_active)):
    try:
        dep=normalize_department(department); roots=allowed_hdfs_roots(user,dep)
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
    if path is None:
        return ResourceListResponse(department=dep,path="",allowed_roots=roots,entries=roots)
    try: assert_hdfs_path_allowed(user,path,dep)
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
    target=path.rstrip('/') or '/'
    entries=[r for r in roots if r==target or r.startswith(target+'/') or target.startswith(r.rstrip('/')+'/') or target==r.rstrip('/')]
    return ResourceListResponse(department=dep,path=path,allowed_roots=roots,entries=entries or [target])
def _hdfs_check(user:User,path:str,department:str|None):
    try: assert_hdfs_path_allowed(user,path,department); return {"allowed":True}
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
@app.post('/resources/hdfs/check')
def check_post(req:HdfsCheckRequest,user:User=Depends(require_active)):
    return _hdfs_check(user,req.path,req.department)
@app.get('/resources/hdfs/check')
def check(path:str,department:str|None=None,user:User=Depends(require_active)):
    return _hdfs_check(user,path,department)
@app.get('/admin/approvals/pending',response_model=list[PendingApproval])
def pending_approvals(_:User=Depends(require_admin)):
    return [PendingApproval(user_id=u.id,email=u.email,username=u.username,departments=u.departments,status=u.status) for u in store.list_pending()]
@app.post('/admin/users/{user_id}/provision-hdfs',response_model=HdfsProvisionReport)
def provision_hdfs(user_id:str,admin:User=Depends(require_admin)):
    u=store.get_user(user_id)
    if not u: raise HTTPException(404,"user not found")
    try: report=HdfsProvisioner().provision_user(u)
    except (ProvisioningError,ValueError) as e:
        store.add_audit_event("hdfs_provision",user_id=user_id,actor=admin.email,success=False,detail=str(e)); raise HTTPException(400,str(e))
    store.add_audit_event("hdfs_provision",user_id=user_id,actor=admin.email,success=True,detail=f"enabled={report.enabled} dry_run={report.dry_run} dirs={len(report.targets)}")
    return report
@app.get('/workspace/hdfs',response_model=WorkspaceHdfsResponse)
def workspace_hdfs(user:User=Depends(require_active)):
    try: resp=HdfsProvisioner().workspace(user)
    except (ProvisioningError,ValueError) as e: raise HTTPException(400,str(e))
    store.add_audit_event("workspace_view",user_id=user.id,actor=user.email,success=True,detail=f"hdfs status={resp.provisioning_status}")
    return resp

@app.get('/workspace/hdfs/list')
def workspace_hdfs_list(root:str,path:str='.',user:User=Depends(require_active)):
    from .hdfs_web import HdfsBrowserError, list_dir as hdfs_list_dir
    try: return hdfs_list_dir(user,root,path)
    except AccessDenied as e: raise HTTPException(403,str(e))
    except ValueError as e: raise HTTPException(400,str(e))
    except HdfsBrowserError as e: raise HTTPException(e.status_code,e.message)

@app.get('/workspace/hdfs/file')
def workspace_hdfs_file(root:str,path:str,user:User=Depends(require_active)):
    from .hdfs_web import HdfsBrowserError, read_file as hdfs_read_file
    try: return hdfs_read_file(user,root,path)
    except AccessDenied as e: raise HTTPException(403,str(e))
    except ValueError as e: raise HTTPException(400,str(e))
    except HdfsBrowserError as e: raise HTTPException(e.status_code,e.message)

@app.post('/workspace/hdfs/file')
async def workspace_hdfs_file_save(request:Request,user:User=Depends(require_active)):
    from .hdfs_web import HdfsBrowserError, write_file as hdfs_write_file
    body=await request.json()
    try: return hdfs_write_file(user,str(body.get('root') or ''),str(body.get('path') or ''),str(body.get('content') or ''))
    except AccessDenied as e: raise HTTPException(403,str(e))
    except ValueError as e: raise HTTPException(400,str(e))
    except HdfsBrowserError as e: raise HTTPException(e.status_code,e.message)

@app.post('/workspace/hdfs/chat-history')
async def workspace_hdfs_chat_history(request:Request,user:User=Depends(require_active)):
    from datetime import datetime, timezone
    import re
    from .hdfs_web import HdfsBrowserError, write_file as hdfs_write_file
    body=await request.json()
    session_id=re.sub(r'[^A-Za-z0-9_.-]+','_',str(body.get('session_id') or 'session'))[:96] or 'session'
    payload=body.get('payload') or body
    payload.setdefault('username', user.username) if isinstance(payload,dict) else None
    payload.setdefault('saved_at', datetime.now(timezone.utc).isoformat()) if isinstance(payload,dict) else None
    try:
        import json
        return hdfs_write_file(user,user_history_root(user),f'{session_id}.json',json.dumps(payload,ensure_ascii=False,indent=2))
    except AccessDenied as e: raise HTTPException(403,str(e))
    except ValueError as e: raise HTTPException(400,str(e))
    except HdfsBrowserError as e: raise HTTPException(e.status_code,e.message)
# --- Phase 05: Qdrant vector scope adapter -------------------------------
@app.post('/vector/{department}/records',response_model=VectorInsertResponse,status_code=201)
def vector_insert(department:str,req:VectorInsertRequest,user:User=Depends(require_active)):
    try: dep=normalize_department(department)
    except ValueError as e: raise HTTPException(400,str(e))
    try: rec=vector_adapter.insert(user,dep,req.collection,req.payload,req.scope,record_id=req.id)
    except AccessDenied as e:
        store.add_audit_event("vector_insert",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(403,str(e))
    except (VectorScopeError,ValueError) as e:
        store.add_audit_event("vector_insert",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(400,str(e))
    store.add_audit_event("vector_insert",user_id=user.id,actor=user.email,success=True,detail=f"department={dep} collection={req.collection} scope={req.scope}")
    return VectorInsertResponse(department=dep,collection=req.collection,record=VectorRecord(**rec))
def _vector_search(user:User,department:str,collection:str,query:str|None,limit:int)->VectorSearchResponse:
    try: dep=normalize_department(department)
    except ValueError as e: raise HTTPException(400,str(e))
    try:
        filt=vector_adapter.build_filter(user,dep)
        results=vector_adapter.search(user,dep,collection,query=query,limit=limit)
    except AccessDenied as e:
        store.add_audit_event("vector_search",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(403,str(e))
    except (VectorScopeError,ValueError) as e:
        store.add_audit_event("vector_search",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(400,str(e))
    store.add_audit_event("vector_search",user_id=user.id,actor=user.email,success=True,detail=f"department={dep} collection={collection} hits={len(results)}")
    return VectorSearchResponse(department=dep,collection=collection,filter=filt,count=len(results),results=[VectorRecord(**r) for r in results])
@app.post('/vector/{department}/search',response_model=VectorSearchResponse)
def vector_search_post(department:str,req:VectorSearchRequest,user:User=Depends(require_active)):
    return _vector_search(user,department,req.collection,req.query,req.limit)
@app.get('/vector/{department}/search',response_model=VectorSearchResponse)
def vector_search_get(department:str,collection:str,query:str|None=None,limit:int=Query(default=10,ge=0,le=1000),user:User=Depends(require_active)):
    return _vector_search(user,department,collection,query,limit)
# --- Phase 05: Neo4j graph scope adapter ---------------------------------
@app.post('/graph/{department}/nodes',response_model=GraphNodeInsertResponse,status_code=201)
def graph_insert(department:str,req:GraphNodeInsertRequest,user:User=Depends(require_active)):
    try: dep=normalize_department(department)
    except ValueError as e: raise HTTPException(400,str(e))
    try: node=graph_adapter.insert_node(user,dep,req.label,req.properties,req.scope,node_id=req.id)
    except AccessDenied as e:
        store.add_audit_event("graph_insert",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(403,str(e))
    except (GraphScopeError,ValueError) as e:
        store.add_audit_event("graph_insert",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(400,str(e))
    store.add_audit_event("graph_insert",user_id=user.id,actor=user.email,success=True,detail=f"department={dep} label={req.label} scope={req.scope}")
    return GraphNodeInsertResponse(department=dep,label=req.label,node=GraphNode(**node))
def _graph_search(user:User,department:str,label:str,query:str|None,limit:int)->GraphNodeSearchResponse:
    try: dep=normalize_department(department)
    except ValueError as e: raise HTTPException(400,str(e))
    try:
        desc=graph_adapter.build_node_match(user,dep,label)
        results=graph_adapter.search_nodes(user,dep,label,query=query,limit=limit)
    except AccessDenied as e:
        store.add_audit_event("graph_search",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(403,str(e))
    except (GraphScopeError,ValueError) as e:
        store.add_audit_event("graph_search",user_id=user.id,actor=user.email,success=False,detail=str(e)); raise HTTPException(400,str(e))
    store.add_audit_event("graph_search",user_id=user.id,actor=user.email,success=True,detail=f"department={dep} label={label} hits={len(results)}")
    return GraphNodeSearchResponse(department=dep,label=label,cypher=desc["cypher"],params=desc["params"],count=len(results),results=[GraphNode(**n) for n in results])
@app.post('/graph/{department}/nodes/search',response_model=GraphNodeSearchResponse)
def graph_search_post(department:str,req:GraphNodeSearchRequest,user:User=Depends(require_active)):
    return _graph_search(user,department,req.label,req.query,req.limit)
@app.get('/graph/{department}/nodes/search',response_model=GraphNodeSearchResponse)
def graph_search_get(department:str,label:str,query:str|None=None,limit:int=Query(default=10,ge=0,le=1000),user:User=Depends(require_active)):
    return _graph_search(user,department,label,query,limit)
