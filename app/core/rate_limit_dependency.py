from app.core.config import get_settings
from app.core.rate_limit import RateLimiter, RedisRateLimiter


def get_rate_limiter() -> RateLimiter:
    settings = get_settings()
    return RedisRateLimiter(settings.redis_url)
