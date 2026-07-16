import os
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException
from .models import AgentContext, AdminUserUpdate, ChatRequest, ChatResponse, CoreWebUILaunchConfig, HdfsCheckRequest, LoginRequest, PendingApproval, ResourceListResponse, SessionBootstrapResponse, SignupRequest, User, UserStatus
from .rbac import AccessDenied, allowed_hdfs_roots, allowed_mcp_servers, allowed_tools, assert_hdfs_path_allowed, neo4j_filter, normalize_department, qdrant_filter
from .security import hash_password, new_token, verify_password
from .store import store
app=FastAPI(title="NAPlatform API",version="0.1.0"); store.seed_admin(password=os.environ.get("ADMIN_PASSWORD","ChangeMe123!"))
def current_user(authorization:str|None=Header(default=None))->User:
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(401,"missing bearer token")
    u=store.get_session_user(authorization.removeprefix("Bearer ").strip())
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
    except ValueError as e: raise HTTPException(409,str(e))
    return {"id":u.id,"status":u.status,"message":"admin approval required"}
@app.post('/auth/login')
def login(req:LoginRequest):
    u=store.get_user_by_email(req.email)
    if not u or not verify_password(req.password,u.password_hash): raise HTTPException(401,"invalid credentials")
    if u.status!=UserStatus.active: raise HTTPException(403,"user is not active")
    return {"token":store.create_session(u.id),"user_id":u.id,"is_admin":u.is_admin}
@app.post('/auth/password-reset/request')
def password_reset(email:str):
    u=store.get_user_by_email(email)
    if u: store.password_reset_tokens[new_token()]=u.id
    return {"message":"if the email exists, a reset email will be sent"}
@app.get('/admin/users')
def users(_:User=Depends(require_admin)): return [u.model_dump(exclude={'password_hash'}) for u in store.users.values()]
@app.patch('/admin/users/{user_id}')
def update_user(user_id:str, update:AdminUserUpdate, _:User=Depends(require_admin)):
    u=store.get_user(user_id)
    if not u: raise HTTPException(404,"user not found")
    if update.status is not None: u.status=update.status
    if update.departments is not None: u.departments=[normalize_department(d) for d in update.departments]
    if update.password is not None: u.password_hash=hash_password(update.password)
    return u.model_dump(exclude={'password_hash'})
def build_agent_context(user:User,department:str)->AgentContext:
    dep=normalize_department(department)
    return AgentContext(user_id=user.id,username=user.username,department=dep,hdfs_roots=allowed_hdfs_roots(user,dep),qdrant_filter=qdrant_filter(user,dep),neo4j_filter=neo4j_filter(user,dep),allowed_tools=allowed_tools(user,dep),allowed_mcp_servers=allowed_mcp_servers(user,dep))
def core_webui_config()->CoreWebUILaunchConfig:
    return CoreWebUILaunchConfig(brand_name=os.environ.get("BRAND_NAME","HMGMA"),brand_logo=os.environ.get("BRAND_LOGO","/apptoo/branding/logo.jpg"),api_base_url=os.environ.get("NAPLATFORM_API_BASE_URL","http://api:8080"),webui_url=os.environ.get("CORE_WEBUI_URL","http://localhost:3000"))
@app.get('/agents/{department}/context',response_model=AgentContext)
def context(department:str,user:User=Depends(require_active)):
    try: return build_agent_context(user,department)
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
@app.get('/core-webui/session',response_model=SessionBootstrapResponse)
def core_webui_session(user:User=Depends(require_active)):
    deps=[normalize_department(d) for d in user.departments]
    return SessionBootstrapResponse(user_id=user.id,username=user.username,email=user.email,is_admin=user.is_admin,status=user.status,departments=deps,default_department=deps[0] if deps else None,core_webui=core_webui_config())
@app.post('/agents/{department}/chat',response_model=ChatResponse)
def agent_chat(department:str,req:ChatRequest,user:User=Depends(require_active)):
    try: ctx=build_agent_context(user,department)
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
    # Phase 02 stub: real Hermes invocation is wired in the next phase.
    reply=f"[stub:{ctx.department}] received '{req.message}' for {ctx.username}; Hermes invocation pending."
    return ChatResponse(department=ctx.department,user_id=ctx.user_id,username=ctx.username,hdfs_roots=ctx.hdfs_roots,allowed_tools=ctx.allowed_tools,allowed_mcp_servers=ctx.allowed_mcp_servers,reply=reply,hermes_invoked=False)
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
    return [PendingApproval(user_id=u.id,email=u.email,username=u.username,departments=u.departments,status=u.status) for u in store.users.values() if u.status==UserStatus.pending]
