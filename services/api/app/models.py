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
