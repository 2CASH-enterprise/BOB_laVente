"""
Lot 94 — parcours à boutons sur WhatsApp (commerce) : accueil, rayons, produits avec leur prix, détails, tailles.
Le code répond aux boutons, sans IA ; une vraie question va à l'IA ; « Parler à un conseiller » = transfert.
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.category import Category
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.product import Product
from app.services import guided_flow as gf
from app.tests.fakes import text_response
from app.tests.test_handoff_rules import _setup, wire  # noqa: F401 — fixture

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


class _WA:
    sent: list = []
    fail = False

    def __init__(self, *a, **kw):
        pass

    async def _log(self, kind, **kw):
        if _WA.fail and kind != "text":
            raise RuntimeError("refus")
        _WA.sent.append({"kind": kind, **kw})
        return {}

    async def send_text_message(self, to, body):
        return await self._log("text", body=body)

    async def send_buttons(self, to, body, buttons, image_link=None):
        return await self._log("buttons", body=body, buttons=buttons, image=image_link)

    async def send_list(self, to, body, button, rows, section=None):
        return await self._log("list", body=body, button=button, rows=rows)

    async def send_product_carousel(self, to, body, cards):
        return await self._log("carousel", body=body, cards=cards)

    async def send_product_card(self, *a, **kw):
        return await self._log("card", **kw)

    async def send_image_message(self, *a, **kw):
        return await self._log("image", **kw)


@pytest.fixture
def guided_on(monkeypatch, wire):  # noqa: F811
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)
    _WA.sent, _WA.fail = [], False
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _WA)
    return wire


def _text(pnid, sender, text):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [
        {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


def _tap(pnid, sender, reply_id, title, kind="button_reply"):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [
        {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "type": "interactive", "timestamp": "1",
         "interactive": {"type": kind, kind: {"id": reply_id, "title": title}}}]}}]}]}


async def _shop(db, email, pnid, with_categories=True):
    tenant = await _setup(db, email, pnid)
    robes, sacs = Category(tenant_id=tenant.id, name="Robes"), Category(tenant_id=tenant.id, name="Sacs")
    db.add_all([robes, sacs])
    await db.flush()
    items = [
        Product(tenant_id=tenant.id, sku="R1", name="Robe wax Awa", price=15000, currency="XOF", stock_quantity=3,
                image_url="https://cdn.example.com/r1.jpg", category_id=robes.id if with_categories else None,
                sheet={"compare_at_price": 20000}, description="Coton wax, coupe longue.",
                variants=[{"options": {"Taille": "38"}, "stock": 3, "price": 15000},
                          {"options": {"Taille": "40"}, "stock": 0, "price": 15000},
                          {"options": {"Taille": "42"}, "stock": 2, "price": 16500}]),
        Product(tenant_id=tenant.id, sku="R2", name="Robe Kente", price=18000, currency="XOF", stock_quantity=2,
                image_url="https://cdn.example.com/r2.jpg", category_id=robes.id if with_categories else None),
        Product(tenant_id=tenant.id, sku="S1", name="Sac cabas", price=9500, currency="XOF", stock_quantity=1,
                category_id=sacs.id if with_categories else None),
        Product(tenant_id=tenant.id, sku="X", name="Épuisé", price=1000, currency="XOF", stock_quantity=0,
                image_url="https://cdn.example.com/x.jpg", category_id=robes.id if with_categories else None),
    ]
    db.add_all(items)
    await db.commit()
    return tenant, robes, items


# --- Quand l'accueil part -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, welcome", [
    ("Bonjour", True), ("bonsoir madame !", True), ("Salut Bob 👋", True), ("menu", True), ("Bjr", True),
    ("Bonjour, vous avez des robes rouges ?", False), ("Je veux la robe wax", False), ("", False),
])
def test_only_a_greeting_opens_the_menu(text, welcome):
    assert gf.is_greeting(text) is welcome


def test_an_ad_click_without_text_opens_the_menu():
    assert gf.wants_welcome("", {"ad_id": "1"}) and not gf.wants_welcome("", None)


@pytest.mark.asyncio
async def test_greeting_gets_the_menu_without_ai(client, db_session, unique_email, guided_on):
    tenant, _, _ = await _shop(db_session, unique_email, "pn-l94-1")
    state = guided_on({"intents": [], "objections": []}, [])  # aucune réponse d'IA prévue : elle ne doit pas servir
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l94-1", "2250700000001", "Bonjour"))
    assert r.json() == {"status": "processed", "guided": True}
    [menu] = _WA.sent
    assert menu["kind"] == "buttons" and "bienvenue chez *Boutique*" in menu["body"]
    assert menu["buttons"] == [("g:cat", "Voir le catalogue"), ("g:off:0", "Voir les offres"), ("g:human", "Parler à la boutique")]
    assert state["llm"].call_count == 0
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal))).first() is None  # ni réponse ni analyse par l'IA
    trace = (await db_session.execute(select(Message).where(Message.sender == MessageSender.AI))).scalar_one()
    assert trace.message_type == "guided" and "[Boutons : Voir le catalogue · Voir les offres · Parler à la boutique]" in trace.content


@pytest.mark.asyncio
async def test_a_real_question_still_goes_to_the_ai(client, db_session, unique_email, guided_on):
    await _shop(db_session, unique_email, "pn-l94-2")
    guided_on({"intents": ["AUTRE"], "objections": []}, [text_response("Oui, nous avons la Robe wax Awa.")])
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l94-2", "2250700000002", "Vous avez des robes ?"))
    assert r.json()["ai_reply"] == "Oui, nous avons la Robe wax Awa."


@pytest.mark.asyncio
async def test_switch_off_and_other_sectors_keep_the_ai(client, db_session, unique_email, guided_on):
    tenant, _, _ = await _shop(db_session, unique_email, "pn-l94-3")
    tenant.guided_flow_enabled = False
    await db_session.commit()
    guided_on({"intents": [], "objections": []}, [text_response("Bonjour ! Que cherchez-vous ?")])
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l94-3", "2250700000003", "Bonjour"))
    assert r.json()["ai_reply"] == "Bonjour ! Que cherchez-vous ?"
    tenant.guided_flow_enabled, tenant.business_type = True, "INSURANCE_BROKER"
    await db_session.commit()
    assert not gf.applies(tenant)  # courtier et agence : lots 97 et 98 (la concession a son parcours depuis le lot 96)
    tenant.business_type = "REAL_ESTATE"
    assert not gf.applies(tenant)


# --- Les écrans ------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catalogue_lists_the_real_rayons(db_session, unique_email):
    tenant, robes, _ = await _shop(db_session, unique_email, "pn-l94-4")
    turn = await gf.catalogue(db_session, tenant, tu=False)
    [action] = turn.actions
    assert action["type"] == "list" and action["button"] == "Choisir un rayon"
    assert action["rows"] == [(f"g:r:{robes.id}:0", "Robes", "2 produits · dès 15 000 XOF"),
                              (action["rows"][1][0], "Sacs", "1 produit · dès 9 500 XOF")]  # l'article épuisé ne compte pas


@pytest.mark.asyncio
async def test_a_shop_without_rayons_goes_straight_to_products(db_session, unique_email):
    tenant, _, _ = await _shop(db_session, unique_email, "pn-l94-5", with_categories=False)
    turn = await gf.catalogue(db_session, tenant, tu=False)
    kinds = [a["type"] for a in turn.actions]
    assert kinds == ["carousel", "list", "buttons"]  # 2 avec photo, 1 sans photo, puis la navigation


@pytest.mark.asyncio
async def test_products_carry_the_exact_price_and_two_buttons(db_session, unique_email):
    tenant, robes, items = await _shop(db_session, unique_email, "pn-l94-6")
    turn = await gf.page(db_session, tenant, False, "r", robes.id, 0)
    carousel = turn.actions[0]
    assert [c["name"] for c in carousel["cards"]] == ["Robe wax Awa", "Robe Kente"] or \
        [c["name"] for c in carousel["cards"]] == ["Robe Kente", "Robe wax Awa"]
    wax = next(c for c in carousel["cards"] if c["name"] == "Robe wax Awa")
    assert wax["body"].startswith("Robe wax Awa\nÀ partir de *15 000 XOF*")
    # Lot 95 : « Ça m'intéresse » est devenu « Ajouter au panier ».
    assert wax["buttons"] == [(f"g:d:{items[0].id}", "Voir détails"), (f"g:a:{items[0].id}", "Ajouter au panier")]
    assert turn.actions[-1]["buttons"] == [("g:cat", "Retour au catalogue")]


@pytest.mark.asyncio
async def test_more_than_ten_products_offers_voir_plus(db_session, unique_email):
    tenant = await _setup(db_session, unique_email, "pn-l94-7")
    db_session.add_all([Product(tenant_id=tenant.id, sku=f"P{i}", name=f"Pagne {i:02d}", price=5000 + i, currency="XOF",
                                stock_quantity=1, image_url=f"https://cdn.example.com/{i}.jpg") for i in range(13)])
    await db_session.commit()
    first = await gf.page(db_session, tenant, False, "all", None, 0)
    assert len(first.actions[0]["cards"]) == 10
    assert first.actions[-1]["body"] == "Il reste 3 produits." and first.actions[-1]["buttons"][0] == ("g:all:1", "Voir plus")
    second = await gf.page(db_session, tenant, False, "all", None, 1)
    assert len(second.actions[0]["cards"]) == 3 and second.actions[-1]["buttons"] == [("g:cat", "Retour au catalogue")]


@pytest.mark.asyncio
async def test_offers_are_the_products_with_a_crossed_price(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l94-8")
    turn = await gf.page(db_session, tenant, False, "off", None, 0)
    assert turn.actions[0]["type"] == "buttons" and turn.actions[0]["product_id"] == str(items[0].id)  # une seule offre : ses détails
    items[0].sheet = {}
    await db_session.commit()
    nothing = await gf.page(db_session, tenant, False, "off", None, 0)
    assert "Pas d'offre en ce moment" in nothing.actions[0]["body"]
    assert [b[1] for b in (await gf.welcome(db_session, tenant, False)).actions[0]["buttons"]] == \
        ["Voir le catalogue", "Parler à la boutique"]  # pas de bouton « offres » sans offre


@pytest.mark.asyncio
async def test_details_and_sizes_in_stock_with_their_price(db_session, unique_email):
    tenant, _, items = await _shop(db_session, unique_email, "pn-l94-9")
    [card] = (await gf.details(db_session, tenant, False, items[0])).actions
    assert card["image"] == "https://cdn.example.com/r1.jpg"
    assert card["body"].splitlines() == ["*Robe wax Awa*", "À partir de *15 000 XOF*", "Coton wax, coupe longue.",
                                         "En stock : Taille 38, Taille 42"]
    assert card["buttons"] == [(f"g:t:{items[0].id}", "Choisir la taille"), ("g:cat", "Retour au catalogue")]
    [sizes] = (await gf.sizes(db_session, tenant, False, items[0])).actions
    assert sizes["rows"] == [(f"g:v:{items[0].id}:0", "Taille 38", "15 000 XOF · 3 en stock"),
                             (f"g:v:{items[0].id}:2", "Taille 42", "16 500 XOF · 2 en stock")]
    # Parcours coupé (réglage global des tests) : Bob reçoit « Ça m'intéresse » et prend la commande.
    assert await gf.variant_text(db_session, tenant.id, f"g:v:{items[0].id}:2") == "Ça m'intéresse : Robe wax Awa (Taille 42)"
    assert await gf.variant_text(db_session, uuid.uuid4(), f"g:v:{items[0].id}:2") is None  # autre boutique
    [plain] = (await gf.details(db_session, tenant, True, items[1])).actions
    assert plain["buttons"][0] == (f"g:a:{items[1].id}", "Ajouter au panier")


# --- Boutons touchés, de bout en bout --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_taps_walk_the_journey_and_skip_the_ai(client, db_session, unique_email, guided_on):
    tenant, robes, items = await _shop(db_session, unique_email, "pn-l94-10")
    guided_on({"intents": [], "objections": []}, [])
    for reply_id, title, kind in ((f"g:cat", "Voir le catalogue", "button_reply"),
                                  (f"g:r:{robes.id}:0", "Robes", "list_reply"),
                                  (f"g:d:{items[0].id}", "Voir détails", "button_reply")):
        r = await client.post("/webhooks/whatsapp", json=_tap("pn-l94-10", "2250700000010", reply_id, title, kind))
        assert r.json()["guided"] is True
    assert [s["kind"] for s in _WA.sent] == ["list", "carousel", "buttons", "buttons"]
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal))).first() is None
    cards = (await db_session.execute(select(Message).where(Message.message_type == "product_cards"))).scalar_one()
    assert cards.content.startswith("🛍 Cartes envoyées : ") and cards.message_metadata["product_ids"]


@pytest.mark.asyncio
async def test_a_size_chosen_leads_to_the_quantities(client, db_session, unique_email, guided_on):
    # Lot 95 : la taille choisie ne part plus à Bob ; le code propose les quantités (panier).
    tenant, _, items = await _shop(db_session, unique_email, "pn-l94-11")
    guided_on({"intents": [], "objections": []}, [])
    r = await client.post("/webhooks/whatsapp", json=_tap("pn-l94-11", "2250700000011", f"g:v:{items[0].id}:2",
                                                           "Taille 42", "list_reply"))
    assert r.json()["guided"] is True
    assert _WA.sent[-1]["kind"] == "list" and _WA.sent[-1]["rows"][0][1:] == ("1", "16 500 XOF")
    incoming = (await db_session.execute(select(Message).where(Message.sender == MessageSender.CUSTOMER))).scalar_one()
    assert incoming.content == "Robe wax Awa (Taille 42)"


@pytest.mark.asyncio
async def test_parler_a_un_conseiller_is_a_real_transfer(client, db_session, unique_email, guided_on):
    await _shop(db_session, unique_email, "pn-l94-12")
    state = guided_on({"intents": [], "objections": []}, [])
    r = await client.post("/webhooks/whatsapp", json=_tap("pn-l94-12", "2250700000012", "g:human", "Parler à un conseiller"))
    assert r.json()["ai_reply"] == "Je préviens un conseiller : il vous répond ici dès que possible."
    conversation = (await db_session.execute(select(Conversation).execution_options(populate_existing=True))).scalar_one()
    assert conversation.status == ConversationStatus.WAITING_HUMAN
    assert any("Le client a demandé un conseiller" in e["body"] for e in state["outbox"])  # commerçant prévenu


@pytest.mark.asyncio
async def test_refused_interactive_messages_fall_back_to_text(client, db_session, unique_email, guided_on):
    await _shop(db_session, unique_email, "pn-l94-13")
    guided_on({"intents": [], "objections": []}, [])
    _WA.fail = True
    await client.post("/webhooks/whatsapp", json=_tap("pn-l94-13", "2250700000013", "g:cat", "Voir le catalogue"))
    [fallback] = _WA.sent
    assert fallback["kind"] == "text" and "• Robes : 2 produits · dès 15 000 XOF" in fallback["body"]


@pytest.mark.asyncio
async def test_unknown_or_foreign_buttons_bring_back_the_menu(db_session, unique_email, monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)
    tenant, _, _ = await _shop(db_session, unique_email, "pn-l94-14")
    other = await _setup(db_session, "autre-l94@x.ci", "pn-l94-15")
    foreign = Product(tenant_id=other.id, sku="F", name="Autre", price=1, currency="XOF", stock_quantity=1)
    db_session.add(foreign)
    await db_session.commit()
    assert (await gf.handle(db_session, tenant, "x", "g:zzz")).actions[0]["buttons"][0] == ("g:cat", "Voir le catalogue")
    turn = await gf.handle(db_session, tenant, "x", f"g:d:{foreign.id}")
    assert turn.actions[0]["type"] == "list"  # jamais le produit d'une autre boutique : retour au catalogue
    assert await gf.handle(db_session, tenant, "Vous livrez ?", None) is None


# --- Réglage ---------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_shop_can_switch_it_off(client, db_session, unique_email):
    await _setup(db_session, unique_email, "pn-l94-16")
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/tenants/me/guided-flow", headers=headers)).json() == {"enabled": True}
    assert (await client.put("/api/v1/tenants/me/guided-flow", json={"enabled": False}, headers=headers)).json() == {"enabled": False}
    assert 'id="guided-flow-card"' in DASH and "function saveGuidedFlow(" in DASH and "loadGuidedFlow();" in DASH



@pytest.mark.asyncio
async def test_whatsapp_payloads_follow_meta_limits(monkeypatch):
    from app.integrations.whatsapp.client import WhatsAppClient

    sent = []

    async def fake_post(self, path, payload):
        sent.append(payload)
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    wa = WhatsAppClient(phone_number_id="pn", system_user_token="t")
    await wa.send_buttons("221", "Corps", [("g:a", "Un titre beaucoup trop long pour WhatsApp"), ("g:b", "B"), ("g:c", "C"), ("g:d", "D")],
                          image_link="https://cdn.example.com/a.jpg")
    await wa.send_list("221", "Corps", "Choisir un rayon trop long", [(f"g:r:{i}", "x" * 30, "y" * 80) for i in range(12)])
    await wa.send_product_carousel("221", "Corps", [{"link": "https://cdn.example.com/a.jpg", "body": "A",
                                                     "buttons": [("g:d:1", "Voir détails"), ("interest:1", "Ça m'intéresse"), ("z", "z")]}])
    buttons, listing, carousel = (p["interactive"] for p in sent)
    assert len(buttons["action"]["buttons"]) == 3 and buttons["action"]["buttons"][0]["reply"]["title"] == "Un titre beaucoup tr"
    assert buttons["header"] == {"type": "image", "image": {"link": "https://cdn.example.com/a.jpg"}}
    rows = listing["action"]["sections"][0]["rows"]
    assert len(rows) == 10 and len(rows[0]["title"]) == 24 and len(rows[0]["description"]) == 72
    assert listing["action"]["button"] == "Choisir un rayon tro"
    assert [b["quick_reply"]["id"] for b in carousel["action"]["cards"][0]["action"]["buttons"]] == ["g:d:1", "interest:1"]


@pytest.mark.asyncio
async def test_a_single_rayon_goes_straight_to_its_products(db_session, unique_email, monkeypatch):
    tenant, robes, items = await _shop(db_session, unique_email, "pn-l94-17")
    items[2].category_id = robes.id  # tout dans le même rayon
    await db_session.commit()
    turn = await gf.catalogue(db_session, tenant, tu=False)
    assert turn.actions[0]["type"] == "carousel"


@pytest.mark.asyncio
async def test_another_button_is_never_taken_for_a_greeting(db_session, unique_email, monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)
    tenant, _, items = await _shop(db_session, unique_email, "pn-l94-18")
    assert await gf.handle(db_session, tenant, "Bonjour", f"interest:{items[0].id}") is None
