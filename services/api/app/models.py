from datetime import datetime
from enum import Enum
from pydantic import BaseModel, EmailStr, Field
DEPARTMENTS={"ER","IT","EHS","QC"}
class UserStatus(str,Enum): pending="pending"; active="active"; disabled="disabled"
class User(BaseModel):
    id:str; email:EmailStr; username:str; password_hash:str; status:UserStatus=UserStatus.pending; departments:list[str]=Field(default_factory=list); is_admin:bool=False
class SignupRequest(BaseModel):
    email:EmailStr; username:str=Field(min_length=3, pattern=r"^[a-zA-Z0-9_.-]+$"); password:str=Field(min_length=8); department:str
class LoginRequest(BaseModel): email:EmailStr; password:str
class AdminUserUpdate(BaseModel):
    status:UserStatus|None=None; departments:list[str]|None=None; password:str|None=Field(default=None, min_length=8)
class AgentContext(BaseModel):
    user_id:str; username:str; department:str; hdfs_roots:list[str]; qdrant_filter:dict; neo4j_filter:dict; allowed_tools:list[str]; allowed_mcp_servers:list[str]
class CoreWebUILaunchConfig(BaseModel):
    brand_name:str; brand_logo:str; api_base_url:str; webui_url:str
class SessionBootstrapResponse(BaseModel):
    user_id:str; username:str; email:EmailStr; is_admin:bool; status:UserStatus; departments:list[str]; default_department:str|None; core_webui:CoreWebUILaunchConfig
class ChatRequest(BaseModel):
    message:str=Field(min_length=1); session_id:str|None=None
class ChatResponse(BaseModel):
    department:str; user_id:str; username:str; hdfs_roots:list[str]; allowed_tools:list[str]; allowed_mcp_servers:list[str]; reply:str; hermes_invoked:bool=False
class ResourceListResponse(BaseModel):
    department:str; path:str; allowed_roots:list[str]; entries:list[str]
class HdfsCheckRequest(BaseModel):
    path:str; department:str|None=None
class PendingApproval(BaseModel):
    user_id:str; email:EmailStr; username:str; departments:list[str]; status:UserStatus=UserStatus.pending
class AuditEvent(BaseModel):
    id:int; action:str; user_id:str|None=None; actor:str|None=None; success:bool=True; detail:str|None=None; created_at:datetime
class HdfsCommandPlan(BaseModel):
    command:str; description:str; argv:list[str]=Field(default_factory=list)
class HdfsCommandResult(BaseModel):
    command:str; description:str; executed:bool=False; returncode:int|None=None; stdout:str=""; stderr:str=""
class HdfsDirProvision(BaseModel):
    path:str; kind:str; owner:str; group:str; mode:str; plan:list[HdfsCommandPlan]=Field(default_factory=list); results:list[HdfsCommandResult]=Field(default_factory=list)
class HdfsProvisionReport(BaseModel):
    user_id:str|None=None; username:str; enabled:bool; dry_run:bool; targets:list[HdfsDirProvision]=Field(default_factory=list)
class WorkspaceHdfsResponse(BaseModel):
    user_id:str; username:str; personal_root:str; department_roots:list[str]; enabled:bool; dry_run:bool; provisioning_status:str; plan:list[HdfsDirProvision]=Field(default_factory=list)
class VectorInsertRequest(BaseModel):
    collection:str; scope:str; payload:dict=Field(default_factory=dict); id:str|None=None
class VectorRecord(BaseModel):
    id:str; collection:str; scope:str; payload:dict=Field(default_factory=dict); metadata:dict=Field(default_factory=dict)
class VectorInsertResponse(BaseModel):
    department:str; collection:str; record:VectorRecord
class VectorSearchRequest(BaseModel):
    collection:str; query:str|None=None; limit:int=Field(default=10, ge=0, le=1000)
class VectorSearchResponse(BaseModel):
    department:str; collection:str; filter:dict; count:int; results:list[VectorRecord]=Field(default_factory=list)
class GraphNodeInsertRequest(BaseModel):
    label:str; scope:str; properties:dict=Field(default_factory=dict); id:str|None=None
class GraphNode(BaseModel):
    id:str; label:str; scope:str; properties:dict=Field(default_factory=dict); metadata:dict=Field(default_factory=dict)
class GraphNodeInsertResponse(BaseModel):
    department:str; label:str; node:GraphNode
class GraphNodeSearchRequest(BaseModel):
    label:str; query:str|None=None; limit:int=Field(default=10, ge=0, le=1000)
class GraphNodeSearchResponse(BaseModel):
    department:str; label:str; cypher:str; params:dict; count:int; results:list[GraphNode]=Field(default_factory=list)
