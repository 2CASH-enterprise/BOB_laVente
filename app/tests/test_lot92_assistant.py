"""
Lot 92 — « Bob, votre assistant » sur le numéro AgenC'AI (contrat Bob ↔ Naomy 1.3.1, décisions du 27/10).

Portée choisie dans Bob, demande d'appairage venue de Naomy (confirmée dans Bob), messages du commerçant transmis
par Naomy, commandes sans IA, photo + légende, alertes (vente à confirmer, client qui attend), envoi signé par Naomy
avec plafond et repli par e-mail, bilan du soir par e-mail, interrupteurs (général et boutique).
"""
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.naomy import (AssistantMessage, AssistantOutbox, NaomyConnection, NaomyEvent, NaomyPairingCode,
                              NaomyPairingRequest)
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.tenant import TenantPlan
from app.models.whatsapp_account import WhatsAppAccount
from app.services import merchant_assistant as ma
from app.services import naomy_bridge as nb
from app.tests.test_lot89_connecteur_naomy import (DASH, EVENTS_URL, SECRET, _contract, _login, _shop, _signed,
                                                   conforms, naomy_env)  # noqa: F401 — fixture

NOW = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)  # 10 h à Abidjan
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class Sent:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    async def __call__(self, url, body, headers):
        self.calls.append((url, json.loads(body), headers, body))
        return self.status


class Mails:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, body, **kw):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return True


@pytest.fixture
def assistant_env(naomy_env, monkeypatch, tmp_path):
    monkeypatch.setattr(naomy_env, "assistant_enabled", True)
    monkeypatch.setattr(ma, "STORE", tmp_path / "assistant")
    monkeypatch.setattr(ma, "PRODUCT_UPLOADS", tmp_path / "products")
    monkeypatch.setattr(ma, "schedule", lambda _id: None)
    sender, mails = Sent(), Mails()
    monkeypatch.setattr(ma, "http_post", sender)
    import app.services.email_service as email_service

    monkeypatch.setattr(email_service, "send_email", mails)
    from types import SimpleNamespace

    return SimpleNamespace(settings=naomy_env, sender=sender, mails=mails)


async def _linked(client, db, plan=TenantPlan.PRO, scope="complete", on=True, now=NOW):
    tenant, email = await _shop(db, plan=plan)
    code = nb.new_code()
    db.add(NaomyPairingCode(tenant_id=tenant.id, code_hash=nb.digest(code), scope=scope,
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)))
    await db.commit()
    raw, headers = _signed({"code": code, "compte_naomy": "nao_1", "numero_confirmation": "+2250701020312",
                            "url_evenements": EVENTS_URL})
    r = await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    connection = (await db.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant.id))).scalar_one()
    connection.assistant_enabled, connection.assistant_enabled_at = on, now - timedelta(hours=1)
    await db.commit()
    return tenant, email, {"Authorization": f"Bearer {r.json()['cle_api']}"}, connection


def _msg(text=None, kind="texte", button=None, media=None, wa_id=None):
    body = {"id_message": wa_id or f"wamid.{uuid.uuid4().hex}", "recu_le": "2026-10-27T10:00:00Z", "type": kind}
    if text is not None:
        body["texte"] = text
    if button:
        body["bouton_id"] = button
    if media:
        body["media"] = media
    return body


_CLOCK = [0]


async def _say(client, db, headers, body):
    r = await client.post("/api/v1/naomy/assistant/messages", json=body, headers=headers)
    assert r.status_code == 202, r.text
    row = (await db.execute(select(AssistantMessage).where(AssistantMessage.wa_message_id == body["id_message"])
                            .execution_options(populate_existing=True))).scalar_one()
    _CLOCK[0] += 1  # une seconde entre deux messages : les réponses restent dans l'ordre
    await ma.process(db, row.id, NOW + timedelta(seconds=_CLOCK[0]))
    return row


async def _replies(db, tenant):
    rows = (await db.execute(select(AssistantOutbox).where(AssistantOutbox.tenant_id == tenant.id)
                             .order_by(AssistantOutbox.created_at, AssistantOutbox.message_id)
                             .execution_options(populate_existing=True))).scalars().all()
    for row in rows:  # chaque envoi suit le schéma EnvoiAssistant du contrat
        conforms(row.payload, "EnvoiAssistant")
        for b in row.payload.get("boutons") or []:
            assert b["id"].startswith("bob:") and 1 <= len(b["titre"]) <= 20
    return rows


async def _order(db, tenant, status=OrderStatus.PENDING, number="2250700001111", name="Aïcha", created=None, stock=3):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number, first_name=name)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
                                last_message_at=NOW)
    product = Product(tenant_id=tenant.id, sku=f"R{uuid.uuid4().hex[:6]}", name="Robe longue wax", price=15000,
                      currency="XOF", stock_quantity=stock, active=True)
    db.add_all([conversation, product])
    await db.flush()
    order = Order(tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id, status=status,
                  total_amount=30000, currency="XOF", created_at=created or NOW - timedelta(hours=1))
    db.add(order)
    await db.flush()
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=2, unit_price=15000, subtotal=30000))
    await db.commit()
    return order, conversation, product


# --- Commandes et légendes, sans IA --------------------------------------------------------------------------------

def test_commands_follow_the_contract_list():
    contract = {ma.normalize(w) for w in _contract()["x-commandes-bob"]["mots"]}
    assert contract <= set(ma.COMMANDS)
    assert ma.match_command("Bob, mes ventes") == ("sales", "")
    assert ma.match_command("STOCK robe wax") == ("stock", "robe wax")
    assert ma.match_command("Ventes à confirmer") == ("to_confirm", "")
    assert ma.match_command("vendus") == (None, "vendus")  # message entier ou premier mot suivi d'un espace
    assert ma.match_command("Bonjour") == (None, "bonjour")


@pytest.mark.parametrize("caption, expected", [
    ("Bob, Sac cabas en raphia 12000", ("Sac cabas en raphia", 12000)),
    ("bob sac cabas 12 000 FCFA", ("sac cabas", 12000)),
    ("Bob: Robe wax - 15.000F", ("Robe wax", 15000)),
    ("Bob, Chaussures 1,500,000", ("Chaussures", 1500000)),
    ("Bob, Sac cabas", ("Sac cabas", None)),
    ("Bob, 12000", (None, 12000)),
])
def test_caption_gives_name_and_price(caption, expected):
    name, price = ma.parse_caption(caption)
    assert (name, int(price) if price is not None else None) == expected


# --- Portée et demande d'appairage -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scope_is_decided_by_bob_and_free_shops_get_the_assistant_only(client, db_session, assistant_env):
    free, email = await _shop(db_session, plan=TenantPlan.FREE)
    headers = await _login(client, email)
    r = await client.post("/api/v1/naomy-connection/pairing", json={"scope": "complete"}, headers=headers)
    assert r.status_code == 403
    r = await client.post("/api/v1/naomy-connection/pairing", json={"scope": "assistant"}, headers=headers)
    assert r.status_code == 200
    code = r.json()["url"].rsplit("=", 1)[1]
    assert (await db_session.execute(select(NaomyPairingCode.scope))).scalar_one() == "assistant"
    # Naomy envoie « complete » : ignoré, c'est le choix du commerçant qui compte.
    raw, h = _signed({"code": code, "compte_naomy": "n", "numero_confirmation": "+2250701020312",
                      "url_evenements": EVENTS_URL, "portee": "complete"})
    r = await client.post("/api/v1/naomy/appairage", content=raw, headers=h)
    assert r.status_code == 200 and r.json()["portee"] == "assistant"
    conforms(r.json(), "ReponseAppairage")
    view = (await client.get("/api/v1/naomy-connection", headers=headers)).json()
    assert view["scope"] == "assistant" and view["suspended"] is False and view["reminders_enabled"] is None
    assert view["assistant_available"] is True and view["assistant_enabled"] is False  # éteint par défaut
    assert (await client.post("/api/v1/naomy-connection/pairing", json={"scope": "autre"}, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_assistant_only_connection_sends_no_catalog_event(client, db_session, assistant_env):
    tenant, _, _, connection = await _linked(client, db_session, plan=TenantPlan.FREE, scope="assistant")
    db_session.add(Product(tenant_id=tenant.id, sku="A", name="A", price=1, currency="XOF", stock_quantity=1,
                           image_url="https://x/a.jpg", active=True))
    await db_session.commit()
    sender = Sent()
    await nb.dispatch(db_session, now=datetime.now(timezone.utc) + timedelta(seconds=5), sender=sender)
    assert [c[1]["type"] for c in sender.calls] == ["connexion.ouverte"]
    statuses = dict((await db_session.execute(select(NaomyEvent.type, NaomyEvent.status))).all())
    assert statuses["produit.cree"] == "SKIPPED"


class Limiter:
    def __init__(self):
        self.keys = set()

    async def is_allowed(self, key, limit, window):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


@pytest.mark.asyncio
async def test_pairing_request_never_reveals_a_shop_and_is_confirmed_in_bob(client, db_session, assistant_env):
    tenant, email = await _shop(db_session)
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-l92-req", system_user_token="t",
                                   display_phone_number="+225 07 01 02 03 04"))
    await db_session.commit()
    for number in ("+2250701020304", "+2250799999999"):  # connue ou non : toujours 202, même corps
        raw, h = _signed({"numero_whatsapp": number, "compte_naomy": "nao_9", "nom_boutique": "Wax & Co"})
        r = await client.post("/api/v1/naomy/appairage/demande", content=raw, headers=h)
        assert r.status_code == 202 and r.json() == {"statut": "recue"}
    raw, h = _signed({"numero_whatsapp": "+2250701020304", "compte_naomy": "x"}, secret="faux")
    assert (await client.post("/api/v1/naomy/appairage/demande", content=raw, headers=h)).status_code == 401
    raw, h = _signed({"numero_whatsapp": "0701020304", "compte_naomy": "x"})
    assert (await client.post("/api/v1/naomy/appairage/demande", content=raw, headers=h)).status_code == 400

    [request] = (await db_session.execute(select(NaomyPairingRequest))).scalars().all()
    assert request.tenant_id == tenant.id and request.shop_name == "Wax & Co"
    [mail] = assistant_env.mails.sent
    assert mail["to"] == email and "Naomy demande à relier" in mail["subject"] and "Intégrations" in mail["body"]

    headers = await _login(client, email)
    view = (await client.get("/api/v1/naomy-connection", headers=headers)).json()
    assert view["request"]["id"] == str(request.id)
    r = await client.post(f"/api/v1/naomy-connection/request/{request.id}", json={"accept": True, "scope": "assistant"},
                          headers=headers)
    assert r.status_code == 200 and r.json()["url"].startswith("https://agenc-ai.com/naomy/connexion/bob?code=")
    assert r.json()["status"]["request"] is None
    assert (await db_session.execute(select(NaomyPairingCode.scope))).scalar_one() == "assistant"
    again = await client.post(f"/api/v1/naomy-connection/request/{request.id}", json={"accept": False}, headers=headers)
    assert again.status_code == 404  # déjà traitée


@pytest.mark.asyncio
async def test_pairing_request_limits_and_refusal(db_session, assistant_env):
    tenant, _ = await _shop(db_session)
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-l92-lim", system_user_token="t",
                                   display_phone_number="+2250701020305"))
    await db_session.commit()
    limiter = Limiter()
    body = {"numero_whatsapp": "+2250701020305", "compte_naomy": "n"}
    await nb.receive_request(db_session, body, limiter, mailer=Mails())
    await nb.receive_request(db_session, body, limiter, mailer=Mails())  # même numéro, même jour : ignorée
    [request] = (await db_session.execute(select(NaomyPairingRequest))).scalars().all()
    assert await nb.decide_request(db_session, tenant, str(request.id), accept=False) is None
    assert request.status == "REFUSED"
    assert (await db_session.execute(select(NaomyPairingCode))).first() is None  # rien n'est relié


# --- Réception des messages ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_is_closed_until_both_switches_are_on(client, db_session, assistant_env, monkeypatch):
    tenant, _, headers, connection = await _linked(client, db_session, on=False)
    r = await client.post("/api/v1/naomy/assistant/messages", json=_msg("mes ventes"), headers=headers)
    assert r.status_code == 409 and r.json()["erreur"] == "assistant_eteint"
    connection.assistant_enabled = True
    await db_session.commit()
    monkeypatch.setattr(assistant_env.settings, "assistant_enabled", False)
    assert (await client.post("/api/v1/naomy/assistant/messages", json=_msg("mes ventes"), headers=headers)).status_code == 409
    monkeypatch.setattr(assistant_env.settings, "assistant_enabled", True)
    body = _msg("mes ventes", wa_id="wamid.UN")
    r = await client.post("/api/v1/naomy/assistant/messages", json=body, headers=headers)
    assert r.status_code == 202 and r.json() == {"id_message": "wamid.UN", "statut": "recu"}
    r = await client.post("/api/v1/naomy/assistant/messages", json=body, headers=headers)
    assert r.json()["statut"] == "deja_recu"
    assert (await client.post("/api/v1/naomy/assistant/messages", json=_msg("x"),
                              headers={"Authorization": "Bearer bnk_faux"})).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("body, status", [
    ({"id_message": "a", "recu_le": "x", "type": "fax"}, 400),
    ({"id_message": "a", "recu_le": "x", "type": "texte"}, 400),
    ({"id_message": "a", "recu_le": "x", "type": "bouton", "bouton_id": "naomy:menu"}, 400),
    ({"id_message": "a", "recu_le": "x", "type": "bouton"}, 400),
    ({"id_message": "a", "recu_le": "x", "type": "texte", "texte": "ok", "autre": 1}, 400),
    ({"id_message": "a", "recu_le": "x", "type": "image", "media": {"mime": "image/gif", "base64": "AAAA"}}, 400),
    ({"id_message": "a", "recu_le": "x", "type": "image", "media": {"mime": "image/png", "base64": "@@@"}}, 400),
])
async def test_invalid_messages_are_refused(client, db_session, assistant_env, body, status):
    _, _, headers, _ = await _linked(client, db_session)
    r = await client.post("/api/v1/naomy/assistant/messages", json=body, headers=headers)
    assert r.status_code == status and "erreur" in r.json()


@pytest.mark.asyncio
async def test_media_size_limits(client, db_session, assistant_env, monkeypatch):
    _, _, headers, _ = await _linked(client, db_session)
    monkeypatch.setattr(ma, "MAX_IMAGE", 10)
    big = base64.b64encode(b"x" * 11).decode()
    r = await client.post("/api/v1/naomy/assistant/messages", headers=headers,
                          json=_msg(kind="image", media={"mime": "image/png", "base64": big}))
    assert r.status_code == 413
    voice = base64.b64encode(b"o" * 2000).decode()
    r = await client.post("/api/v1/naomy/assistant/messages", headers=headers,
                          json=_msg(kind="audio", media={"mime": "audio/ogg", "base64": voice}))
    assert r.status_code == 202
    row = (await db_session.execute(select(AssistantMessage).where(AssistantMessage.kind == "audio"))).scalar_one()
    assert row.media_path is None  # une note vocale n'est jamais gardée


# --- Traitement : réponses, actions, rien de gardé --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sales_command_and_the_text_is_erased(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    order, _, _ = await _order(db_session, tenant, status=OrderStatus.PAID)
    order.paid_at = NOW - timedelta(minutes=5)
    await db_session.commit()
    row = await _say(client, db_session, headers, _msg("Bob, mes ventes"))
    [reply] = await _replies(db_session, tenant)
    assert reply.kind == "REPLY" and reply.status == "SENT"
    assert "Aujourd'hui : 30 000 XOF (1 vente)" in reply.payload["texte"]
    await db_session.refresh(row)
    assert row.status == "DONE" and row.text is None and row.button_id is None
    url, body, sent_headers, raw = assistant_env.sender.calls[0]
    assert url == "https://agenc-ai.com/naomy/api/bob/assistant/envoyer"
    connection = (await db_session.execute(select(NaomyConnection))).scalar_one()
    assert sent_headers["X-Bob-Signature"] == nb.sign(connection.events_secret, sent_headers["X-Bob-Timestamp"], raw)
    assert body["boutique_id"] == str(tenant.id) and not body["texte"].startswith("Bob ·")


@pytest.mark.asyncio
async def test_vendu_asks_then_the_button_confirms_like_the_dashboard(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    order, _, _ = await _order(db_session, tenant)
    await _say(client, db_session, headers, _msg("vendu"))
    [ask] = await _replies(db_session, tenant)
    assert [b["id"] for b in ask.payload["boutons"]] == [f"bob:vendu:{order.id}", "bob:retour"]
    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING  # le mot seul ne confirme rien
    await _say(client, db_session, headers, _msg(kind="bouton", button=f"bob:vendu:{order.id}"))
    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID and order.paid_at is not None
    replies = await _replies(db_session, tenant)
    assert replies[-1].payload["texte"].startswith("Vente confirmée : Aïcha (30 000 XOF)")
    await _say(client, db_session, headers, _msg(kind="bouton", button=f"bob:vendu:{order.id}"))
    assert (await _replies(db_session, tenant))[-1].payload["texte"] == "Cette vente est déjà confirmée."


@pytest.mark.asyncio
async def test_annuler_never_cancels_without_an_explicit_yes(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    order, _, product = await _order(db_session, tenant, stock=3)
    await _say(client, db_session, headers, _msg("annuler"))
    await _say(client, db_session, headers, _msg(kind="bouton", button=f"bob:annuler:{order.id}"))
    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING
    replies = await _replies(db_session, tenant)
    assert all(b["id"] != f"bob:vendu:{order.id}" for r in replies for b in r.payload.get("boutons") or [])
    assert [b["id"] for b in replies[-1].payload["boutons"]] == [f"bob:annuler-oui:{order.id}", "bob:retour"]
    await _say(client, db_session, headers, _msg(kind="bouton", button="bob:retour"))
    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING
    await _say(client, db_session, headers, _msg(kind="bouton", button=f"bob:annuler-oui:{order.id}"))
    await db_session.refresh(order)
    await db_session.refresh(product)
    assert order.status == OrderStatus.CANCELLED and product.stock_quantity == 5  # articles remis en stock
    assert "Vente annulée" in (await _replies(db_session, tenant))[-1].payload["texte"]


@pytest.mark.asyncio
async def test_several_sales_offer_one_button_each(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    for i in range(4):
        await _order(db_session, tenant, number=f"22507000011{i:02d}", name=f"Cliente{i}")
    await _say(client, db_session, headers, _msg("vendu"))
    [ask] = await _replies(db_session, tenant)
    assert len(ask.payload["boutons"]) == 3 and all(b["id"].startswith("bob:vendu:") for b in ask.payload["boutons"])
    assert "Les autres sont dans Bob" in ask.payload["texte"]


@pytest.mark.asyncio
async def test_waiting_and_bob_continues(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    _, conversation, _ = await _order(db_session, tenant, status=OrderStatus.PAID)
    conversation.status = ConversationStatus.WAITING_HUMAN
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                           message_type="text", content="Vous l'avez en 46 ?", created_at=NOW))
    await db_session.commit()
    await _say(client, db_session, headers, _msg("qui attend"))
    [reply] = await _replies(db_session, tenant)
    assert "Aïcha : « Vous l'avez en 46 ? »" in reply.payload["texte"]
    assert reply.payload["boutons"] == [{"id": f"bob:continue:{conversation.id}", "titre": "Bob : Aïcha"}]
    await _say(client, db_session, headers, _msg(kind="bouton", button=f"bob:continue:{conversation.id}"))
    await db_session.refresh(conversation)
    assert conversation.status == ConversationStatus.ACTIVE
    assert (await _replies(db_session, tenant))[-1].payload["texte"] == "Bob reprend la conversation avec Aïcha."


@pytest.mark.asyncio
async def test_stock_menu_and_voice_notes(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    db_session.add_all([Product(tenant_id=tenant.id, sku="E", name="Écharpe soie", price=1, currency="XOF", stock_quantity=0, active=True),
                        Product(tenant_id=tenant.id, sku="S", name="Sac cabas", price=1, currency="XOF", stock_quantity=2, active=True)])
    await db_session.commit()
    await _say(client, db_session, headers, _msg("stock"))
    await _say(client, db_session, headers, _msg("stock echarpe"))
    await _say(client, db_session, headers, _msg("Bonjour Bob"))
    await _say(client, db_session, headers, _msg(kind="audio", media={"mime": "audio/ogg", "base64": base64.b64encode(b"o").decode()}))
    texts = [r.payload["texte"] for r in await _replies(db_session, tenant)]
    assert "Épuisés (Bob ne les propose plus) :\n• Écharpe soie" in texts[0] and "• Sac cabas : 2" in texts[0]
    assert texts[1] == "• Écharpe soie : 0 en stock"  # recherche sans accents
    assert texts[2:4] == [ma.MENU_1[0], ma.MENU_2[0]]  # lot 92c : le menu en 2 messages de 3 boutons
    assert texts[4] == ma.VOICE_REPLY


@pytest.mark.asyncio
async def test_photo_with_caption_creates_the_product_then_asks_the_stock(client, db_session, assistant_env):
    tenant, _, headers, connection = await _linked(client, db_session)
    media = {"mime": "image/png", "base64": base64.b64encode(PNG).decode()}
    row = await _say(client, db_session, headers, _msg("Bob, Sac cabas en raphia 12 000", kind="image", media=media))
    product = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalar_one()
    assert product.name == "Sac cabas en raphia" and int(product.price) == 12000 and product.stock_quantity == 1
    assert product.image_status == "PENDING" and product.image_url.startswith("https://agenc-ai.com/bob/uploads/products/")
    assert product.sku.startswith("SAC-CABAS-EN-RAPHIA-")
    assert list((ma.PRODUCT_UPLOADS).iterdir()) and not list(ma.STORE.iterdir())  # copie publiée, original effacé
    [reply] = await _replies(db_session, tenant)
    assert reply.payload["attend_reponse"] is True and "combien en avez-vous" in reply.payload["texte"]
    await db_session.refresh(row)
    assert row.media_path is None and row.text is None
    await _say(client, db_session, headers, _msg("mes ventes"))  # pas un nombre : une autre demande, stock inchangé
    await db_session.refresh(product)
    assert product.stock_quantity == 1
    await _say(client, db_session, headers, _msg("5"))
    await db_session.refresh(product)
    await db_session.refresh(connection)
    assert product.stock_quantity == 5 and connection.awaiting is None
    assert (await _replies(db_session, tenant))[-1].payload["texte"] == "C'est noté : 5 en stock pour Sac cabas en raphia."


def _photo(text=None):
    return _msg(text, kind="image", media={"mime": "image/png", "base64": base64.b64encode(PNG).decode()})


async def _products(db, tenant):
    return (await db.execute(select(Product).where(Product.tenant_id == tenant.id)
                             .execution_options(populate_existing=True))).scalars().all()


@pytest.mark.asyncio
async def test_photo_alone_then_name_and_price(client, db_session, assistant_env):
    """Lot 92b : photo seule, Bob la garde et demande le nom et le prix."""
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _photo())
    assert await _products(db_session, tenant) == []
    [ask] = await _replies(db_session, tenant)
    assert ask.payload["texte"] == ma.PHOTO_ASK and ask.payload["attend_reponse"] is True
    assert len(list(ma.STORE.iterdir())) == 1  # gardée en attendant
    await _say(client, db_session, headers, _msg("Sac cabas 12 000"))
    [product] = await _products(db_session, tenant)
    assert product.name == "Sac cabas" and int(product.price) == 12000 and product.image_url
    assert not list(ma.STORE.iterdir()) and len(list(ma.PRODUCT_UPLOADS.iterdir())) == 1
    assert "combien en avez-vous" in (await _replies(db_session, tenant))[-1].payload["texte"]


@pytest.mark.asyncio
async def test_photo_with_a_name_only_asks_the_price(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _photo("Bob, joli sac"))
    assert (await _replies(db_session, tenant))[-1].payload["texte"] == "J'ai la photo de « joli sac ». Quel est son prix ? Par exemple : 12000"
    await _say(client, db_session, headers, _msg("un très joli sac"))  # toujours pas de prix
    assert (await _replies(db_session, tenant))[-1].payload["texte"] == ma.PRICE_MISSING
    await _say(client, db_session, headers, _msg("8000"))
    [product] = await _products(db_session, tenant)
    assert product.name == "un très joli sac" and int(product.price) == 8000


@pytest.mark.asyncio
async def test_a_command_still_works_while_a_photo_waits(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _photo())
    await _say(client, db_session, headers, _msg("mes ventes"))
    assert (await _replies(db_session, tenant))[-1].payload["texte"].startswith("Vos ventes confirmées")
    assert await _products(db_session, tenant) == [] and len(list(ma.STORE.iterdir())) == 1
    await _say(client, db_session, headers, _msg("Robe wax 15000"))
    assert [p.name for p in await _products(db_session, tenant)] == ["Robe wax"]


@pytest.mark.asyncio
async def test_name_and_price_first_then_the_photo(client, db_session, assistant_env):
    tenant, _, headers, connection = await _linked(client, db_session)
    await _say(client, db_session, headers, _msg("Sac cabas 12000"))
    [ask] = await _replies(db_session, tenant)
    assert ask.payload["texte"] == "Ajouter « Sac cabas » à 12 000 XOF ? Envoyez sa photo." and ask.payload["attend_reponse"]
    assert await _products(db_session, tenant) == []  # rien sans la photo
    await _say(client, db_session, headers, _photo())
    [product] = await _products(db_session, tenant)
    assert product.name == "Sac cabas" and int(product.price) == 12000 and product.image_url
    await db_session.refresh(connection)
    assert connection.awaiting.startswith("stock:")


@pytest.mark.asyncio
async def test_commands_and_plain_words_are_never_products(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _msg("vendu 30000"))
    await _say(client, db_session, headers, _msg("Bonjour"))
    await _say(client, db_session, headers, _msg("15000"))
    texts = [r.payload["texte"] for r in await _replies(db_session, tenant)]
    assert texts == ["Aucune vente à confirmer pour l'instant.", ma.MENU_1[0], ma.MENU_2[0], ma.MENU_1[0], ma.MENU_2[0]]
    assert all("attend_reponse" not in r.payload for r in await _replies(db_session, tenant))


@pytest.mark.asyncio
async def test_a_new_photo_replaces_the_waiting_one_and_old_photos_are_cleaned(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _photo())
    first = {p.name for p in ma.STORE.iterdir()}
    await _say(client, db_session, headers, _photo())
    second = {p.name for p in ma.STORE.iterdir()}
    assert len(second) == 1 and second != first
    assert ma.clean_store(datetime.now(timezone.utc)) == 0
    report = await ma.tick(db_session, datetime.now(timezone.utc) + timedelta(hours=3), sender=Sent(), mailer=Mails())
    assert report["photos_removed"] == 1 and not list(ma.STORE.iterdir())  # la tâche de chaque minute fait le ménage


@pytest.mark.asyncio
async def test_waiting_ends_after_30_minutes(client, db_session, assistant_env):
    tenant, _, headers, connection = await _linked(client, db_session)
    await _say(client, db_session, headers, _photo())
    await db_session.refresh(connection)
    connection.awaiting_until = NOW - timedelta(minutes=1)
    await db_session.commit()
    await _say(client, db_session, headers, _msg("12000"))
    assert await _products(db_session, tenant) == []
    assert (await _replies(db_session, tenant))[-1].payload["texte"] == ma.MENU_2[0]


@pytest.mark.asyncio
async def test_free_plan_product_limit_applies(client, db_session, assistant_env, monkeypatch):
    import app.services.plan_limits as plan_limits

    tenant, _, headers, _ = await _linked(client, db_session, plan=TenantPlan.FREE, scope="assistant")

    async def none_left(*_a, **_k):
        return 0

    monkeypatch.setattr(plan_limits, "remaining_product_slots", none_left)
    media = {"mime": "image/png", "base64": base64.b64encode(PNG).decode()}
    await _say(client, db_session, headers, _msg("Bob, Sac 5000", kind="image", media=media))
    assert (await db_session.execute(select(Product))).first() is None
    assert "formule gratuite" in (await _replies(db_session, tenant))[0].payload["texte"]


# --- Envoi par Naomy : renvois, plafond, repli par e-mail -------------------------------------------------------------

@pytest.mark.asyncio
async def test_closed_window_alert_goes_by_email_and_reply_is_dropped(client, db_session, assistant_env):
    tenant, email, _, connection = await _linked(client, db_session)
    alert = ma.queue(db_session, connection, "Aïcha vous attend", kind="ALERT", ref="attente:x",
                     fallback_subject="Un client vous attend : Aïcha", now=NOW)
    reply = ma.queue(db_session, connection, "Réponse", now=NOW)
    await db_session.commit()
    report = await ma.send_outbox(db_session, NOW, sender=Sent(409), mailer=assistant_env.mails)
    assert report["emailed"] == 1 and report["failed"] == 1
    assert alert.status == "EMAILED" and reply.status == "FAILED"
    [mail] = assistant_env.mails.sent
    assert mail["to"] == email and mail["subject"] == "Un client vous attend : Aïcha" and "Aïcha vous attend" in mail["body"]


@pytest.mark.asyncio
async def test_retries_then_email(client, db_session, assistant_env):
    _, _, _, connection = await _linked(client, db_session)
    alert = ma.queue(db_session, connection, "Alerte", kind="ALERT", now=NOW)
    await db_session.commit()
    down = Sent(503)
    moment = NOW
    for minutes in ma.RETRY_MINUTES:
        await ma.send_outbox(db_session, moment, sender=down, mailer=assistant_env.mails)
        assert alert.status == "PENDING" and alert.next_attempt_at == moment + timedelta(minutes=minutes)
        moment = alert.next_attempt_at
    await ma.send_outbox(db_session, moment, sender=down, mailer=assistant_env.mails)
    assert alert.status == "EMAILED" and len(down.calls) == 4


@pytest.mark.asyncio
async def test_daily_cap_of_30(client, db_session, assistant_env):
    _, _, _, connection = await _linked(client, db_session)
    for _ in range(ma.DAILY_CAP):
        ma.queue(db_session, connection, "ok", now=NOW)
    await db_session.commit()
    sender = Sent()
    assert (await ma.send_outbox(db_session, NOW, sender=sender, mailer=assistant_env.mails))["sent"] == 30
    reply = ma.queue(db_session, connection, "une de trop", now=NOW)
    alert = ma.queue(db_session, connection, "alerte de trop", kind="ALERT", now=NOW)
    await db_session.commit()
    await ma.send_outbox(db_session, NOW, sender=sender, mailer=assistant_env.mails)
    assert len(sender.calls) == 30 and reply.status == "SKIPPED" and alert.status == "EMAILED"


# --- Alertes ---------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_image_from_a_customer_with_a_pending_order_is_a_sale_to_confirm(client, db_session, assistant_env):
    tenant, _, _, connection = await _linked(client, db_session)
    order, conversation, _ = await _order(db_session, tenant)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                           message_type="image", content="[image]", created_at=NOW - timedelta(minutes=2)))
    await db_session.commit()
    assert (await ma.scan_alerts(db_session, NOW))["sales"] == 1
    assert (await ma.scan_alerts(db_session, NOW))["sales"] == 0  # une seule alerte par vente
    [alert] = await _replies(db_session, tenant)
    assert alert.kind == "ALERT" and alert.ref == f"vente:{order.id}"
    assert "Aïcha a envoyé une image pour sa commande de 30 000 XOF (2 × Robe longue wax)" in alert.payload["texte"]
    assert [b["id"] for b in alert.payload["boutons"]] == [f"bob:vendu:{order.id}", f"bob:annuler:{order.id}"]


@pytest.mark.asyncio
async def test_no_alert_for_old_or_unrelated_images(client, db_session, assistant_env):
    tenant, _, _, connection = await _linked(client, db_session)
    connection.assistant_enabled_at = NOW - timedelta(minutes=1)  # allumé il y a une minute
    order, conversation, _ = await _order(db_session, tenant)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                           message_type="image", content="[image]", created_at=NOW - timedelta(minutes=5)))
    paid, other, _ = await _order(db_session, tenant, status=OrderStatus.PAID, number="2250700002222")
    db_session.add(Message(tenant_id=tenant.id, conversation_id=other.id, sender=MessageSender.CUSTOMER,
                           message_type="image", content="[image]", created_at=NOW))
    await db_session.commit()
    assert (await ma.scan_alerts(db_session, NOW))["sales"] == 0


@pytest.mark.asyncio
async def test_waiting_customer_alert_once_per_12_hours(client, db_session, assistant_env):
    tenant, _, _, connection = await _linked(client, db_session)
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-l92-wait", system_user_token="t",
                                   coexistence_mode=True))
    _, conversation, _ = await _order(db_session, tenant, status=OrderStatus.PAID)
    conversation.status, conversation.last_message_at = ConversationStatus.WAITING_HUMAN, NOW - timedelta(minutes=3)
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                           message_type="text", content="Je peux passer ce soir ?", created_at=NOW - timedelta(minutes=3)))
    await db_session.commit()
    assert (await ma.scan_alerts(db_session, NOW))["waiting"] == 1
    assert (await ma.scan_alerts(db_session, NOW + timedelta(minutes=10)))["waiting"] == 0  # pas de doublon
    [alert] = await _replies(db_session, tenant)
    assert "Aïcha vous attend : « Je peux passer ce soir ? »" in alert.payload["texte"]
    assert "https://wa.me/2250700001111" in alert.payload["texte"]  # coexistence : réponse depuis son téléphone
    assert alert.payload["boutons"] == [{"id": f"bob:continue:{conversation.id}", "titre": "Bob continue"}]


@pytest.mark.asyncio
async def test_no_alert_when_switched_off(client, db_session, assistant_env, monkeypatch):
    tenant, _, _, connection = await _linked(client, db_session, on=False)
    order, conversation, _ = await _order(db_session, tenant)
    conversation.status = ConversationStatus.WAITING_HUMAN
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                           message_type="image", content="[image]", created_at=NOW))
    await db_session.commit()
    assert await ma.scan_alerts(db_session, NOW) == {"sales": 0, "waiting": 0}
    connection.assistant_enabled = True
    await db_session.commit()
    monkeypatch.setattr(assistant_env.settings, "assistant_enabled", False)
    assert await ma.scan_alerts(db_session, NOW) == {"sales": 0, "waiting": 0}


# --- Bilan du soir par e-mail ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evening_summary_once_at_20h(client, db_session, assistant_env):
    tenant, email, _, connection = await _linked(client, db_session)
    await _order(db_session, tenant)
    mails = Mails()
    assert (await ma.send_summaries(db_session, NOW.replace(hour=19, minute=59), mailer=mails))["sent"] == 0
    evening = NOW.replace(hour=20, minute=1)
    assert (await ma.send_summaries(db_session, evening, mailer=mails))["sent"] == 1
    assert (await ma.send_summaries(db_session, evening + timedelta(hours=1), mailer=mails))["sent"] == 0
    [mail] = mails.sent
    assert mail["to"] == email and mail["subject"].startswith("Votre journée avec Bob : 0 vente")
    assert "- 1 vente à confirmer" in mail["body"] and "Intégrations" in mail["body"]
    connection.summary_enabled, connection.last_summary_on = False, None
    await db_session.commit()
    assert (await ma.send_summaries(db_session, evening, mailer=mails))["sent"] == 0


# --- Carte du tableau de bord ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_card_switches(client, db_session, assistant_env, monkeypatch):
    tenant, email, _, connection = await _linked(client, db_session, on=False)
    connection.assistant_enabled_at = None
    await db_session.commit()
    headers = await _login(client, email)
    monkeypatch.setattr(assistant_env.settings, "assistant_enabled", False)
    r = await client.put("/api/v1/naomy-connection/assistant", json={"enabled": True}, headers=headers)
    assert r.status_code == 403 and "bientôt" in r.json()["detail"]
    assert (await client.get("/api/v1/naomy-connection", headers=headers)).json()["assistant_available"] is False
    monkeypatch.setattr(assistant_env.settings, "assistant_enabled", True)
    view = (await client.put("/api/v1/naomy-connection/assistant", json={"enabled": True}, headers=headers)).json()
    assert view["assistant_enabled"] is True and view["summary_enabled"] is True
    await db_session.refresh(connection)
    assert connection.assistant_enabled_at is not None
    view = (await client.put("/api/v1/naomy-connection/assistant", json={"summary": False}, headers=headers)).json()
    assert view["summary_enabled"] is False


def test_dashboard_card_markup():
    for text in ("Ce que vous reliez", "L'assistant seul", "Naomy demande à relier votre boutique", "Accepter", "Refuser",
                 "Bob, votre assistant sur WhatsApp", "Bilan du soir par e-mail (20 h)", "function decideNaomyRequest(",
                 "/api/v1/naomy-connection/assistant", "scope: chosenNaomyScope()"):
        assert text in DASH, text


def test_contract_documents_the_new_rules():
    doc = _contract()
    assert "/appairage/demande" in doc["paths"] and doc["paths"]["/appairage/demande"]["post"]["responses"].get("202")
    assert "portee" in doc["components"]["schemas"]["ReponseAppairage"]["required"]
    assert doc["components"]["schemas"]["DemandeAppairage"]["properties"]["portee"].get("deprecated") is True
    assert "25 Mo" in doc["paths"]["/assistant/messages"]["post"]["description"]


@pytest.mark.asyncio
async def test_nothing_leaves_after_disconnection(client, db_session, assistant_env):
    _, _, _, connection = await _linked(client, db_session)
    item = ma.queue(db_session, connection, "Alerte", kind="ALERT", now=NOW)
    nb.close(db_session, connection, "BOB", NOW)
    await db_session.commit()
    sender = Sent()
    await ma.send_outbox(db_session, NOW, sender=sender, mailer=assistant_env.mails)
    assert item.status == "SKIPPED" and sender.calls == [] and assistant_env.mails.sent == []
