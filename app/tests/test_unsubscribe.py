import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_string

import pytest
from sqlalchemy import select

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.campaign_service import get_eligible_customers, send_campaign
from app.services.consent_service import grant_marketing_consent, withdraw_marketing_consent
from app.services.unsubscribe_service import build_unsubscribe_url, make_unsubscribe_token, read_unsubscribe_token


async def _tenant(db_session, name: str, email: str) -> tuple[Tenant, User]:
    tenant = Tenant(name=name, country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    user = User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    db_session.add(user)
    await db_session.commit()
    return tenant, user


async def _consenting_customer(db_session, tenant: Tenant, number: str, email: str) -> Customer:
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number, email=email)
    grant_marketing_consent(customer, source="AI_ASKED")
    db_session.add(customer)
    await db_session.commit()
    return customer


async def _reload(db_session, customer_id) -> Customer:
    return (await db_session.execute(
        select(Customer).where(Customer.id == customer_id).execution_options(populate_existing=True)
    )).scalar_one()


def _path(customer_id) -> str:
    return f"/unsubscribe/{make_unsubscribe_token(customer_id)}"


# --- Lien signé ------------------------------------------------------------------------

def test_token_roundtrip():
    customer_id = uuid.uuid4()
    assert read_unsubscribe_token(make_unsubscribe_token(customer_id)) == customer_id


@pytest.mark.parametrize("tamper", [
    lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),       # signature modifiée
    lambda t: uuid.uuid4().hex + "." + t.split(".")[1],        # signature d'un autre client
    lambda t: t.split(".")[0],                                 # pas de signature
    lambda t: t.split(".")[0] + ".",                           # signature vide
    lambda t: t + ".x",                                        # segments en trop
    lambda t: t.upper(),                                       # forme non canonique
    lambda t: "pas-un-uuid." + t.split(".")[1],
    lambda t: "",
])
def test_tampered_tokens_are_rejected(tamper):
    assert read_unsubscribe_token(tamper(make_unsubscribe_token(uuid.uuid4()))) is None


def test_unsubscribe_url_uses_public_base_url():
    url = build_unsubscribe_url(uuid.uuid4())
    assert url.startswith("https://agenc-ai.com/bob/unsubscribe/")


# --- Page publique ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_shows_confirmation_and_changes_nothing(client, db_session, unique_email):
    """Les antivirus de messagerie ouvrent les liens : un simple GET ne doit jamais désinscrire."""
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000101", "client@example.com")

    r = await client.get(_path(customer.id))

    assert r.status_code == 200
    assert "Boutique Awa" in r.text
    assert "<form method=\"post\">" in r.text
    customer = await _reload(db_session, customer.id)
    assert customer.marketing_consent is True
    assert customer.marketing_consent_withdrawn_at is None


@pytest.mark.asyncio
async def test_page_shows_no_personal_data(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000102", "secret.client@example.com")

    r = await client.get(_path(customer.id))

    assert "secret.client@example.com" not in r.text
    assert "221700000102" not in r.text


@pytest.mark.asyncio
async def test_post_withdraws_consent_with_email_source(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000103", "client@example.com")
    given_at = customer.marketing_consent_given_at

    r = await client.post(_path(customer.id))

    assert r.status_code == 200
    assert "Boutique Awa" in r.text
    customer = await _reload(db_session, customer.id)
    assert customer.marketing_consent is False
    assert customer.marketing_consent_withdrawn_at is not None
    assert customer.marketing_consent_withdrawn_source == "EMAIL_LINK"
    # La trace du consentement d'origine est conservée.
    assert customer.marketing_consent_source == "AI_ASKED"
    assert customer.marketing_consent_given_at is not None


@pytest.mark.asyncio
async def test_one_click_post_from_mail_provider(client, db_session, unique_email):
    """RFC 8058 : Gmail/Outlook envoient ce corps précis en POST sur l'URL de List-Unsubscribe."""
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000104", "client@example.com")

    r = await client.post(
        _path(customer.id), content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert r.status_code == 200
    assert (await _reload(db_session, customer.id)).marketing_consent is False


@pytest.mark.asyncio
async def test_second_unsubscribe_keeps_first_date(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000105", "client@example.com")

    await client.post(_path(customer.id))
    first = (await _reload(db_session, customer.id)).marketing_consent_withdrawn_at
    r = await client.post(_path(customer.id))

    assert r.status_code == 200
    assert (await _reload(db_session, customer.id)).marketing_consent_withdrawn_at == first


@pytest.mark.asyncio
async def test_get_after_unsubscribe_shows_done_page(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000106", "client@example.com")
    await client.post(_path(customer.id))

    r = await client.get(_path(customer.id))

    assert r.status_code == 200
    assert "<form" not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post"])
async def test_invalid_link_gives_neutral_404(client, method):
    r = await getattr(client, method)("/unsubscribe/nimporte.quoi")
    assert r.status_code == 404
    assert "Lien invalide" in r.text


@pytest.mark.asyncio
async def test_valid_signature_for_deleted_customer_gives_neutral_404(client):
    r = await client.post(_path(uuid.uuid4()))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_shop_name_is_html_escaped(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "<script>alert(1)</script>", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000107", "client@example.com")

    r = await client.get(_path(customer.id))

    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


# --- Isolation -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_only_affects_its_own_customer(client, db_session):
    """Le lien d'un client ne touche jamais un autre client, ni du même commerce ni d'un autre."""
    tenant_a, _ = await _tenant(db_session, "Boutique A", "a@boutique-a.sn")
    tenant_b, _ = await _tenant(db_session, "Boutique B", "b@boutique-b.sn")
    target = await _consenting_customer(db_session, tenant_a, "221700000110", "cible@example.com")
    same_shop = await _consenting_customer(db_session, tenant_a, "221700000111", "voisin@example.com")
    # Même personne (même numéro) chez un autre commerce : consentement indépendant.
    other_shop = await _consenting_customer(db_session, tenant_b, "221700000110", "cible@example.com")

    await client.post(_path(target.id))

    assert (await _reload(db_session, target.id)).marketing_consent is False
    assert (await _reload(db_session, same_shop.id)).marketing_consent is True
    assert (await _reload(db_session, other_shop.id)).marketing_consent is True


# --- Campagnes : pied de page, en-têtes, boucle complète -----------------------------

@pytest.mark.asyncio
async def test_campaign_email_contains_personal_link_footer_and_headers(db_session, unique_email, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("app.services.campaign_service.send_email", lambda **kw: calls.append(kw) or True)
    tenant, user = await _tenant(db_session, "Boutique Awa", unique_email)
    c1 = await _consenting_customer(db_session, tenant, "221700000120", "un@example.com")
    c2 = await _consenting_customer(db_session, tenant, "221700000121", "deux@example.com")

    await send_campaign(db_session, tenant.id, user.id, "Promo", "Texte", "CTA")

    by_recipient = {c["to"]: c for c in calls}
    for customer in (c1, c2):
        call = by_recipient[customer.email]
        url = build_unsubscribe_url(customer.id)
        assert f"Se désinscrire : {url}" in call["body"]
        assert "accepté de recevoir les offres de Boutique Awa" in call["body"]
        assert call["extra_headers"] == {
            "List-Unsubscribe": f"<{url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    # Liens personnels : jamais le même lien pour deux destinataires.
    assert by_recipient["un@example.com"]["body"] != by_recipient["deux@example.com"]["body"]


@pytest.mark.asyncio
async def test_footer_stays_after_whatsapp_link(db_session, unique_email, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("app.services.campaign_service.send_email", lambda **kw: calls.append(kw) or True)
    tenant, user = await _tenant(db_session, "Boutique Awa", unique_email)
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="p_unsub", system_user_token="t", display_phone_number="+221 70 000 00 00"))
    await _consenting_customer(db_session, tenant, "221700000122", "un@example.com")

    await send_campaign(db_session, tenant.id, user.id, "Promo", "Texte", "Bonjour")

    body = calls[0]["body"]
    assert body.index("wa.me/") < body.index("Se désinscrire")


@pytest.mark.asyncio
async def test_unsubscribed_customer_excluded_from_next_campaign(client, db_session, unique_email, monkeypatch):
    """Boucle complète : campagne → clic de désinscription → la campagne suivante l'exclut."""
    calls: list[dict] = []
    monkeypatch.setattr("app.services.campaign_service.send_email", lambda **kw: calls.append(kw) or True)
    tenant, user = await _tenant(db_session, "Boutique Awa", unique_email)
    leaving = await _consenting_customer(db_session, tenant, "221700000123", "part@example.com")
    staying = await _consenting_customer(db_session, tenant, "221700000124", "reste@example.com")

    tenant_id = tenant.id
    await send_campaign(db_session, tenant_id, user.id, "Promo 1", "Texte", "CTA")
    await db_session.commit()
    # Le client clique sur le lien reçu dans SON email (celui des en-têtes, pas un lien recalculé).
    received = next(c for c in calls if c["to"] == "part@example.com")
    link = received["extra_headers"]["List-Unsubscribe"].strip("<>")
    await client.post(link.replace("https://agenc-ai.com/bob", ""))

    db_session.expire_all()
    eligible = await get_eligible_customers(db_session, tenant_id)
    assert [c.email for c in eligible] == ["reste@example.com"]


def test_extra_headers_are_whitelisted():
    from app.services.email_service import build_message

    msg = message_from_string(build_message(
        "c@example.com", "S", "B",
        extra_headers={
            "List-Unsubscribe": "<https://agenc-ai.com/bob/unsubscribe/x.y>",
            "Bcc": "victime@example.com",
            "From": "pirate@evil.com",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click\r\nBcc: victime@example.com",
        },
    ).as_string())

    assert msg["List-Unsubscribe"] == "<https://agenc-ai.com/bob/unsubscribe/x.y>"
    assert msg["Bcc"] is None
    assert msg["List-Unsubscribe-Post"] is None  # refusé (retour à la ligne)
    assert "pirate@evil.com" not in msg["From"]


# --- Traçabilité : idempotence et sources ---------------------------------------------

def test_regranting_keeps_original_proof():
    customer = Customer(whatsapp_number="1", marketing_consent=False)
    first = datetime(2026, 3, 3, tzinfo=timezone.utc)
    assert grant_marketing_consent(customer, source="AI_ASKED", now=first) is True
    assert grant_marketing_consent(customer, source="MANUAL", now=first + timedelta(days=90)) is False
    assert customer.marketing_consent_source == "AI_ASKED"
    assert customer.marketing_consent_given_at == first


def test_rewithdrawing_keeps_first_withdrawal():
    customer = Customer(whatsapp_number="1", marketing_consent=True)
    first = datetime(2026, 3, 3, tzinfo=timezone.utc)
    assert withdraw_marketing_consent(customer, source="EMAIL_LINK", now=first) is True
    assert withdraw_marketing_consent(customer, source="MANUAL", now=first + timedelta(days=1)) is False
    assert customer.marketing_consent_withdrawn_source == "EMAIL_LINK"
    assert customer.marketing_consent_withdrawn_at == first


def test_refusal_without_prior_consent_is_recorded():
    customer = Customer(whatsapp_number="1", marketing_consent=False)
    assert withdraw_marketing_consent(customer, source="WHATSAPP_KEYWORD") is True
    assert customer.marketing_consent_withdrawn_at is not None


def test_regranting_after_withdrawal_clears_withdrawal_source():
    customer = Customer(whatsapp_number="1", marketing_consent=True)
    withdraw_marketing_consent(customer, source="EMAIL_LINK")
    grant_marketing_consent(customer, source="MANUAL")
    assert customer.marketing_consent_withdrawn_at is None
    assert customer.marketing_consent_withdrawn_source is None


@pytest.mark.asyncio
async def test_whatsapp_stop_records_keyword_source(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="p_stop_src", system_user_token="t"))
    await db_session.commit()
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "p_stop_src"},
        "messages": [{"from": "221700000130", "id": "wamid.src", "type": "text", "text": {"body": "STOP"}, "timestamp": "1"}],
    }}]}]}

    await client.post("/webhooks/whatsapp", json=payload)

    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == "221700000130"))).scalar_one()
    assert customer.marketing_consent_withdrawn_source == "WHATSAPP_KEYWORD"


@pytest.mark.asyncio
async def test_ai_refusal_records_ai_source(db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000131", "c@example.com")
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()

    await ToolExecutor(db_session, tenant.id, conversation).execute("record_marketing_consent", {"accepted": False})

    assert (await _reload(db_session, customer.id)).marketing_consent_withdrawn_source == "AI_DETECTED"


async def _put_customer(client, email: str, customer_id, payload: dict):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return await client.put(f"/api/v1/customers/{customer_id}", json=payload, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_manual_uncheck_records_manual_source(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000132", "c@example.com")

    r = await _put_customer(client, unique_email, customer.id, {"marketing_consent": False})

    assert r.json()["marketing_consent_withdrawn_source"] == "MANUAL"


@pytest.mark.asyncio
async def test_saving_a_note_does_not_overwrite_consent_proof(client, db_session, unique_email):
    """Bug corrigé : le dashboard renvoie la case à chaque enregistrement, même pour une note."""
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000133", "c@example.com")
    original_given_at = customer.marketing_consent_given_at

    r = await _put_customer(client, unique_email, customer.id, {"marketing_consent": True, "notes": "Préfère le rouge"})

    assert r.status_code == 200
    customer = await _reload(db_session, customer.id)
    assert customer.marketing_consent_source == "AI_ASKED"  # pas écrasé par MANUAL
    given_at = customer.marketing_consent_given_at
    if given_at.tzinfo is None:
        given_at = given_at.replace(tzinfo=timezone.utc)
    assert given_at == original_given_at


@pytest.mark.asyncio
async def test_saving_a_note_does_not_invent_a_withdrawal(client, db_session, unique_email):
    """Un client jamais sollicité ne doit pas se retrouver avec un faux « retrait »."""
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000134")
    db_session.add(customer)
    await db_session.commit()

    await _put_customer(client, unique_email, customer.id, {"marketing_consent": False, "notes": "Note"})

    assert (await _reload(db_session, customer.id)).marketing_consent_withdrawn_at is None


@pytest.mark.asyncio
async def test_saving_a_note_keeps_email_withdrawal_source(client, db_session, unique_email):
    tenant, _ = await _tenant(db_session, "Boutique Awa", unique_email)
    customer = await _consenting_customer(db_session, tenant, "221700000135", "c@example.com")
    await client.post(_path(customer.id))

    await _put_customer(client, unique_email, customer.id, {"marketing_consent": False, "notes": "Note"})

    assert (await _reload(db_session, customer.id)).marketing_consent_withdrawn_source == "EMAIL_LINK"
