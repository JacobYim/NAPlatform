"""Phase 03: DB persistence, session TTL, audit events, reset-token expiry."""
import logging
import time

import pytest

from app.models import User, UserStatus
from app.security import hash_password
from app.session_store import MemorySessionStore, build_session_store
from app.store import Store


def _user(email="p1@example.com", username="puser"):
    return User(id="p1", email=email, username=username,
                password_hash=hash_password("Password123!"),
                status=UserStatus.active, departments=["IT"])


def test_users_persist_across_store_instances(tmp_path):
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    s1 = Store(database_url=url, session_store=MemorySessionStore())
    s1.add_user(_user())
    # A brand new Store pointing at the same file must see the committed row.
    s2 = Store(database_url=url, session_store=MemorySessionStore())
    u = s2.get_user_by_email("p1@example.com")
    assert u is not None and u.username == "puser" and u.departments == ["IT"]


def test_duplicate_email_rejected_after_persist(tmp_path):
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    Store(database_url=url, session_store=MemorySessionStore()).add_user(_user())
    s2 = Store(database_url=url, session_store=MemorySessionStore())
    try:
        s2.add_user(_user(username="other"))
        assert False, "expected duplicate email to raise"
    except ValueError as e:
        assert "email" in str(e)


def test_email_match_is_exact_not_like_wildcard(tmp_path):
    # `_` is a single-char wildcard in SQL LIKE/ILIKE. Exact case-insensitive
    # matching must treat foo_bar and fooXbar as distinct addresses, and a
    # lookup must return the exact normalized email that was stored.
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    s = Store(database_url=url, session_store=MemorySessionStore())
    s.add_user(User(id="u1", email="foo_bar@example.com", username="u1",
                    password_hash=hash_password("Password123!"),
                    status=UserStatus.active, departments=["IT"]))
    # A different address that would collide under a LIKE wildcard must be
    # accepted as a separate user, not rejected as a duplicate.
    s.add_user(User(id="u2", email="fooXbar@example.com", username="u2",
                    password_hash=hash_password("Password123!"),
                    status=UserStatus.active, departments=["IT"]))
    u1 = s.get_user_by_email("FOO_BAR@example.com")  # case-insensitive
    u2 = s.get_user_by_email("fooXbar@example.com")
    assert u1 is not None and u1.id == "u1" and u1.email == "foo_bar@example.com"
    assert u2 is not None and u2.id == "u2" and u2.email == "fooXbar@example.com"


def test_duplicate_email_is_case_insensitive(tmp_path):
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    s = Store(database_url=url, session_store=MemorySessionStore())
    s.add_user(_user(email="Mixed@Example.com"))
    with pytest.raises(ValueError, match="email"):
        s.add_user(_user(username="other", email="mixed@example.com"))


def test_strict_mode_raises_when_redis_unreachable(monkeypatch):
    # SESSION_STORE_STRICT must turn an unreachable Redis into a hard failure
    # instead of a silent in-memory fallback.
    monkeypatch.setenv("SESSION_STORE_STRICT", "true")
    with pytest.raises(RuntimeError):
        build_session_store("redis://127.0.0.1:6390/0")


def test_unreachable_redis_logs_warning(monkeypatch, caplog):
    monkeypatch.delenv("SESSION_STORE_STRICT", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.session_store"):
        store = build_session_store("redis://127.0.0.1:6390/0")
    assert isinstance(store, MemorySessionStore)
    assert any(r.levelno == logging.WARNING and "REDIS_URL" in r.getMessage()
               for r in caplog.records)


def test_update_user_persists(tmp_path):
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    s1 = Store(database_url=url, session_store=MemorySessionStore())
    s1.add_user(_user())
    s1.update_user("p1", status=UserStatus.disabled, departments=["QC", "ER"])
    s2 = Store(database_url=url, session_store=MemorySessionStore())
    u = s2.get_user("p1")
    assert u.status == UserStatus.disabled and u.departments == ["QC", "ER"]


def test_memory_session_ttl_expires():
    ss = MemorySessionStore()
    token = ss.create("p1", ttl=1)
    assert ss.get(token) == "p1"
    time.sleep(1.1)
    assert ss.get(token) is None


def test_build_session_store_falls_back_to_memory_without_redis():
    # No REDIS_URL -> in-memory fallback keeps the API testable without Redis.
    assert isinstance(build_session_store(None), MemorySessionStore)


def test_build_session_store_falls_back_when_redis_unreachable():
    # An unreachable Redis URL must not crash; it degrades to in-memory.
    assert isinstance(build_session_store("redis://127.0.0.1:6390/0"),
                      MemorySessionStore)


def test_audit_events_recorded_and_listed(tmp_path):
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    s = Store(database_url=url, session_store=MemorySessionStore())
    s.add_audit_event("login", user_id="p1", actor="p1@example.com", success=True)
    s.add_audit_event("login", actor="bad@example.com", success=False,
                      detail="invalid credentials")
    events = s.list_audit_events()
    assert len(events) == 2
    actions = {e.action for e in events}
    assert actions == {"login"}
    assert any(e.success is False for e in events)


def test_reset_token_persists_with_expiry(tmp_path):
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    s1 = Store(database_url=url, session_store=MemorySessionStore())
    s1.add_user(_user())
    token = s1.create_reset_token("p1", ttl_seconds=3600)
    # Token row survives into a fresh Store instance and carries an expiry.
    s2 = Store(database_url=url, session_store=MemorySessionStore())
    row = s2.get_reset_token(token)
    assert row is not None and row.user_id == "p1" and row.expires_at is not None
    assert s2.reset_token_valid(token) is True


def test_reset_token_expired_is_invalid(tmp_path):
    url = f"sqlite:///{tmp_path / 'nap.db'}"
    s = Store(database_url=url, session_store=MemorySessionStore())
    s.add_user(_user())
    token = s.create_reset_token("p1", ttl_seconds=-1)  # already expired
    assert s.reset_token_valid(token) is False
