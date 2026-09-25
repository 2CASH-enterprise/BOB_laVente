import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.security import hash_password, verify_password
from app.models.audit_log import AuditLog
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.services.password_reset_service import _hash_token, build_reset_link

FORGOT = "/api/v1/auth/forgot-password"
RESET = "/api/v1/auth/reset-password"
LOGIN = "/api/v1/auth/login"


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture les emails au lieu de les envoyer (les routes appellent send_email en tâche de fond)."""
    outbox: list[dict] = []

    def fake_send_email(to: str, subject: str, body: str) -> bool:
        outbox.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr("app.api.auth.routes.send_email", fake_send_email)
    return outbox


async def _create_user(db_session, email: str, password: str = "ancien-mdp", mfa_enabled: bool = False, active: bool = True) -> User:
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id, email=email, hashed_password=hash_password(password), full_name="Awa",
        role=Role.OWNER, mfa_enabled=mfa_enabled, active=active,
    )
    db_session.add(user)
    await db_session.commit()
    return user


def _token_from(email_body: str) -> str:
    match = re.search(r"reset_token=([A-Za-z0-9_\-]+)", email_body)
    assert match, "le lien de réinitialisation doit contenir le token"
    return match.group(1)


async def _reload(db_session, user_id) -> User:
    return (await db_session.execute(
        select(User).where(User.id == user_id).execution_options(populate_existing=True)
    )).scalar_one()


async def _request_token(client, sent_emails, email: str) -> str:
    r = await client.post(FORGOT, json={"email": email})
    assert r.status_code == 202
    return _token_from(sent_emails[-1]["body"])


# --- Demande de réinitialisation -------------------------------------------------------

@pytest.mark.asyncio
async def test_forgot_password_sends_link_and_stores_only_hash(client, db_session, unique_email, sent_emails):
    user = await _create_user(db_session, unique_email)

    r = await client.post(FORGOT, json={"email": unique_email})

    assert r.status_code == 202
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == unique_email
    token = _token_from(sent_emails[0]["body"])

    user = await _reload(db_session, user.id)
    assert user.password_reset_token_hash == _hash_token(token)
    assert user.password_reset_token_hash != token  # jamais en clair
    assert user.password_reset_expires_at is not None


@pytest.mark.asyncio
async def test_forgot_password_unknown_email_gives_identical_response(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)

    known = await client.post(FORGOT, json={"email": unique_email})
    unknown = await client.post(FORGOT, json={"email": "personne@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert [e["to"] for e in sent_emails] == [unique_email]  # aucun email pour l'inconnu


@pytest.mark.asyncio
async def test_forgot_password_inactive_user_gets_nothing(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email, active=False)

    r = await client.post(FORGOT, json={"email": unique_email})

    assert r.status_code == 202
    assert sent_emails == []


@pytest.mark.asyncio
async def test_forgot_password_rate_limited_per_email(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)
    for _ in range(3):
        assert (await client.post(FORGOT, json={"email": unique_email})).status_code == 202

    r = await client.post(FORGOT, json={"email": unique_email})
    assert r.status_code == 429
    assert len(sent_emails) == 3


@pytest.mark.asyncio
async def test_forgot_password_rate_limit_ignores_case(client, db_session, unique_email, sent_emails):
    """Varier la casse de l'email ne doit pas permettre de contourner la limite."""
    await _create_user(db_session, unique_email)
    for variant in (unique_email, unique_email.upper(), unique_email.capitalize()):
        await client.post(FORGOT, json={"email": variant})

    assert (await client.post(FORGOT, json={"email": unique_email.upper()})).status_code == 429


@pytest.mark.asyncio
async def test_reset_link_uses_public_base_url():
    link = build_reset_link("abc")
    assert link.startswith("https://agenc-ai.com/bob/dashboard/")
    assert link.endswith("?reset_token=abc")


# --- Réinitialisation effective --------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_password_changes_password(client, db_session, unique_email, sent_emails):
    user = await _create_user(db_session, unique_email)
    token = await _request_token(client, sent_emails, unique_email)

    r = await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})
    assert r.status_code == 204
    assert r.content == b""  # ne connecte pas : aucun token d'accès renvoyé

    old = await client.post(LOGIN, data={"username": unique_email, "password": "ancien-mdp"})
    new = await client.post(LOGIN, data={"username": unique_email, "password": "nouveau-mdp-123"})
    assert old.status_code == 401
    assert new.status_code == 200 and new.json()["access_token"]

    user = await _reload(db_session, user.id)
    assert user.password_reset_token_hash is None
    assert user.password_reset_expires_at is None


@pytest.mark.asyncio
async def test_reset_password_sends_confirmation_email(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)
    token = await _request_token(client, sent_emails, unique_email)

    await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})

    assert len(sent_emails) == 2
    assert sent_emails[1]["to"] == unique_email
    assert "modifié" in sent_emails[1]["subject"]
    assert "reset_token" not in sent_emails[1]["body"]


@pytest.mark.asyncio
async def test_reset_token_is_single_use(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)
    token = await _request_token(client, sent_emails, unique_email)

    assert (await client.post(RESET, json={"token": token, "new_password": "premier-123"})).status_code == 204
    r = await client.post(RESET, json={"token": token, "new_password": "second-12345"})

    assert r.status_code == 400
    ok = await client.post(LOGIN, data={"username": unique_email, "password": "premier-123"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_expired_token_is_rejected(client, db_session, unique_email, sent_emails):
    user = await _create_user(db_session, unique_email)
    token = await _request_token(client, sent_emails, unique_email)

    user = await _reload(db_session, user.id)
    user.password_reset_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    r = await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})
    assert r.status_code == 400
    assert (await client.post(LOGIN, data={"username": unique_email, "password": "ancien-mdp"})).status_code == 200


@pytest.mark.asyncio
async def test_new_request_invalidates_previous_token(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)
    first = await _request_token(client, sent_emails, unique_email)
    second = await _request_token(client, sent_emails, unique_email)
    assert first != second

    assert (await client.post(RESET, json={"token": first, "new_password": "nouveau-mdp-123"})).status_code == 400
    assert (await client.post(RESET, json={"token": second, "new_password": "nouveau-mdp-123"})).status_code == 204


@pytest.mark.asyncio
async def test_random_token_is_rejected(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)
    await _request_token(client, sent_emails, unique_email)

    r = await client.post(RESET, json={"token": "token-invente", "new_password": "nouveau-mdp-123"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_token_of_deactivated_user_is_rejected(client, db_session, unique_email, sent_emails):
    user = await _create_user(db_session, unique_email)
    token = await _request_token(client, sent_emails, unique_email)

    user = await _reload(db_session, user.id)
    user.active = False
    await db_session.commit()

    r = await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_too_short_password_rejected_and_token_not_consumed(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)
    token = await _request_token(client, sent_emails, unique_email)

    r = await client.post(RESET, json={"token": token, "new_password": "court"})
    assert r.status_code == 422

    # Le token reste utilisable : l'erreur de saisie ne doit pas obliger à refaire une demande.
    assert (await client.post(RESET, json={"token": token, "new_password": "assez-long-123"})).status_code == 204


@pytest.mark.asyncio
async def test_reset_endpoint_rate_limited_per_ip(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email)
    for _ in range(10):
        await client.post(RESET, json={"token": "x", "new_password": "nouveau-mdp-123"})

    r = await client.post(RESET, json={"token": "x", "new_password": "nouveau-mdp-123"})
    assert r.status_code == 429


# --- Sécurité : 2FA et isolation --------------------------------------------------------

@pytest.mark.asyncio
async def test_mfa_still_required_after_reset(client, db_session, unique_email, sent_emails):
    await _create_user(db_session, unique_email, mfa_enabled=True)
    token = await _request_token(client, sent_emails, unique_email)

    await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})
    r = await client.post(LOGIN, data={"username": unique_email, "password": "nouveau-mdp-123"})

    assert r.status_code == 200
    assert r.json()["mfa_required"] is True
    assert r.json()["access_token"] is None


@pytest.mark.asyncio
async def test_reset_clears_pending_mfa_code(client, db_session, unique_email, sent_emails):
    user = await _create_user(db_session, unique_email, mfa_enabled=True)
    user.mfa_otp_code_hash = "x" * 64
    user.mfa_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db_session.commit()
    token = await _request_token(client, sent_emails, unique_email)

    await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})

    user = await _reload(db_session, user.id)
    assert user.mfa_otp_code_hash is None


@pytest.mark.asyncio
async def test_reset_only_affects_token_owner(client, db_session, sent_emails):
    """Isolation : le token d'un utilisateur ne touche jamais le compte d'un autre tenant."""
    alice = await _create_user(db_session, "alice@example.com", password="mdp-alice")
    bob = await _create_user(db_session, "bob@example.com", password="mdp-bob")
    assert alice.tenant_id != bob.tenant_id

    token = await _request_token(client, sent_emails, "alice@example.com")
    await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})

    bob = await _reload(db_session, bob.id)
    assert verify_password("mdp-bob", bob.hashed_password)
    assert bob.password_reset_token_hash is None


@pytest.mark.asyncio
async def test_reset_flow_is_audited(client, db_session, unique_email, sent_emails):
    user = await _create_user(db_session, unique_email)
    token = await _request_token(client, sent_emails, unique_email)
    await client.post(RESET, json={"token": token, "new_password": "nouveau-mdp-123"})
    await client.post(FORGOT, json={"email": "inconnu@example.com"})

    actions = (await db_session.execute(select(AuditLog.action, AuditLog.tenant_id))).all()
    assert ("PASSWORD_RESET_REQUESTED", user.tenant_id) in actions
    assert ("PASSWORD_RESET_COMPLETED", user.tenant_id) in actions
    assert any(a == "PASSWORD_RESET_REQUESTED_UNKNOWN" for a, _ in actions)
