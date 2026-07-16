"""Persistence layer for users, sessions, audit events, and reset tokens.

Phase 03 replaces the in-memory dict store with a SQLAlchemy-backed `Store`
(Postgres-ready, SQLite for tests). Sessions live in a pluggable session store
(Redis or in-memory). The public method surface is kept compatible with the
Phase 02 store so existing imports and tests keep working.
"""
from datetime import timedelta

from sqlalchemy import func, select

from .db import (AuditEventRow, PasswordResetTokenRow, UserDepartmentRow,
                 UserRow, init_db, make_engine, make_session_factory, utcnow)
from .models import AuditEvent, User, UserStatus
from .security import hash_password, new_token
from .session_store import DEFAULT_TTL_SECONDS, SessionStore, build_session_store

RESET_TOKEN_TTL_SECONDS = 3600


class Store:
    def __init__(self, database_url: str | None = None,
                 session_store: SessionStore | None = None):
        self.engine = make_engine(database_url)
        self._Session = make_session_factory(self.engine)
        init_db(self.engine)
        self.sessions = session_store or build_session_store()

    # --- row <-> pydantic conversion ---------------------------------------
    @staticmethod
    def _to_user(row: UserRow | None) -> User | None:
        if row is None:
            return None
        return User(id=row.id, email=row.email, username=row.username,
                    password_hash=row.password_hash, status=UserStatus(row.status),
                    departments=[d.department for d in row.departments],
                    is_admin=row.is_admin)

    # --- users -------------------------------------------------------------
    def seed_admin(self, email="admin@example.com", password="ChangeMe123!") -> User:
        existing = self.get_user_by_email(email)
        if existing:
            return existing
        admin = User(id="admin", email=email, username="admin",
                     password_hash=hash_password(password), status=UserStatus.active,
                     departments=["ER", "IT", "EHS", "QC"], is_admin=True)
        return self.add_user(admin)

    def add_user(self, user: User) -> User:
        with self._Session() as s:
            if s.scalar(select(UserRow).where(
                    func.lower(UserRow.email) == user.email.lower())):
                raise ValueError("email already exists")
            if s.scalar(select(UserRow).where(UserRow.username == user.username)):
                raise ValueError("username already exists")
            row = UserRow(id=user.id, email=user.email, username=user.username,
                          password_hash=user.password_hash, status=user.status.value,
                          is_admin=user.is_admin)
            row.departments = [UserDepartmentRow(department=d) for d in user.departments]
            s.add(row)
            s.commit()
        return user

    def get_user_by_email(self, email: str) -> User | None:
        with self._Session() as s:
            return self._to_user(s.scalar(select(UserRow).where(
                func.lower(UserRow.email) == email.lower())))

    def get_user(self, user_id: str) -> User | None:
        with self._Session() as s:
            return self._to_user(s.get(UserRow, user_id))

    def list_users(self) -> list[User]:
        with self._Session() as s:
            rows = s.scalars(select(UserRow).order_by(UserRow.created_at)).all()
            return [self._to_user(r) for r in rows]

    def list_pending(self) -> list[User]:
        return [u for u in self.list_users() if u.status == UserStatus.pending]

    def update_user(self, user_id: str, *, status: UserStatus | None = None,
                    departments: list[str] | None = None,
                    password_hash: str | None = None) -> User | None:
        with self._Session() as s:
            row = s.get(UserRow, user_id)
            if row is None:
                return None
            if status is not None:
                row.status = status.value
            if password_hash is not None:
                row.password_hash = password_hash
            if departments is not None:
                row.departments = [UserDepartmentRow(department=d) for d in departments]
            s.commit()
            return self._to_user(row)

    # --- sessions ----------------------------------------------------------
    def create_session(self, user_id: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
        return self.sessions.create(user_id, ttl)

    def get_session_user(self, token: str) -> User | None:
        user_id = self.sessions.get(token)
        return self.get_user(user_id) if user_id else None

    # --- password reset tokens --------------------------------------------
    def create_reset_token(self, user_id: str,
                           ttl_seconds: int = RESET_TOKEN_TTL_SECONDS) -> str:
        token = new_token()
        with self._Session() as s:
            s.add(PasswordResetTokenRow(
                token=token, user_id=user_id,
                expires_at=utcnow() + timedelta(seconds=ttl_seconds)))
            s.commit()
        return token

    def get_reset_token(self, token: str) -> PasswordResetTokenRow | None:
        with self._Session() as s:
            return s.get(PasswordResetTokenRow, token)

    def reset_token_valid(self, token: str) -> bool:
        row = self.get_reset_token(token)
        if row is None or row.used:
            return False
        expires = row.expires_at
        if expires.tzinfo is None:
            from datetime import timezone
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > utcnow()

    # --- audit events ------------------------------------------------------
    def add_audit_event(self, action: str, *, user_id: str | None = None,
                        actor: str | None = None, success: bool = True,
                        detail: str | None = None) -> None:
        with self._Session() as s:
            s.add(AuditEventRow(action=action, user_id=user_id, actor=actor,
                                success=success, detail=detail))
            s.commit()

    def list_audit_events(self, limit: int = 100) -> list[AuditEvent]:
        with self._Session() as s:
            rows = s.scalars(
                select(AuditEventRow).order_by(AuditEventRow.created_at.desc(),
                                               AuditEventRow.id.desc()).limit(limit)).all()
            return [AuditEvent(id=r.id, action=r.action, user_id=r.user_id,
                               actor=r.actor, success=r.success, detail=r.detail,
                               created_at=r.created_at) for r in rows]


# Backwards-compatible alias for the Phase 02 name.
InMemoryStore = Store

store = Store()
