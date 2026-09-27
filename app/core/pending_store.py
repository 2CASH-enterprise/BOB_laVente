"""
Stockage temporaire côté serveur (lot 21) : une valeur qui ne doit JAMAIS transiter par le
navigateur (ex. un jeton Meta en attente du choix d'un catalogue) et qui expire d'elle-même.
Même principe que RateLimiter : Redis en production, mémoire en test.
"""
import json
import time
from abc import ABC, abstractmethod


class PendingStore(ABC):
    @abstractmethod
    async def put(self, key: str, value: dict, ttl_seconds: int) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> dict | None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...


class InMemoryPendingStore(PendingStore):
    def __init__(self):
        self._items: dict[str, tuple[float, dict]] = {}

    async def put(self, key: str, value: dict, ttl_seconds: int) -> None:
        self._items[key] = (time.time() + ttl_seconds, value)

    async def get(self, key: str) -> dict | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.time() >= expires_at:
            self._items.pop(key, None)
            return None
        return value

    async def delete(self, key: str) -> None:
        self._items.pop(key, None)


class RedisPendingStore(PendingStore):
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client = None

    def _get_client(self):
        if self._client is None:
            import redis.asyncio as redis  # import différé

            self._client = redis.from_url(self.redis_url)
        return self._client

    async def put(self, key: str, value: dict, ttl_seconds: int) -> None:
        await self._get_client().set(f"pending:{key}", json.dumps(value), ex=ttl_seconds)

    async def get(self, key: str) -> dict | None:
        raw = await self._get_client().get(f"pending:{key}")
        return json.loads(raw) if raw else None

    async def delete(self, key: str) -> None:
        await self._get_client().delete(f"pending:{key}")


def get_pending_store() -> PendingStore:
    from app.core.config import get_settings

    return RedisPendingStore(get_settings().redis_url)
