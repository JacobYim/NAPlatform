"""Session store interface with a Redis-backed impl and an in-memory fallback.

Login mints a session token with a TTL. `current_user` resolves the token to a
user id here, then loads the user from the DB. When REDIS_URL is unset (tests)
or Redis cannot be reached, we fall back to the in-memory store so the API stays
runnable and testable without infrastructure.
"""
import logging
import os
import time

from .security import new_token

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "3600"))


def _strict_mode() -> bool:
    return os.environ.get("SESSION_STORE_STRICT", "").strip().lower() in ("1", "true")


class SessionStore:
    """Token -> user_id mapping with a TTL. Backend-agnostic interface."""

    def create(self, user_id: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
        raise NotImplementedError

    def get(self, token: str) -> str | None:
        raise NotImplementedError

    def delete(self, token: str) -> None:
        raise NotImplementedError


class MemorySessionStore(SessionStore):
    def __init__(self):
        self._data: dict[str, tuple[str, float | None]] = {}

    def create(self, user_id: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
        token = new_token()
        expires_at = time.time() + ttl if ttl and ttl > 0 else None
        self._data[token] = (user_id, expires_at)
        return token

    def get(self, token: str) -> str | None:
        entry = self._data.get(token)
        if not entry:
            return None
        user_id, expires_at = entry
        if expires_at is not None and time.time() >= expires_at:
            self._data.pop(token, None)
            return None
        return user_id

    def delete(self, token: str) -> None:
        self._data.pop(token, None)


class RedisSessionStore(SessionStore):
    KEY_PREFIX = "naplatform:session:"

    def __init__(self, redis_url: str):
        import redis  # imported lazily so the package is optional in tests
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        # Fail fast if Redis is unreachable so callers can fall back.
        self._redis.ping()

    def create(self, user_id: str, ttl: int = DEFAULT_TTL_SECONDS) -> str:
        token = new_token()
        key = self.KEY_PREFIX + token
        if ttl and ttl > 0:
            self._redis.setex(key, ttl, user_id)
        else:
            self._redis.set(key, user_id)
        return token

    def get(self, token: str) -> str | None:
        return self._redis.get(self.KEY_PREFIX + token)

    def delete(self, token: str) -> None:
        self._redis.delete(self.KEY_PREFIX + token)


def build_session_store(redis_url: str | None = None) -> SessionStore:
    """Use Redis when REDIS_URL is set and reachable; otherwise in-memory.

    A configured-but-unreachable Redis is never silent: it is logged at WARNING
    with the exception type/message. When SESSION_STORE_STRICT is truthy the
    failure is re-raised as RuntimeError instead of degrading to in-memory, so
    production can refuse to boot without its session backend. The default keeps
    the in-memory fallback for tests and local dev.
    """
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
    if url:
        try:
            return RedisSessionStore(url)
        except Exception as exc:
            # Redis package missing or server unreachable.
            logger.warning(
                "REDIS_URL is set but the session store could not be "
                "initialized (%s: %s); falling back to in-memory sessions",
                type(exc).__name__, exc)
            if _strict_mode():
                raise RuntimeError(
                    "SESSION_STORE_STRICT is set and Redis is unreachable "
                    f"({type(exc).__name__}: {exc})") from exc
    return MemorySessionStore()
