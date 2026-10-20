"""Lot 76 — cartes produits WhatsApp (choix du 16/10 : sans prix, 3 cartes au plus, un bouton, dès qu'il présente)."""
import uuid

import pytest
from sqlalchemy import select

from app.agents.prompts import BASE_RULES, DEALERSHIP_RULES, STYLE_RULES
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.integrations.whatsapp.client import WhatsAppClient, parse_whatsapp_message
from app.models.conversation import Message, MessageSender
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.services import product_cards
from app.tests.fakes import text_response, tool_use_response
from app.tests.test_handoff_rules import _payload, _setup, wire  # noqa: F401


# --- Le texte de la carte ------------------------------------------------------------------------

def _p(**kw):
    base = {"name": "Robe wax Awa", "price": 35, "currency": "EUR"}
    base.update(kw)
    return Product(**base)


def test_card_says_name_and_one_useful_detail_never_the_price():
    with_variants = _p(variants=[{"options": {"Taille": "38", "Couleur": "Bleu"}, "stock": 2},
                                 {"options": {"Taille": "40", "Couleur": "Bleu"}, "stock": 0},
                                 {"options": {"Taille": "42", "Couleur": "Vert"}, "stock": 1}])
    assert product_cards.card_body(with_variants) == "Robe wax Awa\nTaille : 38, 42"  # seulement ce qui est en stock
    assert product_cards.card_body(_p(bob_category="FASHION", sheet={"colors": "noir", "sizes": "S-L"})) == \
        "Robe wax Awa\nTailles ou pointures : S-L"
    assert product_cards.card_body(_p(vehicle={"year": 2021, "fuel": "DIESEL"})) == "Robe wax Awa\n2021 · Diesel"
    assert product_cards.card_body(_p(sheet={"brand": "Awa"})) == "Robe wax Awa\nMarque : Awa"
    assert product_cards.card_body(_p()) == "Robe wax Awa"
    long = product_cards.card_body(_p(name="x" * 300))
    assert len(long) == product_cards.CARD_BODY_MAX and "35" not in product_cards.card_body(_p())


# --- Ce qui part chez Meta ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_card_and_carousel_payloads(monkeypatch):
    sent = []

    async def fake_post(self, path, payload):
        sent.append(payload)
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    client = WhatsAppClient("pn", "token")
    await client.send_product_card("336", "https://cdn/x.jpg", "Robe\nTaille : M", "interest:1", "Ça m'intéresse")
    card = sent[0]["interactive"]
    assert card["type"] == "button" and card["header"] == {"type": "image", "image": {"link": "https://cdn/x.jpg"}}
    assert card["action"]["buttons"] == [{"type": "reply", "reply": {"id": "interest:1", "title": "Ça m'intéresse"}}]

    await client.send_product_carousel("336", "Touchez « Ça m'intéresse »", [
        {"link": f"https://cdn/{i}.jpg", "body": "y" * 300, "button_id": f"interest:{i}", "button_title": "Ça m'intéresse"}
        for i in range(2)])
    carousel = sent[1]["interactive"]
    assert carousel["type"] == "carousel" and [c["card_index"] for c in carousel["action"]["cards"]] == [0, 1]
    first = carousel["action"]["cards"][0]
    assert len(first["body"]["text"]) == 160 and first["header"]["image"]["link"] == "https://cdn/0.jpg"
    assert first["action"]["buttons"] == [{"type": "quick_reply", "quick_reply": {"id": "interest:0", "title": "Ça m'intéresse"}}]


# --- Ce que le client renvoie ---------------------------------------------------------------------------

def _incoming(msg):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn"}, "messages": [
        {"from": "336", "id": "wamid.x", **msg}]}}]}]}


def test_button_replies_are_read_instead_of_being_empty():
    tapped = parse_whatsapp_message(_incoming({"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": "interest:abc", "title": "Ça m'intéresse"}}}))
    assert tapped["text"] == "Ça m'intéresse" and tapped["reply_id"] == "interest:abc"
    template = parse_whatsapp_message(_incoming({"type": "button", "button": {"text": "Payer ma commande", "payload": "PAY"}}))
    assert template["text"] == "Payer ma commande" and template["reply_id"] == "PAY"
    plain = parse_whatsapp_message(_incoming({"type": "text", "text": {"body": "Bonjour"}}))
    assert plain["text"] == "Bonjour" and plain["reply_id"] is None


@pytest.mark.asyncio
async def test_interest_is_resolved_only_in_this_shop(db_session):
    shop = Tenant(name="A", country="FR", currency="EUR", email="a@l76.fr", plan=TenantPlan.PRO)
    other = Tenant(name="B", country="FR", currency="EUR", email="b@l76.fr", plan=TenantPlan.PRO)
    db_session.add_all([shop, other])
    await db_session.flush()
    mine = Product(tenant_id=shop.id, sku="1", name="Robe wax Awa", price=35, currency="EUR")
    theirs = Product(tenant_id=other.id, sku="1", name="Secret", price=1, currency="EUR")
    db_session.add_all([mine, theirs])
    await db_session.commit()
    assert await product_cards.interest_text(db_session, shop.id, f"interest:{mine.id}") == "Ça m'intéresse : Robe wax Awa"
    assert await product_cards.interest_text(db_session, shop.id, f"interest:{theirs.id}") is None
    assert await product_cards.interest_text(db_session, shop.id, "interest:pas-un-id") is None
    assert await product_cards.interest_text(db_session, shop.id, "PAY") is None


# --- De bout en bout ------------------------------------------------------------------------------------

class _Recorder:
    texts, images, cards, carousels = [], [], [], []
    fail_cards = False

    def __init__(self, *a, **kw):
        pass

    async def send_text_message(self, to, body):
        _Recorder.texts.append(body)
        return {}

    async def send_image_message(self, to, link, caption=None):
        _Recorder.images.append({"link": link, "caption": caption})
        return {}

    async def send_product_card(self, to, link, body, button_id, button_title):
        if _Recorder.fail_cards:
            raise RuntimeError("refus")
        _Recorder.cards.append({"link": link, "body": body, "button_id": button_id})
        return {}

    async def send_product_carousel(self, to, body, cards):
        if _Recorder.fail_cards:
            raise RuntimeError("refus")
        _Recorder.carousels.append({"body": body, "cards": cards})
        return {}


@pytest.fixture
def recorder(monkeypatch, wire):  # noqa: F811
    _Recorder.texts, _Recorder.images, _Recorder.cards, _Recorder.carousels = [], [], [], []
    _Recorder.fail_cards = False
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Recorder)
    return wire


async def _robes(db_session, tenant, n=2):
    products = [Product(tenant_id=tenant.id, sku=f"R{i}", name=f"Robe {i}", price=35, currency="EUR", stock_quantity=2,
                        image_url=f"https://cdn.example.com/r{i}.jpg") for i in range(n)]
    db_session.add_all(products)
    await db_session.commit()
    return products


@pytest.mark.asyncio
async def test_bob_presents_two_products_in_a_carousel_then_reads_the_tap(client, db_session, unique_email, recorder):
    tenant = await _setup(db_session, unique_email, "pn-l76-1")
    robes = await _robes(db_session, tenant)
    state = recorder({"intents": ["AUTRE"], "objections": []}, [
        tool_use_response("send_product_images", {"product_ids": [str(r.id) for r in robes]}),
        text_response("Voici nos robes en stock :"),
        text_response("La Robe 1 est à 35 €. Quelle taille portez-vous ?"),
    ])

    first = await client.post("/webhooks/whatsapp", json=_payload("pn-l76-1", "33612340001", "Vous avez des robes ?"))
    assert first.json()["images_sent"] == 2 and _Recorder.texts == ["Voici nos robes en stock :"]
    carousel = _Recorder.carousels[0]
    assert carousel["body"] == "Touchez « Ça m'intéresse » sous le produit qui vous plaît."
    assert [c["button_id"] for c in carousel["cards"]] == [f"interest:{r.id}" for r in robes]
    assert all("35" not in c["body"] for c in carousel["cards"])  # 1A : jamais de prix sur la carte
    stored = (await db_session.execute(select(Message).where(Message.message_type == "product_cards"))).scalar_one()
    assert stored.content == "🛍 Cartes envoyées : Robe 0, Robe 1"

    tap = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "pn-l76-1"}, "messages": [
        {"from": "33612340001", "id": f"wamid.{uuid.uuid4().hex}", "type": "interactive", "timestamp": "2",
         "interactive": {"type": "button_reply", "button_reply": {"id": f"interest:{robes[1].id}", "title": "Ça m'intéresse"}}}]}}]}]}
    await client.post("/webhooks/whatsapp", json=tap)
    customer_msgs = (await db_session.execute(select(Message.content).where(
        Message.sender == MessageSender.CUSTOMER).order_by(Message.created_at))).scalars().all()
    assert customer_msgs[-1] == "Ça m'intéresse : Robe 1"
    assert state["llm"].received_messages[-1][-1]["content"].endswith("Ça m'intéresse : Robe 1")


@pytest.mark.asyncio
async def test_one_product_is_one_card_and_refused_cards_fall_back_to_photos(client, db_session, unique_email, recorder):
    tenant = await _setup(db_session, unique_email, "pn-l76-2")
    robe, = await _robes(db_session, tenant, n=1)
    recorder({"intents": ["AUTRE"], "objections": []}, [
        tool_use_response("send_product_images", {"product_ids": [str(robe.id)]}), text_response("La voici :"),
        tool_use_response("send_product_images", {"product_ids": [str(robe.id)]}, tool_use_id="t2"), text_response("Encore :"),
    ])
    await client.post("/webhooks/whatsapp", json=_payload("pn-l76-2", "33612340002", "Une photo ?"))
    assert _Recorder.cards == [{"link": robe.image_url, "body": "Robe 0", "button_id": f"interest:{robe.id}"}]

    _Recorder.fail_cards = True
    await client.post("/webhooks/whatsapp", json=_payload("pn-l76-2", "33612340002", "Vous pouvez renvoyer la photo ?"))
    assert _Recorder.images == [{"link": robe.image_url, "caption": "Robe 0"}]  # repli : photo + légende, sans prix
    image = (await db_session.execute(select(Message).where(Message.message_type == "image"))).scalar_one()
    assert image.content == "📷 Photo envoyée : Robe 0"


@pytest.mark.asyncio
async def test_a_product_already_shown_in_cards_is_not_shown_again(db_session, unique_email):
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    robe = _product(tenant, "R", name="Robe")
    db_session.add(robe)
    await db_session.flush()
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI,
                           message_type="product_cards", content="🛍 Cartes envoyées : Robe",
                           message_metadata={"product_ids": [str(robe.id)], "image_urls": [robe.image_url]}))
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation)
    executor.incoming_text = "D'accord"
    await executor.execute("send_product_images", {"product_ids": [str(robe.id)]})
    assert executor.pending_images == []

    # Lot 73 g (retour réel du 19/10, MS4 Africa : la photo repartait à chaque réponse) : une carte déjà montrée
    # ne repart pas dans les 24 h, quelle que soit la question…
    for message in ("C'est combien ?", "Vous livrez à Cocody ?", "Je prends laquelle ?", "Et en rouge ?"):
        again = ToolExecutor(db_session, tenant.id, conversation)
        again.incoming_text = message
        await again.execute("send_product_images", {"product_ids": [str(robe.id)]})
        assert again.pending_images == [], message
    # … sauf si le client demande à la voir.
    for message in ("Je peux voir la robe ?", "Envoyez la photo svp", "Montrez-moi", "Elle ressemble à quoi ?"):
        again = ToolExecutor(db_session, tenant.id, conversation)
        again.incoming_text = message
        await again.execute("send_product_images", {"product_ids": [str(robe.id)]})
        assert [img["product_id"] for img in again.pending_images] == [str(robe.id)], message


def test_bob_is_told_how_cards_work():
    assert "21bis." in BASE_RULES and "« Ça m'intéresse : <produit> »" in BASE_RULES
    assert "sans lister les produits ni\n    leurs prix" in BASE_RULES
    assert "« Ça m'intéresse : <véhicule> »" in DEALERSHIP_RULES
    assert "les cartes produits, elles, n'ont pas besoin d'être annoncées" in STYLE_RULES
    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "send_product_images")
    assert "CARTES" in tool["description"] and "Ça m'intéresse" in tool["description"]


def test_tutoiement_for_the_carousel_intro():
    assert product_cards.carousel_intro(True) == "Touche « Ça m'intéresse » sous le produit qui te plaît."
    assert product_cards.MAX_CARDS == 3 and product_cards.BUTTON_TITLE == "Ça m'intéresse"


@pytest.mark.asyncio
async def test_never_more_than_three_cards():
    _Recorder.carousels, _Recorder.fail_cards = [], False
    items = [{"product_id": str(i), "link": f"https://cdn/{i}.jpg", "name": f"P{i}", "card": f"P{i}"} for i in range(5)]
    mode, sent = await product_cards.send(_Recorder(), "336", items, tu=False)
    assert mode == "cards" and len(sent) == 3 and len(_Recorder.carousels[0]["cards"]) == 3


# --- Test réel du 16/10 (21:18) : « Vous avez des sacs ? » → liste en texte, aucune carte ------------------------

@pytest.mark.asyncio
async def test_products_named_in_the_reply_go_out_as_cards_even_if_bob_forgot_the_tool(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.tests.fakes import FakeLLMClient
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    with_photo = _product(tenant, "S1", name="Sac à main dame")
    no_photo = _product(tenant, "S2", name="Sac à main", image_url=None)
    other = _product(tenant, "S3", name="Sac de sport")
    db_session.add_all([with_photo, no_photo, other])
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([tool_use_response("search_products", {"query": "Sac"}),
                         text_response("Voici nos sacs disponibles :\n• Sac à main dame\n• Sac à main\nC'est pour quelle occasion ?")])
    reply, failure = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                      incoming_text="Vous avez des sacs ?", llm_client=llm, image_outbox=outbox)
    assert failure is None and [i["product_id"] for i in outbox] == [str(with_photo.id)]  # nommé ET avec photo


@pytest.mark.asyncio
async def test_no_automatic_cards_when_nothing_is_named_or_for_a_broker(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.services.business_type import INSURANCE_BROKER
    from app.tests.fakes import FakeLLMClient
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(_product(tenant, "S1", name="Sac à main dame"))
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([tool_use_response("search_products", {"query": "Sac"}), text_response("Quelle couleur préférez-vous ?")])
    await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                     incoming_text="Des sacs ?", llm_client=llm, image_outbox=outbox)
    assert outbox == []
    tenant.business_type = INSURANCE_BROKER
    llm = FakeLLMClient([tool_use_response("search_products", {"query": "Sac"}), text_response("Le Sac à main dame ?")])
    await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                     incoming_text="Des sacs ?", llm_client=llm, image_outbox=outbox)
    assert outbox == []


# --- Test réel du 16/10 (21:32) : « avant le carrousel, il ressort un message de choix avec des prix » -----------

@pytest.mark.parametrize("text, expected", [
    ("Voici nos sacs disponibles en stock :\n\n• Sac à main dame – 220 000 XOF\n• *Sac à main dame*\n1. Sac à main dame\n\n"
     "C'est pour quelle occasion ?", "Voici nos sacs disponibles en stock :\n\nC'est pour quelle occasion ?"),
    ("• Sac à main dame", "Voici ce que nous avons :"),
    ("Voici la Peugeot 5008 !", "Voici la Peugeot 5008 !"),  # une phrase sans prix reste
    ("Elle est à 35 € et existe en M.\nQuelle taille ?", "Quelle taille ?"),
])
def test_text_before_cards_keeps_intro_and_question_only(text, expected):
    assert product_cards.text_for_cards(text, ["Sac à main dame", "Peugeot 5008"]) == expected


@pytest.mark.asyncio
async def test_bob_text_loses_the_priced_list_when_cards_go(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.tests.fakes import FakeLLMClient
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    bag = _product(tenant, "S1", name="Sac à main dame")
    db_session.add(bag)
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([tool_use_response("send_product_images", {"product_ids": [str(bag.id)]}),
                         text_response("Voici nos sacs :\n• Sac à main dame – 220 000 XOF\nC'est pour quelle occasion ?")])
    reply, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                incoming_text="Des sacs ?", llm_client=llm, image_outbox=outbox)
    assert reply == "Voici nos sacs :\nC'est pour quelle occasion ?" and len(outbox) == 1


@pytest.mark.parametrize("message, ack", [
    ("Vous avez des sacs ?", False), ("Je veux un sac", False), ("Je peux voir ?", False),
    ("Montrez-moi les modèles", False), ("Qu'est-ce que vous avez en stock ?", False),
    ("Okay", True), ("Merci", True), ("D'accord", True), ("oui !", True), ("Super 👍", False),
])
def test_only_a_simple_acknowledgement_keeps_a_card_from_being_shown_again(message, ack):
    """Tests réels du 16/10 (21:43, 22:19) : une nouvelle demande 10 minutes plus tard → plus de carrousel."""
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.incoming_text = message
    assert executor.is_acknowledgement() is ack


@pytest.mark.parametrize("incoming, kept", [
    ("Vous avez des sacs ?", False), ("Bonjour", False),
    ("C'est combien ?", True), ("Ça m'intéresse : Sac à main dame", True), ("Je le prends", True),
    ("Oui je valide", True), ("Quel est le prix ?", True), ("Je veux un sac", False),
])
def test_prices_in_lists_only_when_the_client_talks_price_or_buys(incoming, kept):
    text = "Voici nos sacs :\n• Sac à main dame – 220 000 XOF\n• *Sac en cuir* (45 000 FCFA)\nQuelle couleur ?"
    out = product_cards.without_unasked_prices(text, incoming)
    assert (out == text) is kept
    if not kept:
        assert out == "Voici nos sacs :\n• Sac à main dame\n• *Sac en cuir*\nQuelle couleur ?"


@pytest.mark.asyncio
async def test_unasked_prices_are_removed_from_bob_list_even_without_cards(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.tests.fakes import FakeLLMClient
    from app.tests.test_product_images import _shop

    tenant, conversation = await _shop(db_session, unique_email)
    llm = FakeLLMClient([text_response("Voici nos sacs :\n• Sac rouge – 22 000 XOF\nQuelle couleur ?")])
    reply, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                incoming_text="Vous avez des sacs ?", llm_client=llm, image_outbox=[])
    assert reply == "Voici nos sacs :\n• Sac rouge\nQuelle couleur ?"


# --- Test réel du 16/10 (21:55) : « 🛍 Cartes envoyées : … » recopié dans la réponse de Bob ----------------------

def test_dashboard_traces_never_reach_bob_nor_the_client():
    from app.agents.orchestrator import _history_to_anthropic_messages
    from app.services.reply_cleanup import clean_ai_reply

    history = [Message(sender=MessageSender.CUSTOMER, content="Des chemises ?", message_type="text"),
               Message(sender=MessageSender.AI, content="Voici nos chemises :", message_type="text"),
               Message(sender=MessageSender.AI, content="🛍 Cartes envoyées : Chemise jaune", message_type="product_cards"),
               Message(sender=MessageSender.AI, content="📷 Photo envoyée : Chemise jaune", message_type="image")]
    assert [m["content"] for m in _history_to_anthropic_messages(history)] == ["Des chemises ?", "Voici nos chemises :"]
    assert clean_ai_reply("Voici nos chemises :\n\n🛍 Cartes envoyées : *Chemise jaune* et *Chemise rayée*\n\n"
                          "Laquelle vous intéresse ?") == "Voici nos chemises :\n\nLaquelle vous intéresse ?"


# --- Test réel du 16/10 (22:08) : Bob répond d'après l'aperçu du catalogue, sans chercher → aucune carte -------------

@pytest.mark.asyncio
async def test_named_products_go_out_as_cards_even_without_any_search(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.tests.fakes import FakeLLMClient
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    yellow = _product(tenant, "C1", name="Chemise homme jaune")
    striped = _product(tenant, "C2", name="Chemise homme jaune rayé bleue")
    hidden = _product(tenant, "C3", name="Chemise cachée", active=False)
    db_session.add_all([yellow, striped, hidden])
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([text_response("Voici nos chemises disponibles :\n• *Chemise homme jaune*\n"
                                       "• *Chemise homme jaune rayé bleue*\n• Chemise cachée\nLaquelle vous intéresse ?")])
    reply, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                incoming_text="Vous avez des chemises", llm_client=llm, image_outbox=outbox)
    assert {i["product_id"] for i in outbox} == {str(yellow.id), str(striped.id)}  # jamais un produit inactif
    assert reply == "Voici nos chemises disponibles :\n• Chemise cachée\nLaquelle vous intéresse ?"


@pytest.mark.asyncio
async def test_a_dealership_shows_its_vehicles_in_cards_too(db_session, unique_email):
    """Remarque du 16/10 : les cartes valent pour tous les secteurs qui ont des photos (concession comprise)."""
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.tests.fakes import FakeLLMClient
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    tenant.business_type = "CAR_DEALERSHIP"
    suv = _product(tenant, "V1", name="Toyota RAV4")
    db_session.add(suv)
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([text_response("Nous avons le *Toyota RAV4* en ce moment. Vous souhaitez un essai ?")])
    await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                     incoming_text="Je veux un SUV", llm_client=llm, image_outbox=outbox)
    assert [i["product_id"] for i in outbox] == [str(suv.id)]
