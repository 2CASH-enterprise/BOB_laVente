import uuid
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.contact_point import ContactPoint
from app.models.conversation import Message
from app.models.customer import Customer
from app.models.product import Product
from app.models.product_qr_code import ProductQrCode
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount

API = "/api/v1/contact-points"


async def _tenant(db_session, email: str, plan=TenantPlan.FREE, is_demo=False, phone="+221 70 111 22 33",
                  phone_number_id=None, role=Role.OWNER):
    tenant = Tenant(name=f"Boutique {email}", country="SN", currency="XOF", email=email, plan=plan, is_demo=is_demo)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    db_session.add(WhatsAppAccount(
        tenant_id=tenant.id, waba_id="w", phone_number_id=phone_number_id or f"pn-{uuid.uuid4().hex[:10]}",
        system_user_token="t", display_phone_number=phone,
    ))
    await db_session.commit()
    return tenant


async def _auth(client, email: str) -> dict:
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _create(client, headers, name="Page Facebook", **extra):
    return await client.post(API, json={"name": name, **extra}, headers=headers)


async def _product(db_session, tenant, sku="ROBE-1", name="Robe Wax Bleue", active=True):
    product = Product(tenant_id=tenant.id, sku=sku, name=name, price=15000, currency="XOF", stock_quantity=3, active=active)
    db_session.add(product)
    await db_session.commit()
    return product


def _wa_text(response) -> str:
    return unquote(parse_qs(urlparse(response.headers["location"]).query)["text"][0])


# --- Gestion depuis le dashboard ------------------------------------------------------

@pytest.mark.asyncio
async def test_create_contact_point(client, db_session, unique_email):
    await _tenant(db_session, unique_email)
    r = await _create(client, await _auth(client, unique_email), greeting="Bonjour, je viens de votre page", position="LEFT")

    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Page Facebook"
    assert body["greeting"] == "Bonjour, je viens de votre page"
    assert body["position"] == "LEFT"
    assert body["active"] is True
    assert body["click_count"] == 0 and body["customer_count"] == 0
    assert body["short_path"] == f"/w/{body['code']}"
    assert 1 <= len(body["code"]) <= 16


@pytest.mark.asyncio
async def test_default_greeting(client, db_session, unique_email):
    await _tenant(db_session, unique_email)
    r = await _create(client, await _auth(client, unique_email))
    assert r.json()["greeting"] == "Bonjour, je souhaite avoir des informations."


@pytest.mark.asyncio
async def test_free_plan_limited_to_one(client, db_session, unique_email):
    await _tenant(db_session, unique_email, plan=TenantPlan.FREE)
    headers = await _auth(client, unique_email)
    assert (await _create(client, headers, "Un")).status_code == 201

    r = await _create(client, headers, "Deux")
    assert r.status_code == 403
    assert "plan payant" in r.json()["detail"]

    limits = (await client.get(f"{API}/limits", headers=headers)).json()
    assert limits == {"used": 1, "max": 1}


@pytest.mark.asyncio
async def test_paid_plan_limited_to_ten(client, db_session, unique_email):
    await _tenant(db_session, unique_email, plan=TenantPlan.INDEPENDANT)
    headers = await _auth(client, unique_email)
    for i in range(10):
        assert (await _create(client, headers, f"Lien {i}")).status_code == 201
    assert (await _create(client, headers, "Onzième")).status_code == 403


@pytest.mark.asyncio
async def test_demo_tenant_unlimited(client, db_session, unique_email):
    await _tenant(db_session, unique_email, is_demo=True)
    headers = await _auth(client, unique_email)
    for i in range(12):
        assert (await _create(client, headers, f"Lien {i}")).status_code == 201
    assert (await client.get(f"{API}/limits", headers=headers)).json()["max"] is None


@pytest.mark.asyncio
async def test_archived_contact_point_frees_a_slot(client, db_session, unique_email):
    await _tenant(db_session, unique_email, plan=TenantPlan.FREE)
    headers = await _auth(client, unique_email)
    first = (await _create(client, headers, "Un")).json()

    assert (await client.delete(f"{API}/{first['id']}", headers=headers)).status_code == 204
    assert (await _create(client, headers, "Deux")).status_code == 201
    assert [c["name"] for c in (await client.get(API, headers=headers)).json()] == ["Deux"]


@pytest.mark.asyncio
async def test_update_and_deactivate(client, db_session, unique_email):
    await _tenant(db_session, unique_email)
    headers = await _auth(client, unique_email)
    cp = (await _create(client, headers)).json()

    r = await client.patch(f"{API}/{cp['id']}", json={"greeting": "Nouveau message", "active": False}, headers=headers)

    assert r.status_code == 200
    assert r.json()["greeting"] == "Nouveau message"
    assert r.json()["active"] is False
    assert r.json()["code"] == cp["code"]  # le lien partagé ne change jamais


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"name": ""}, {"name": "x" * 81}, {"name": "ok", "position": "TOP"}, {"name": "ok", "greeting": ""}])
async def test_invalid_payloads_rejected(client, db_session, unique_email, payload):
    await _tenant(db_session, unique_email)
    r = await client.post(API, json=payload, headers=await _auth(client, unique_email))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_agent_role_cannot_create(client, db_session, unique_email):
    await _tenant(db_session, unique_email, role=Role.AGENT)
    r = await _create(client, await _auth(client, unique_email))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_tenant_isolation_on_management(client, db_session):
    await _tenant(db_session, "a@cp-test.sn")
    await _tenant(db_session, "b@cp-test.sn")
    headers_a = await _auth(client, "a@cp-test.sn")
    headers_b = await _auth(client, "b@cp-test.sn")
    cp_a = (await _create(client, headers_a, "Lien de A")).json()

    assert (await client.get(API, headers=headers_b)).json() == []
    assert (await client.patch(f"{API}/{cp_a['id']}", json={"active": False}, headers=headers_b)).status_code == 404
    assert (await client.delete(f"{API}/{cp_a['id']}", headers=headers_b)).status_code == 404
    assert (await client.get(API, headers=headers_a)).json()[0]["active"] is True


# --- Redirection publique /w/{code} ---------------------------------------------------

@pytest.mark.asyncio
async def test_redirect_to_whatsapp_with_greeting_and_counts_click(client, db_session, unique_email):
    await _tenant(db_session, unique_email, phone="+221 70 111 22 33")
    headers = await _auth(client, unique_email)
    cp = (await _create(client, headers, greeting="Bonjour, je viens de Facebook")).json()

    r = await client.get(cp["short_path"])

    assert r.status_code == 302
    assert r.headers["location"].startswith("https://wa.me/221701112233?text=")
    assert _wa_text(r) == f"Bonjour, je viens de Facebook [W:{cp['code']}]"
    assert (await client.get(API, headers=headers)).json()[0]["click_count"] == 1


@pytest.mark.asyncio
async def test_product_link_uses_real_catalog_name(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    await _product(db_session, tenant, sku="ROBE-1", name="Robe Wax Bleue")
    cp = (await _create(client, await _auth(client, unique_email))).json()

    r = await client.get(f"{cp['short_path']}?p=ROBE-1")

    assert _wa_text(r) == f"Bonjour, je suis intéressé(e) par : Robe Wax Bleue [W:{cp['code']}]"


@pytest.mark.asyncio
async def test_product_of_another_tenant_is_never_used(client, db_session):
    tenant_a = await _tenant(db_session, "a@cp-prod.sn")
    tenant_b = await _tenant(db_session, "b@cp-prod.sn")
    await _product(db_session, tenant_b, sku="SECRET-B", name="Produit de B")
    cp_a = (await _create(client, await _auth(client, "a@cp-prod.sn"), greeting="Bonjour A")).json()

    r = await client.get(f"{cp_a['short_path']}?p=SECRET-B")

    assert "Produit de B" not in _wa_text(r)
    assert _wa_text(r).startswith("Bonjour A")


@pytest.mark.asyncio
@pytest.mark.parametrize("sku", ["INCONNU", "INACTIF"])
async def test_unknown_or_inactive_product_falls_back_to_greeting(client, db_session, unique_email, sku):
    tenant = await _tenant(db_session, unique_email)
    await _product(db_session, tenant, sku="INACTIF", name="Produit retiré", active=False)
    cp = (await _create(client, await _auth(client, unique_email), greeting="Bonjour")).json()

    r = await client.get(f"{cp['short_path']}?p={sku}")

    assert r.status_code == 302
    assert _wa_text(r) == f"Bonjour [W:{cp['code']}]"


@pytest.mark.asyncio
async def test_overlong_product_reference_rejected(client, db_session, unique_email):
    await _tenant(db_session, unique_email)
    cp = (await _create(client, await _auth(client, unique_email))).json()
    r = await client.get(f"{cp['short_path']}?p={'x' * 65}")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_deactivated_archived_and_unknown_links_show_inactive_page(client, db_session, unique_email):
    await _tenant(db_session, unique_email, plan=TenantPlan.INDEPENDANT)
    headers = await _auth(client, unique_email)
    off = (await _create(client, headers, "Off")).json()
    gone = (await _create(client, headers, "Gone")).json()
    await client.patch(f"{API}/{off['id']}", json={"active": False}, headers=headers)
    await client.delete(f"{API}/{gone['id']}", headers=headers)

    for path in (off["short_path"], gone["short_path"], "/w/inconnu"):
        r = await client.get(path)
        assert r.status_code == 404
        assert "plus actif" in r.text

    counts = {c["name"]: c["click_count"] for c in (await client.get(API, headers=headers)).json()}
    assert counts["Off"] == 0  # un clic refusé n'est pas compté


@pytest.mark.asyncio
async def test_no_whatsapp_number_shows_inactive_page(client, db_session, unique_email):
    await _tenant(db_session, unique_email, phone=None)
    cp = (await _create(client, await _auth(client, unique_email))).json()
    assert (await client.get(cp["short_path"])).status_code == 404


# --- Configuration du widget ----------------------------------------------------------

@pytest.mark.asyncio
async def test_widget_config_free_plan_shows_branding(client, db_session, unique_email):
    await _tenant(db_session, unique_email, plan=TenantPlan.FREE)
    cp = (await _create(client, await _auth(client, unique_email), position="LEFT")).json()

    r = await client.get(f"{cp['short_path']}/config")

    assert r.status_code == 200
    assert r.json() == {"active": True, "position": "LEFT", "branding": True}
    assert r.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_widget_config_paid_plan_without_branding(client, db_session, unique_email):
    await _tenant(db_session, unique_email, plan=TenantPlan.PRO)
    cp = (await _create(client, await _auth(client, unique_email))).json()
    assert (await client.get(f"{cp['short_path']}/config")).json()["branding"] is False


@pytest.mark.asyncio
async def test_widget_config_exposes_no_sensitive_data(client, db_session, unique_email):
    await _tenant(db_session, unique_email, phone="+221 70 999 88 77")
    cp = (await _create(client, await _auth(client, unique_email), greeting="Message privé")).json()

    text = (await client.get(f"{cp['short_path']}/config")).text

    for secret in ("70 999", "709998877", unique_email, "Message privé", "Boutique"):
        assert secret not in text


@pytest.mark.asyncio
async def test_widget_config_inactive(client, db_session, unique_email):
    await _tenant(db_session, unique_email)
    headers = await _auth(client, unique_email)
    cp = (await _create(client, headers)).json()
    await client.patch(f"{API}/{cp['id']}", json={"active": False}, headers=headers)

    r = await client.get(f"{cp['short_path']}/config")

    assert r.status_code == 404
    assert r.json() == {"active": False}
    assert r.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_widget_script_is_served_as_javascript(client):
    r = await client.get("/widget.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "data-bob-widget" in r.text
    assert "wa.me" not in r.text  # aucun numéro ni lien direct dans le script


# --- Attribution dans le webhook ------------------------------------------------------

def _payload(phone_number_id: str, from_number: str, text: str, msg_id: str | None = None) -> dict:
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"from": from_number, "id": msg_id or f"wamid.{uuid.uuid4().hex}", "type": "text",
                      "text": {"body": text}, "timestamp": "1"}],
    }}]}]}


async def _reload_customer(db_session, number: str, tenant_id) -> Customer:
    return (await db_session.execute(
        select(Customer).where(Customer.whatsapp_number == number, Customer.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


@pytest.mark.asyncio
async def test_new_customer_is_attributed_to_contact_point(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, phone_number_id="pn-attr-1")
    headers = await _auth(client, unique_email)
    cp = (await _create(client, headers, "Page Facebook")).json()

    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-1", "221700000201", f"Bonjour [W:{cp['code']}]"))

    customer = await _reload_customer(db_session, "221700000201", tenant.id)
    assert customer.acquisition_source == "LINK"
    assert customer.acquisition_detail == "Page Facebook"
    assert str(customer.acquisition_contact_point_id) == cp["id"]
    stored = (await db_session.execute(select(Message.content).where(Message.tenant_id == tenant.id))).scalars().all()
    assert stored and all("[W:" not in m for m in stored)
    assert (await client.get(API, headers=headers)).json()[0]["customer_count"] == 1


@pytest.mark.asyncio
async def test_existing_customer_tag_stripped_but_attribution_unchanged(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, phone_number_id="pn-attr-2")
    cp = (await _create(client, await _auth(client, unique_email), "Page Facebook")).json()
    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-2", "221700000202", "Bonjour"))

    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-2", "221700000202", f"Je reviens [W:{cp['code']}]"))

    customer = await _reload_customer(db_session, "221700000202", tenant.id)
    assert customer.acquisition_source != "LINK"
    assert customer.acquisition_contact_point_id is None
    stored = (await db_session.execute(select(Message.content).where(Message.tenant_id == tenant.id))).scalars().all()
    assert "Je reviens" in stored
    assert all("[W:" not in m for m in stored)


@pytest.mark.asyncio
async def test_existing_customer_qr_tag_is_now_stripped(client, db_session, unique_email):
    """Correctif : la balise [QR:…] n'était retirée que pour un nouveau client."""
    tenant = await _tenant(db_session, unique_email, phone_number_id="pn-attr-3")
    product = await _product(db_session, tenant)
    db_session.add(ProductQrCode(tenant_id=tenant.id, product_id=product.id, code="qr123"))
    await db_session.commit()
    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-3", "221700000203", "Bonjour"))

    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-3", "221700000203", "Intéressé [QR:qr123]"))

    stored = (await db_session.execute(select(Message.content).where(Message.tenant_id == tenant.id))).scalars().all()
    assert "Intéressé" in stored
    assert all("[QR:" not in m for m in stored)


@pytest.mark.asyncio
async def test_new_customer_qr_attribution_still_works(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, phone_number_id="pn-attr-4")
    product = await _product(db_session, tenant, name="Sac Cuir")
    db_session.add(ProductQrCode(tenant_id=tenant.id, product_id=product.id, code="qr456"))
    await db_session.commit()

    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-4", "221700000204", "Bonjour [QR:qr456]"))

    customer = await _reload_customer(db_session, "221700000204", tenant.id)
    assert customer.acquisition_source == "QR"
    assert customer.acquisition_detail == "Produit scanné : Sac Cuir"


@pytest.mark.asyncio
async def test_code_of_another_tenant_never_attributes(client, db_session):
    tenant_a = await _tenant(db_session, "a@cp-attr.sn", phone_number_id="pn-attr-a")
    await _tenant(db_session, "b@cp-attr.sn", phone_number_id="pn-attr-b")
    cp_b = (await _create(client, await _auth(client, "b@cp-attr.sn"), "Lien de B")).json()

    # Le code de B arrive sur le numéro de A.
    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-a", "221700000205", f"Bonjour [W:{cp_b['code']}]"))

    customer = await _reload_customer(db_session, "221700000205", tenant_a.id)
    assert customer.acquisition_contact_point_id is None
    assert customer.acquisition_source != "LINK"
    stored = (await db_session.execute(select(Message.content).where(Message.tenant_id == tenant_a.id))).scalars().all()
    assert all("[W:" not in m for m in stored)


@pytest.mark.asyncio
async def test_tag_in_the_middle_of_a_message_is_left_alone(client, db_session, unique_email):
    """Seule une référence en FIN de message (celle que génère le lien) est interprétée."""
    tenant = await _tenant(db_session, unique_email, phone_number_id="pn-attr-6")
    cp = (await _create(client, await _auth(client, unique_email))).json()

    await client.post("/webhooks/whatsapp", json=_payload("pn-attr-6", "221700000206", f"[W:{cp['code']}] au début"))

    customer = await _reload_customer(db_session, "221700000206", tenant.id)
    assert customer.acquisition_contact_point_id is None
