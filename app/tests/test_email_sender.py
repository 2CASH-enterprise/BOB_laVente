from email import message_from_string
from email.header import decode_header, make_header
from email.utils import parseaddr

import pytest

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import email_service
from app.services.campaign_service import send_campaign
from app.services.email_service import build_message


@pytest.fixture
def smtp_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_from_email", "contact@agenc-ai.com")
    monkeypatch.setattr(settings, "smtp_from_name", "Bob AI")
    return settings


def _decoded(header_value: str) -> str:
    return str(make_header(decode_header(header_value)))


def _parsed(msg) -> "email.message.Message":  # noqa: F821
    """Relit le message tel qu'il part réellement sur le fil."""
    return message_from_string(msg.as_string())


# --- Nom d'expéditeur ------------------------------------------------------------------

def test_default_sender_is_bob_ai(smtp_settings):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps"))
    assert _decoded(msg["From"]) == "Bob AI <contact@agenc-ai.com>"


def test_custom_sender_name_keeps_authenticated_address(smtp_settings):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps", from_name="Boutique Awa"))
    name, address = parseaddr(_decoded(msg["From"]))
    assert name == "Boutique Awa"
    assert address == "contact@agenc-ai.com"  # jamais une autre adresse que le domaine authentifié


def test_accented_sender_name_is_properly_encoded(smtp_settings):
    msg = build_message("client@example.com", "Sujet", "Corps", from_name="Épicerie Aïda & Fils")
    raw = msg.as_string()
    raw.encode("ascii")  # l'en-tête doit être encodé (RFC 2047), sinon l'envoi SMTP échoue
    assert parseaddr(_decoded(_parsed(msg)["From"]))[0] == "Épicerie Aïda & Fils"


def test_empty_sender_name_falls_back_to_bare_address(smtp_settings):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps", from_name=""))
    assert msg["From"] == "contact@agenc-ai.com"


def test_no_configured_name_gives_bare_address(smtp_settings, monkeypatch):
    monkeypatch.setattr(smtp_settings, "smtp_from_name", "")
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps"))
    assert msg["From"] == "contact@agenc-ai.com"


def test_header_injection_in_sender_name_is_neutralized(smtp_settings):
    """Le nom du commerce est une saisie utilisateur : il ne doit jamais pouvoir ajouter d'en-tête."""
    msg = _parsed(build_message(
        "client@example.com", "Sujet", "Corps",
        from_name="Boutique\r\nBcc: victime@example.com\nX-Evil: 1",
    ))
    assert msg["Bcc"] is None
    assert msg["X-Evil"] is None
    assert len(msg.get_all("From")) == 1
    name, address = parseaddr(_decoded(msg["From"]))
    assert "\n" not in name and "\r" not in name
    assert address == "contact@agenc-ai.com"


def test_sender_name_with_quotes_and_comma_stays_single_address(smtp_settings):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps", from_name='Chez "Moussa", Dakar <pirate@evil.com>'))
    name, address = parseaddr(_decoded(msg["From"]))
    assert address == "contact@agenc-ai.com"
    assert "<" not in name and ">" not in name  # pas de fausse adresse affichée dans le nom


def test_very_long_sender_name_is_truncated(smtp_settings):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps", from_name="A" * 500))
    name, _ = parseaddr(_decoded(msg["From"]))
    assert len(name) == 100


# --- Reply-To --------------------------------------------------------------------------

def test_reply_to_is_set_when_valid(smtp_settings):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps", reply_to="awa@boutique.sn"))
    assert msg["Reply-To"] == "awa@boutique.sn"


def test_no_reply_to_by_default(smtp_settings):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps"))
    assert msg["Reply-To"] is None


@pytest.mark.parametrize("bad", [
    "pas-une-adresse",
    "awa@boutique.sn\r\nBcc: victime@example.com",
    "awa@boutique.sn, autre@example.com",
    "",
])
def test_invalid_reply_to_is_omitted(smtp_settings, bad):
    msg = _parsed(build_message("client@example.com", "Sujet", "Corps", reply_to=bad))
    assert msg["Reply-To"] is None
    assert msg["Bcc"] is None


# --- Envoi réel (serveur SMTP simulé) --------------------------------------------------

class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, envelope_from, recipients, raw):
        _FakeSMTP.sent.append((envelope_from, recipients, raw))


def test_send_email_uses_bare_address_as_envelope_sender(smtp_settings, monkeypatch):
    monkeypatch.setattr(smtp_settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(email_service.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = []

    assert email_service.send_email("client@example.com", "Sujet", "Corps", from_name="Boutique Awa", reply_to="awa@boutique.sn")

    envelope_from, recipients, raw = _FakeSMTP.sent[0]
    assert envelope_from == "contact@agenc-ai.com"  # l'enveloppe ne contient jamais le nom affiché
    assert recipients == ["client@example.com"]
    msg = message_from_string(raw)
    assert parseaddr(_decoded(msg["From"])) == ("Boutique Awa", "contact@agenc-ai.com")
    assert msg["Reply-To"] == "awa@boutique.sn"


def test_send_email_without_smtp_never_raises(smtp_settings, monkeypatch):
    monkeypatch.setattr(smtp_settings, "smtp_host", "")
    assert email_service.send_email("client@example.com", "Sujet", "Corps", from_name="X", reply_to="bad") is False


# --- Campagnes : au nom du commerce, réponses au commerçant ---------------------------

async def _tenant_with_customer(db_session, name: str, email: str, customer_email: str) -> tuple[Tenant, User]:
    tenant = Tenant(name=name, country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    user = User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    db_session.add(user)
    db_session.add(Customer(tenant_id=tenant.id, whatsapp_number="221700000001", email=customer_email, marketing_consent=True))
    await db_session.commit()
    return tenant, user


@pytest.mark.asyncio
async def test_campaign_is_sent_in_the_merchant_name(db_session, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("app.services.campaign_service.send_email", lambda **kw: calls.append(kw) or True)

    tenant, user = await _tenant_with_customer(db_session, "Boutique Awa", "awa@boutique.sn", "client@example.com")
    await send_campaign(db_session, tenant.id, user.id, "Promo", "Texte", "CTA")

    assert len(calls) == 1
    assert calls[0]["from_name"] == "Boutique Awa"
    assert calls[0]["reply_to"] == "awa@boutique.sn"


@pytest.mark.asyncio
async def test_campaign_sender_is_isolated_per_tenant(db_session, monkeypatch):
    """Une campagne du tenant B ne doit jamais partir au nom (ni avec l'email de réponse) du tenant A."""
    calls: list[dict] = []
    monkeypatch.setattr("app.services.campaign_service.send_email", lambda **kw: calls.append(kw) or True)

    await _tenant_with_customer(db_session, "Boutique A", "a@boutique-a.sn", "client-a@example.com")
    tenant_b, user_b = await _tenant_with_customer(db_session, "Boutique B", "b@boutique-b.sn", "client-b@example.com")

    await send_campaign(db_session, tenant_b.id, user_b.id, "Promo", "Texte", "CTA")

    assert [(c["to"], c["from_name"], c["reply_to"]) for c in calls] == [
        ("client-b@example.com", "Boutique B", "b@boutique-b.sn")
    ]


@pytest.mark.asyncio
async def test_transactional_emails_keep_default_bob_sender(client, db_session, unique_email, monkeypatch):
    """Mot de passe oublié : aucun nom de commerce, c'est Bob qui écrit (nom par défaut)."""
    calls: list[dict] = []

    def fake_send(to, subject, body, **kw):
        calls.append({"to": to, **kw})
        return True

    monkeypatch.setattr("app.api.auth.routes.send_email", fake_send)
    tenant = Tenant(name="Boutique Awa", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()

    await client.post("/api/v1/auth/forgot-password", json={"email": unique_email})

    assert len(calls) == 1
    assert calls[0].get("from_name") is None  # → smtp_from_name (« Bob AI ») appliqué par build_message
