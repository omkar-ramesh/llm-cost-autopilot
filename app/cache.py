"""Response cache. Exact-match on the request payload, backed by Redis.

The interface is deliberately narrow (`get`/`set` on an opaque key) so a semantic cache
can be dropped in without touching the pipeline.
"""

import hashlib
import json
from typing import Any, Protocol

from app.config import get_settings
from app.redis_client import get_redis

KEY_PREFIX = "cache:v1:"

# Fields that change the answer. Anything else (user, metadata) must not split the cache.
CACHEABLE_FIELDS = (
    "model",
    "messages",
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "tools",
    "tool_choice",
    "response_format",
    "seed",
)


class Cache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any]) -> None: ...


def cache_key(payload: dict[str, Any], routed_model: str) -> str:
    """Keyed on the model that actually answers: a `quality` request must never be
    served an answer a cheap model produced for the same prompt."""
    canonical = {f: payload[f] for f in CACHEABLE_FIELDS if f in payload}
    canonical["__routed_model"] = routed_model
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return KEY_PREFIX + hashlib.sha256(blob.encode()).hexdigest()


def is_cacheable(payload: dict[str, Any]) -> bool:
    """Only deterministic, non-streaming requests are safe to reuse."""
    if payload.get("stream"):
        return False
    if payload.get("n", 1) != 1:
        return False
    return payload.get("temperature", 0) in (0, 0.0, None)


class NullCache:
    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        return None


class RedisCache:
    def __init__(self, ttl_s: int):
        self.ttl_s = ttl_s

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = get_redis().get(key)
        except Exception:
            return None  # A cache outage must degrade to a miss, never an error.
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        try:
            get_redis().set(key, json.dumps(value, default=str), ex=self.ttl_s)
        except Exception:
            return None


def get_cache() -> Cache:
    settings = get_settings()
    if not settings.cache_enabled:
        return NullCache()
    return RedisCache(settings.cache_ttl_s)
