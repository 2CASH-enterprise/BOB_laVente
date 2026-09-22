import pytest

from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.product import Product
from app.models.product_qr_code import ProductQrCode
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount


async def _setup_with_qr(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="phone_crm_test", system_user_token="t"))
    product = Product(tenant_id=tenant.id, sku="X", name="Samsung A56", price=280000, currency="XOF", stock_quantity=5)
    db_session.add(product)
    await db_session.flush()
    qr = ProductQrCode(tenant_id=tenant.id, product_id=product.id, code="testcode1")
    db_session.add(qr)
    await db_session.commit()
    return tenant, product


def _payload(phone_number_id, from_number, text):
    return {
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [{"from": from_number, "id": f"wamid.{from_number}", "type": "text", "text": {"body": text}, "timestamp": "1"}],
        }}]}]
    }


@pytest.mark.asyncio
async def test_qr_reference_attributes_acquisition_source_on_first_contact(client, db_session, unique_email):
    tenant, product = await _setup_with_qr(db_session, unique_email)

    response = await client.post(
        "/webhooks/whatsapp",
        json=_payload("phone_crm_test", "221700000001", "Bonjour, je suis intéressé(e) par : Samsung A56 [QR:testcode1]"),
    )
    assert response.status_code == 200

    from sqlalchemy import select

    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == "221700000001"))).scalar_one()
    assert customer.acquisition_source == "QR"
    assert "Samsung A56" in customer.acquisition_detail


@pytest.mark.asyncio
async def test_qr_marker_stripped_from_stored_message(client, db_session, unique_email):
    tenant, product = await _setup_with_qr(db_session, unique_email)

    await client.post(
        "/webhooks/whatsapp",
        json=_payload("phone_crm_test", "221700000002", "Bonjour, je suis intéressé(e) par : Samsung A56 [QR:testcode1]"),
    )

    from sqlalchemy import select

    from app.models.conversation import Message

    msg = (await db_session.execute(select(Message).where(Message.sender == "CUSTOMER"))).scalars().first()
    assert "[QR:" not in msg.content


@pytest.mark.asyncio
async def test_qr_attribution_never_overwritten_on_second_message(client, db_session, unique_email):
    """Un client déjà connu ne doit jamais voir sa source d'acquisition réécrite."""
    tenant, product = await _setup_with_qr(db_session, unique_email)

    await client.post("/webhooks/whatsapp", json=_payload("phone_crm_test", "221700000003", "Bonjour, sans QR"))

    from sqlalchemy import select

    customer = (await db_session.execute(select(Customer).where(Customer.whatsapp_number == "221700000003"))).scalar_one()
    assert customer.acquisition_source is None

    await client.post(
        "/webhooks/whatsapp",
        json=_payload("phone_crm_test", "221700000003", "Un second message [QR:testcode1]"),
    )
    await db_session.refresh(customer)
    assert customer.acquisition_source is None  # toujours pas attribué, second message ignoré pour l'attribution


@pytest.mark.asyncio
async def test_unknown_qr_code_ignored_gracefully(client, db_session, unique_email):
    tenant, product = await _setup_with_qr(db_session, unique_email)

    response = await client.post(
        "/webhooks/whatsapp",
        json=_payload("phone_crm_test", "221700000004", "Bonjour [QR:doesnotexist]"),
    )
    assert response.status_code == 200  # jamais une erreur, juste pas d'attribution
