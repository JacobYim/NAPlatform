from collections import OrderedDict
from .models import User, UserStatus
from .security import hash_password, new_token
class InMemoryStore:
    def __init__(self): self.users=OrderedDict(); self.sessions={}; self.password_reset_tokens={}
    def seed_admin(self,email="admin@example.com",password="ChangeMe123!"):
        u=self.get_user_by_email(email)
        if u: return u
        u=User(id="admin",email=email,username="admin",password_hash=hash_password(password),status=UserStatus.active,departments=["ER","IT","EHS","QC"],is_admin=True); self.users[u.id]=u; return u
    def add_user(self,user):
        if self.get_user_by_email(user.email): raise ValueError("email already exists")
        if any(u.username==user.username for u in self.users.values()): raise ValueError("username already exists")
        self.users[user.id]=user; return user
    def get_user_by_email(self,email): return next((u for u in self.users.values() if u.email.lower()==email.lower()),None)
    def get_user(self,user_id): return self.users.get(user_id)
    def create_session(self,user_id): token=new_token(); self.sessions[token]=user_id; return token
    def get_session_user(self,token): return self.get_user(self.sessions.get(token,''))
store=InMemoryStore()
