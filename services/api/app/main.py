from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException
from .models import AgentContext, AdminUserUpdate, LoginRequest, SignupRequest, User, UserStatus
from .rbac import AccessDenied, allowed_hdfs_roots, allowed_mcp_servers, allowed_tools, assert_hdfs_path_allowed, neo4j_filter, normalize_department, qdrant_filter
from .security import hash_password, new_token, verify_password
from .store import store
app=FastAPI(title="NAPlatform API",version="0.1.0"); store.seed_admin()
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
@app.get('/agents/{department}/context',response_model=AgentContext)
def context(department:str,user:User=Depends(require_active)):
    try:
        dep=normalize_department(department)
        return AgentContext(user_id=user.id,username=user.username,department=dep,hdfs_roots=allowed_hdfs_roots(user,dep),qdrant_filter=qdrant_filter(user,dep),neo4j_filter=neo4j_filter(user,dep),allowed_tools=allowed_tools(user,dep),allowed_mcp_servers=allowed_mcp_servers(user,dep))
    except (AccessDenied,ValueError) as e: raise HTTPException(403,str(e))
@app.post('/resources/hdfs/check')
def check(path:str,department:str|None=None,user:User=Depends(require_active)):
    try: assert_hdfs_path_allowed(user,path,department); return {"allowed":True}
    except AccessDenied as e: raise HTTPException(403,str(e))
