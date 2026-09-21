import pytest

from app.core.rate_limit import InMemoryRateLimiter


@pytest.mark.asyncio
async def test_in_memory_rate_limiter_allows_up_to_limit():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        assert await limiter.is_allowed("key", limit=5, window_seconds=60) is True
    assert await limiter.is_allowed("key", limit=5, window_seconds=60) is False


@pytest.mark.asyncio
async def test_in_memory_rate_limiter_isolates_keys():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        assert await limiter.is_allowed("key-a", limit=5, window_seconds=60) is True
    # Une autre clé n'est pas affectée par les tentatives sur key-a.
    assert await limiter.is_allowed("key-b", limit=5, window_seconds=60) is True


@pytest.mark.asyncio
async def test_login_endpoint_rate_limited_by_email(client, db_session):
    for _ in range(5):
        response = await client.post(
            "/api/v1/auth/login", data={"username": "target@example.com", "password": "wrong"}
        )
        assert response.status_code == 401  # échec normal, pas encore limité

    limited = await client.post("/api/v1/auth/login", data={"username": "target@example.com", "password": "wrong"})
    assert limited.status_code == 429


@pytest.mark.asyncio
async def test_login_rate_limit_does_not_affect_other_email(client, db_session):
    for _ in range(5):
        await client.post("/api/v1/auth/login", data={"username": "exhausted@example.com", "password": "wrong"})

    # Un autre email, même IP, n'est pas bloqué par la limite par email (seule la limite par IP,
    # plus large, s'applique — testée séparément).
    response = await client.post("/api/v1/auth/login", data={"username": "other@example.com", "password": "wrong"})
    assert response.status_code == 401
