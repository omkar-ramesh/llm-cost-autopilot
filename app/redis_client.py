import redis

from app.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def set_redis(client: redis.Redis | None) -> None:
    """Swap the client (tests inject a fake)."""
    global _client
    _client = client
