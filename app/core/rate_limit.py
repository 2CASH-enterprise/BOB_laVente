"""
Rate limiting abstrait, même principe que LLMClient (section 13) : une interface,
une implémentation Redis pour la production, une implémentation mémoire pour les tests
et le développement sans dépendance externe.
"""
import time
from abc import ABC, abstractmethod


class RateLimiter(ABC):
    @abstractmethod
    async def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        """True si la requête est autorisée, False si la limite est dépassée pour cette fenêtre."""
        raise NotImplementedError


class InMemoryRateLimiter(RateLimiter):
    """Fenêtre fixe en mémoire locale. Convient aux tests et à une instance unique en développement."""

    def __init__(self):
        self._hits: dict[str, list[float]] = {}

    async def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        window_start = now - window_seconds
        hits = [t for t in self._hits.get(key, []) if t > window_start]
        if len(hits) >= limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


class RedisRateLimiter(RateLimiter):
    """
    Implémentation production : compteur Redis à fenêtre fixe (INCR + EXPIRE).
    Cohérent entre plusieurs instances FastAPI derrière un load balancer (section 42).
    """

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client = None

    def _get_client(self):
        if self._client is None:
            import redis.asyncio as redis  # import différé

            self._client = redis.from_url(self.redis_url)
        return self._client

    async def is_allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        client = self._get_client()
        redis_key = f"ratelimit:{key}"
        count = await client.incr(redis_key)
        if count == 1:
            await client.expire(redis_key, window_seconds)
        return count <= limit
