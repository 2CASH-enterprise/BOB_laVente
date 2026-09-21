import pytest

from app.core.security import hash_password
from app.integrations.whatsapp.dependency import get_meta_oauth_client
from app.main import app
from app.models.tenant import Tenant
from app.models.user import Role, User


class FakeMetaOAuthClient:
    def __init__(self, token="fake-exchanged-token", raise_on_exchange=False, raise_on_subscribe=False):
        self.token = token
        self.raise_on_exchange = raise_on_exchange
        self.raise_on_subscribe = raise_on_subscribe
        self.exchanged_codes = []
        self.subscribed_wabas = []

    async def exchange_code_for_token(self, code: str) -> str:
        if self.raise_on_exchange:
            raise RuntimeError("Code invalide ou expiré")
        self.exchanged_codes.append(code)
        return self.token

    async def subscribe_app_to_waba(self, waba_id: str, access_token: str) -> None:
        if self.raise_on_subscribe:
            raise RuntimeError("Échec de l'abonnement au WABA")
        self.subscribed_wabas.append((waba_id, access_token))


async def _setup(db_session, email: str, role: Role = Role.OWNER):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role)
    )
    await db_session.commit()
    return tenant


async def _login(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_embedded_signup_exchanges_code_and_subscribes_waba(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    fake = FakeMetaOAuthClient(token="real-looking-token")
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        response = await client.post(
            "/api/v1/whatsapp/embedded-signup/callback",
            json={"code": "auth-code-123", "waba_id": "waba_999", "phone_number_id": "phone_999"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert fake.exchanged_codes == ["auth-code-123"]
        assert fake.subscribed_wabas == [("waba_999", "real-looking-token")]
    finally:
        del app.dependency_overrides[get_meta_oauth_client]

    account = await client.get("/api/v1/whatsapp/account", headers={"Authorization": f"Bearer {token}"})
    assert account.json()["waba_id"] == "waba_999"


@pytest.mark.asyncio
async def test_embedded_signup_stores_real_token_not_placeholder(client, db_session, unique_email):
    """Le token enregistré doit être celui réellement échangé, jamais un placeholder (section 59.6)."""
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    fake = FakeMetaOAuthClient(token="sk-real-system-user-token")
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        await client.post(
            "/api/v1/whatsapp/embedded-signup/callback",
            json={"code": "c", "waba_id": "w", "phone_number_id": "p"},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        del app.dependency_overrides[get_meta_oauth_client]

    from sqlalchemy import select

    from app.models.whatsapp_account import WhatsAppAccount

    account = (await db_session.execute(select(WhatsAppAccount))).scalars().first()
    assert account.system_user_token == "sk-real-system-user-token"
    assert not account.system_user_token.startswith("PENDING_EXCHANGE")


@pytest.mark.asyncio
async def test_embedded_signup_exchange_failure_returns_400(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    fake = FakeMetaOAuthClient(raise_on_exchange=True)
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        response = await client.post(
            "/api/v1/whatsapp/embedded-signup/callback",
            json={"code": "bad-code", "waba_id": "w", "phone_number_id": "p"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
    finally:
        del app.dependency_overrides[get_meta_oauth_client]

    # Aucun compte ne doit être créé en cas d'échec de l'échange
    account_check = await client.get("/api/v1/whatsapp/account", headers={"Authorization": f"Bearer {token}"})
    assert account_check.status_code == 404


@pytest.mark.asyncio
async def test_embedded_signup_subscribe_failure_returns_400(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    fake = FakeMetaOAuthClient(raise_on_subscribe=True)
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        response = await client.post(
            "/api/v1/whatsapp/embedded-signup/callback",
            json={"code": "c", "waba_id": "w", "phone_number_id": "p"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
    finally:
        del app.dependency_overrides[get_meta_oauth_client]


@pytest.mark.asyncio
async def test_embedded_signup_requires_admin_role(client, db_session, unique_email):
    await _setup(db_session, unique_email, role=Role.AGENT)
    token = await _login(client, unique_email)

    response = await client.post(
        "/api/v1/whatsapp/embedded-signup/callback",
        json={"code": "c", "waba_id": "w", "phone_number_id": "p"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_embedded_signup_config_endpoint_is_public(client):
    """Pas d'authentification requise : ce sont des valeurs publiques (App ID, config_id)."""
    response = await client.get("/api/v1/whatsapp/embedded-signup/config")
    assert response.status_code == 200
    assert "app_id" in response.json()


@pytest.mark.asyncio
async def test_cannot_connect_whatsapp_twice_for_same_tenant(client, db_session, unique_email):
    await _setup(db_session, unique_email)
    token = await _login(client, unique_email)

    fake = FakeMetaOAuthClient()
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        first = await client.post(
            "/api/v1/whatsapp/embedded-signup/callback",
            json={"code": "c1", "waba_id": "w1", "phone_number_id": "p1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert first.status_code == 200

        second = await client.post(
            "/api/v1/whatsapp/embedded-signup/callback",
            json={"code": "c2", "waba_id": "w2", "phone_number_id": "p2"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert second.status_code == 409
    finally:
        del app.dependency_overrides[get_meta_oauth_client]
