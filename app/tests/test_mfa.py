import pytest

from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.otp_service import generate_otp, hash_otp, verify_otp


def test_generate_otp_is_six_digits():
    code = generate_otp()
    assert len(code) == 6
    assert code.isdigit()


def test_verify_otp_correct_code():
    code = generate_otp()
    assert verify_otp(code, hash_otp(code)) is True


def test_verify_otp_wrong_code():
    assert verify_otp("000000", hash_otp("999999")) is False


async def _setup(db_session, email: str, mfa_enabled: bool = False):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(
            tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U",
            role=Role.OWNER, mfa_enabled=mfa_enabled,
        )
    )
    await db_session.commit()
    return tenant


@pytest.mark.asyncio
async def test_login_without_mfa_returns_token_directly(client, db_session, unique_email):
    await _setup(db_session, unique_email, mfa_enabled=False)
    response = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    assert response.status_code == 200
    assert response.json()["access_token"] is not None
    assert response.json()["mfa_required"] is False


@pytest.mark.asyncio
async def test_login_with_mfa_does_not_return_token(client, db_session, unique_email):
    await _setup(db_session, unique_email, mfa_enabled=True)
    response = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    assert response.status_code == 200
    assert response.json()["access_token"] is None
    assert response.json()["mfa_required"] is True
    assert response.json()["mfa_pending_token"] is not None


@pytest.mark.asyncio
async def test_mfa_pending_token_cannot_access_protected_routes(client, db_session, unique_email):
    """Critique : le token intermédiaire ne doit jamais donner accès à quoi que ce soit."""
    await _setup(db_session, unique_email, mfa_enabled=True)
    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    pending_token = login.json()["mfa_pending_token"]

    response = await client.get("/api/v1/products", headers={"Authorization": f"Bearer {pending_token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_verify_mfa_with_correct_code_succeeds(client, db_session, unique_email):
    from sqlalchemy import select

    await _setup(db_session, unique_email, mfa_enabled=True)

    user = (await db_session.execute(select(User).where(User.email == unique_email))).scalar_one()

    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    pending_token = login.json()["mfa_pending_token"]

    # Le code réel est envoyé par email (jamais exposé côté API) : pour le test, on relit
    # le hash fraîchement généré par le login et on le remplace par un code connu.
    code = "123456"
    await db_session.refresh(user)
    user.mfa_otp_code_hash = hash_otp(code)
    await db_session.commit()

    response = await client.post("/api/v1/auth/verify-mfa", json={"mfa_pending_token": pending_token, "code": code})
    assert response.status_code == 200
    assert response.json()["access_token"] is not None


@pytest.mark.asyncio
async def test_verify_mfa_with_wrong_code_rejected(client, db_session, unique_email):
    await _setup(db_session, unique_email, mfa_enabled=True)
    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    pending_token = login.json()["mfa_pending_token"]

    response = await client.post("/api/v1/auth/verify-mfa", json={"mfa_pending_token": pending_token, "code": "000000"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_verify_mfa_code_single_use(client, db_session, unique_email):
    """Un code déjà utilisé avec succès ne doit jamais pouvoir être réutilisé."""
    from sqlalchemy import select

    await _setup(db_session, unique_email, mfa_enabled=True)
    user = (await db_session.execute(select(User).where(User.email == unique_email))).scalar_one()

    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    pending_token = login.json()["mfa_pending_token"]

    code = "654321"
    await db_session.refresh(user)
    user.mfa_otp_code_hash = hash_otp(code)
    await db_session.commit()

    first = await client.post("/api/v1/auth/verify-mfa", json={"mfa_pending_token": pending_token, "code": code})
    assert first.status_code == 200

    second = await client.post("/api/v1/auth/verify-mfa", json={"mfa_pending_token": pending_token, "code": code})
    assert second.status_code == 401


@pytest.mark.asyncio
async def test_toggle_mfa_on_and_off(client, db_session, unique_email):
    await _setup(db_session, unique_email, mfa_enabled=False)
    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    token = login.json()["access_token"]

    enable = await client.put(
        "/api/v1/auth/mfa/toggle", json={"enabled": True}, headers={"Authorization": f"Bearer {token}"}
    )
    assert enable.status_code == 200

    second_login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    assert second_login.json()["mfa_required"] is True


@pytest.mark.asyncio
async def test_resend_mfa_rate_limited(client, db_session, unique_email):
    await _setup(db_session, unique_email, mfa_enabled=True)
    login = await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})
    pending_token = login.json()["mfa_pending_token"]

    for _ in range(3):
        r = await client.post("/api/v1/auth/mfa/resend", json={"mfa_pending_token": pending_token})
        assert r.status_code == 204

    fourth = await client.post("/api/v1/auth/mfa/resend", json={"mfa_pending_token": pending_token})
    assert fourth.status_code == 429
