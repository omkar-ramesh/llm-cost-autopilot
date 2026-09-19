"""Cache interface. Phase 1 is a no-op miss; Phase 6 adds the Redis exact-match backend."""

from typing import Any, Protocol


class Cache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any]) -> None: ...


class NullCache:
    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        return None


def get_cache() -> Cache:
    return NullCache()
