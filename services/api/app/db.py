"""SQLAlchemy engine, session factory, and ORM tables for Phase 03 persistence.

Postgres-ready: point DATABASE_URL at postgresql+psycopg://... in Compose.
SQLite-friendly: the default (and the tests) use an in-memory or file SQLite DB.
No Alembic yet — tables are auto-created on startup for the scaffold.
"""
import os
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, String,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

Base = declarative_base()

DEFAULT_DATABASE_URL = "sqlite://"  # in-memory shared via StaticPool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRow(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)
    email = Column(String, nullable=False, unique=True, index=True)
    username = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    departments = relationship(
        "UserDepartmentRow", back_populates="user",
        cascade="all, delete-orphan", order_by="UserDepartmentRow.id")


class UserDepartmentRow(Base):
    __tablename__ = "user_departments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    department = Column(String, nullable=False)
    user = relationship("UserRow", back_populates="departments")


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=True, index=True)
    actor = Column(String, nullable=True)
    success = Column(Boolean, nullable=False, default=True)
    detail = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow,
                        index=True)


class PasswordResetTokenRow(Base):
    __tablename__ = "password_reset_tokens"
    token = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)


def make_engine(database_url: str | None = None):
    url = database_url or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # A bare in-memory URL would create a fresh DB per connection; pin one
        # shared connection so the schema and rows survive across requests.
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False,
                        future=True)


def init_db(engine) -> None:
    """Create all tables if they do not yet exist (scaffold, no migrations)."""
    Base.metadata.create_all(engine)
