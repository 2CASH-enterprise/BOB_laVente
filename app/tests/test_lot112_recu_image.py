"""
Lot 112 — le reçu de commande en image (restaurants et boutiques) : « Commande confirmée » à la validation, « Reçu de
commande » (payé) au clic « Paiement reçu ». Image déposée chez WhatsApp (jamais d'adresse publique), en tête du message
à boutons ; texte court dessous (référence, total, puis lien de paiement et suite) ; si l'image ne part pas, le texte
habituel. Le bandeau du total prend la teinte choisie pour les affiches (gardée par le tableau de bord).
"""
import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import select

from app.models.conversation import Message, MessageSender
from app.models.customer import Customer
from app.models.order import Order
from app.services import guided_flow, receipt_image
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE, RESTAURANT
from app.tests.test_lot53_courtier import _cabinet, _conversation, _headers
from app.tests.test_lot106_restaurant import _resto
from app.tests.test_lot107_parcours_restaurant import HTML, _dish, _guided, _say, _tap  # noqa: F401 (fixture)


class Wa:
    """Faux WhatsApp : garde les dépôts d'images et les envois."""

    def __init__(self, refuse_upload=False):
        self.refuse_upload, self.uploads, self.sent = refuse_upload, [], []

    async def upload_media(self, content, mime_type="image/png", filename="recu.png"):
        if self.refuse_upload:
            raise RuntimeError("dépôt refusé")
        self.uploads.append(content)
        return f"media-{len(self.uploads)}"

    async def send_buttons(self, to, body, buttons, image_link=None, image_id=None):
        self.sent.append({"kind": "buttons", "body": body, "buttons": buttons, "image_id": image_id})
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}

    async def send_text_message(self, to, body):
        self.sent.append({"kind": "text", "body": body})
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}

    async def send_image_by_id(self, to, media_id, caption=None):
        self.sent.append({"kind": "image", "body": caption, "image_id": media_id})
        return {}


def _png(content: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(content))
    assert image.format == "PNG" and image.width == receipt_image.WIDTH and image.height > 800
    return image.convert("RGB")


async def _ordered(db, **fields):
    """Restaurant avec lien de paiement, une commande sur place validée par les boutons."""
    tenant = await _resto(db, pickup_enabled=False, delivery_enabled=False, payment_link="https://pay.example/awa", **fields)
    dish = await _dish(db, tenant)
    customer, conversation = await _conversation(db, tenant)
    customer.last_name = "Koné"
    await _tap(db, tenant, customer, conversation, f"fq:{dish.id}:-:2")
    await _tap(db, tenant, customer, conversation, "fo")
    await _say(db, tenant, customer, conversation, "Table 7")
    await _tap(db, tenant, customer, conversation, "fp:k")
    turn = await _tap(db, tenant, customer, conversation, "fw")
    return tenant, customer, conversation, turn


@pytest.mark.asyncio
async def test_the_confirmation_goes_out_as_a_receipt_image_with_its_buttons(db_session):
    tenant, customer, conversation, turn = await _ordered(db_session)
    action = turn.actions[0]
    assert action["receipt"] and action["message_type"] == "order_confirmation"
    wa = Wa()
    assert await guided_flow.deliver(db_session, tenant, conversation, wa, customer.whatsapp_number, turn) == 1
    sent = wa.sent[-1]
    assert sent["kind"] == "buttons" and sent["image_id"] == "media-1" and [t for _, t in sent["buttons"]] == ["Suivre ma commande", "Voir la carte"]
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    ref = str(order.id)[:8].upper()
    assert sent["body"].startswith(f"✅ Commande confirmée n° {ref}\nTotal : *9 000 FCFA*")
    assert "https://pay.example/awa" in sent["body"]  # le lien de paiement n'est jamais perdu
    _png(wa.uploads[0])
    trace = (await db_session.execute(select(Message).where(Message.conversation_id == conversation.id,
                                                             Message.message_type == "order_confirmation"))).scalar_one()
    assert trace.content.startswith("🧾 Reçu en image envoyé") and "Référence" in trace.content  # l'historique garde le texte
    assert trace.message_metadata["guided"] == "receipt_image" and trace.message_metadata["fallback"]["body"].startswith("✅ Commande")


@pytest.mark.asyncio
async def test_without_the_image_the_usual_text_leaves(db_session):
    tenant, customer, conversation, turn = await _ordered(db_session)
    wa = Wa(refuse_upload=True)
    assert await guided_flow.deliver(db_session, tenant, conversation, wa, customer.whatsapp_number, turn) == 1
    assert wa.sent[-1]["image_id"] is None and wa.sent[-1]["body"].startswith("✅ Commande confirmée — Chez Tante Awa")

    class Refuses(Wa):  # l'image est déposée mais le message est refusé : la confirmation texte part
        async def send_buttons(self, to, body, buttons, image_link=None, image_id=None):
            if image_id:
                raise RuntimeError("refusé")
            return await super().send_buttons(to, body, buttons)

    turn.actions[0]["receipt"] = turn.actions[0].get("receipt") or "x"
    wa = Refuses()
    tenant2, customer2, conversation2, turn2 = await _ordered(db_session)
    await guided_flow.deliver(db_session, tenant2, conversation2, wa, customer2.whatsapp_number, turn2)
    assert len(wa.uploads) == 1 and wa.sent[-1]["body"].startswith("✅ Commande confirmée —")


def test_a_long_confirmation_skips_the_second_message_only_when_the_image_left():
    actions = guided_flow.confirmation("x" * 1200, [("g:s", "Suivre ma commande")], "00000000-0000-0000-0000-000000000000", False)
    assert actions[0]["type"] == "text" and actions[0]["receipt"] and actions[1]["skip_if_receipt"]
    short = guided_flow.confirmation("ok", [("g:s", "Suivre ma commande")], "00000000-0000-0000-0000-000000000000", False)
    assert len(short) == 1 and short[0]["type"] == "buttons"
    image = {"type": "buttons", "body": "c", "buttons": [], "image_id": "m", "original": {"type": "text", "body": "texte"}}
    assert guided_flow.degraded(image) == {"type": "text", "body": "texte"}


@pytest.mark.asyncio
async def test_what_the_receipt_shows(db_session):
    tenant = await _resto(db_session, kitchen_hours=None)
    dish = await _dish(db_session, tenant, name="Poulet braisé 🔥")
    customer, _ = await _conversation(db_session, tenant)
    customer.last_name = "koné"
    from app.services.order_service import create_order

    order = await create_order(db_session, tenant_id=tenant.id, customer_id=customer.id,
                               items=[{"product_id": str(dish.id), "quantity": 2, "option": "avec alloco"}],
                               delivery_address="Angré 7e tranche", payment_method="À la livraison", created_by="IA",
                               delivery_fee=1000, fulfillment="DELIVERY", delivery_area="Cocody",
                               scheduled_for=datetime.now(timezone.utc) + timedelta(days=1))
    data = await receipt_image.gather(db_session, order)
    assert data["client"] == "Awa K." and data["items"][0]["name"] == "Poulet braisé"  # l'émoji ne ferait qu'un carré
    assert data["items"][0]["detail"] == "avec alloco" and data["fee_row"] == ("Livraison · Cocody", "1 000")
    assert data["place"] == ("Livraison", "Angré 7e tranche") and data["payment"] == "À la livraison"
    assert data["scheduled"].startswith("Pour le ") and data["currency"] == "FCFA" and data["total"] == 10000
    assert data["accent"] == ("#C9913D", "#8A5A1F")  # restaurant sans choix : safran
    picture = _png(receipt_image.render(data))
    assert picture.getpixel((receipt_image.WIDTH // 2, 10)) == (250, 246, 238)  # fond crème
    paid = receipt_image.render({**data, "paid_on": "lundi 9 novembre, 12 h 05", "client": None, "fee_row": None,
                                 "scheduled": None, "place": None, "payment": None,
                                 "items": data["items"] * 40, "shop": "Un nom de restaurant vraiment très très long" * 3}, "paid")
    assert _png(paid).height < 3600  # 25 lignes au plus, puis « Et N autres articles »


def test_small_helpers():
    assert receipt_image.clean("  Bissap 🍹 50 cl ") == "Bissap 50 cl"
    assert receipt_image.currency_label("xof") == "FCFA" and receipt_image.currency_label("EUR") == "€"
    assert receipt_image.client_name(Customer(first_name="Awa")) == "Awa" and receipt_image.client_name(Customer()) is None
    order = Order(total_amount=14500, currency="XOF")
    order.id = __import__("uuid").UUID("8f2a1c3d-0000-0000-0000-000000000000")
    text = "✅ Commande confirmée — Awa\nRéférence : 8F2A1C3D\n\n- Poulet x2\n\nTotal : 14 500 XOF\n\nVous pouvez régler via : https://p\n\nUne fois…"
    assert receipt_image.caption(order, "confirmed", text) == ("✅ Commande confirmée n° 8F2A1C3D\nTotal : *14 500 FCFA*\n\n"
                                                              "Vous pouvez régler via : https://p\n\nUne fois…")
    assert receipt_image.caption(order, "paid", text) == "🧾 Reçu de commande n° 8F2A1C3D\nTotal : *14 500 FCFA*"
    for kind, expected in ((ONLINE_STORE, True), (RESTAURANT, True), (CAR_DEALERSHIP, False), (None, True)):
        assert receipt_image.applies(type("T", (), {"business_type": kind})()) is expected


@pytest.mark.asyncio
async def test_the_poster_tint_is_kept_and_colours_the_receipt(client, db_session):
    tenant = await _cabinet(db_session, business_type=ONLINE_STORE)
    headers = await _headers(client, tenant)
    assert (await client.get("/api/v1/kit/poster-options", headers=headers)).json()["current"] == "or"
    r = await client.put("/api/v1/kit/poster-accent", json={"accent": "sauge"}, headers=headers)
    assert r.status_code == 200 and r.json() == {"accent": "sauge"}
    assert (await client.get("/api/v1/kit/poster-options", headers=headers)).json()["current"] == "sauge"
    await db_session.refresh(tenant)
    assert tenant.poster_accent == "sauge" and receipt_image.accent_colors(tenant) == ("#A3B1A0", "#4F6150")
    assert (await client.put("/api/v1/kit/poster-accent", json={"accent": "violet"}, headers=headers)).status_code == 422
    assert (await client.put("/api/v1/kit/poster-accent", json={"accent": "safran"}, headers=headers)).json() == {"accent": "or"}
    assert 'onchange="savePosterAccent()"' in HTML and "/api/v1/kit/poster-accent" in HTML and "o.current" in HTML


@pytest.mark.asyncio
async def test_payment_received_sends_the_paid_receipt_image(client, db_session, monkeypatch):
    sent = Wa()
    monkeypatch.setattr("app.api.orders.routes.WhatsAppClient", lambda **kw: sent)
    tenant = await _cabinet(db_session, business_type=ONLINE_STORE)
    tenant.currency = "XOF"
    dish = await _dish(db_session, tenant, name="Robe wax", price=12000, code=None)
    customer, conversation = await _conversation(db_session, tenant)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER, content="Bonjour"))
    from app.services.order_service import create_order

    order = await create_order(db_session, tenant_id=tenant.id, customer_id=customer.id,
                               items=[{"product_id": str(dish.id), "quantity": 1}], delivery_address=None,
                               payment_method=None, created_by="IA", conversation_id=conversation.id)
    await db_session.commit()
    r = await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers=await _headers(client, tenant))
    assert r.status_code == 200, r.text
    assert sent.sent[-1]["image_id"] == "media-1" and sent.sent[-1]["body"].startswith("🧾 Reçu de commande n° ")
    _png(sent.uploads[0])
    receipt = (await db_session.execute(select(Message).where(Message.conversation_id == conversation.id,
                                                               Message.message_type == "receipt"))).scalar_one()
    assert receipt.content.startswith("🧾 Reçu de commande —")  # l'historique garde le texte complet

    dealer = await _cabinet(db_session, business_type=CAR_DEALERSHIP)  # pas un secteur à commandes : rien ne change
    assert not receipt_image.applies(dealer)
