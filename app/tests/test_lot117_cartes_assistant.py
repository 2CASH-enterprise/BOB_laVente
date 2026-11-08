"""
Lot 117 — cartes en image et menu en liste de l'assistant (contrat Bob ↔ Naomy 1.3.3, lot 6e de Naomy en production le
08/11) : Mes ventes, Ventes à confirmer, Commandes du jour, Stock, Qui attend partent avec `image_png` (texte complet
dessous, c'est le repli ; boutons d'action gardés) ; le menu part en une seule `liste`. Interrupteur ASSISTANT_CARDS
(éteint par défaut, essai sur une boutique). Jamais de numéro de client entier sur une carte ; l'image n'est pas
gardée une fois le message traité.
"""
import base64
import io
import json

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.models.naomy import AssistantOutbox
from app.models.order import OrderStatus
from app.models.product import Product
from app.services import assistant_cards as cards
from app.services import merchant_assistant as ma
from app.tests.test_lot89_connecteur_naomy import naomy_env  # noqa: F401 — fixture
from app.tests.test_lot92_assistant import NOW, Sent, _linked, _msg, _order, _replies, _say, assistant_env  # noqa: F401


def _png(payload) -> Image.Image:
    raw = base64.b64decode(payload["image_png"])
    image = Image.open(io.BytesIO(raw))
    assert image.format == "PNG" and image.width == 1080 and 300 < image.height <= 1920 and len(raw) <= 5 * 1024 * 1024
    return image


@pytest.fixture
def cards_for(monkeypatch):
    def turn_on(value):
        monkeypatch.setattr(get_settings(), "assistant_cards", value)
    return turn_on


def test_the_switch_and_the_names_on_a_card(monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_cards", "")
    assert not cards.cards_on("abc")
    monkeypatch.setattr(get_settings(), "assistant_cards", "on")
    assert cards.cards_on("abc")
    monkeypatch.setattr(get_settings(), "assistant_cards", " AAA-1 , bbb-2 ")
    assert cards.cards_on("aaa-1") and cards.cards_on("bbb-2") and not cards.cards_on("ccc")
    assert cards.person("Aïcha Traoré") == "Aïcha T." and cards.person("Aïcha") == "Aïcha"
    assert cards.person("Client •• 0099") == "Client •• 0099"
    assert cards.person("+225 07 07 00 00 99") == "Client •• 0099"  # jamais un numéro entier
    assert cards.person(None) == "Un client"


def test_the_menu_list_respects_the_contract():
    assert len(ma.MENU_LIST["lignes"]) <= 10 and len(ma.MENU_LIST["bouton"]) <= 20
    ids = [line["id"] for line in ma.MENU_LIST["lignes"]]
    assert len(set(ids)) == len(ids) and all(i.startswith("bob:menu:") and len(i) <= 200 for i in ids)
    assert all(len(line["titre"]) <= 24 and len(line.get("description") or "") <= 72 for line in ma.MENU_LIST["lignes"])
    assert all(i.split(":")[2] in ma.MENU_COMMANDS for i in ids)  # chaque ligne fait ce que fait la commande écrite


@pytest.mark.asyncio
async def test_menu_is_one_list_when_cards_are_on(client, db_session, assistant_env, cards_for):
    tenant, _, headers, _ = await _linked(client, db_session)
    cards_for(str(tenant.id))
    await _say(client, db_session, headers, _msg("menu"))
    [menu] = await _replies(db_session, tenant)  # une seule liste, conforme au contrat 1.3.3
    assert menu.payload["texte"] == ma.MENU_INTRO and menu.payload["liste"]["bouton"] == "Menu de Bob"
    assert "boutons" not in menu.payload and "image_png" not in menu.payload
    assert [c[1]["id_message_bob"] for c in assistant_env.sender.calls] == [menu.message_id]


@pytest.mark.asyncio
async def test_menu_stays_two_messages_when_cards_are_off(client, db_session, assistant_env, cards_for):
    tenant, _, headers, _ = await _linked(client, db_session)
    cards_for("")
    await _say(client, db_session, headers, _msg("menu"))
    first, second = await _replies(db_session, tenant)
    assert first.payload["texte"] == ma.MENU_1[0] and "liste" not in first.payload


@pytest.mark.asyncio
async def test_sales_pending_and_orders_cards(client, db_session, assistant_env, cards_for, monkeypatch):
    tenant, _, headers, _ = await _linked(client, db_session)
    cards_for("on")
    await _order(db_session, tenant, status=OrderStatus.PENDING, number="2250700001111", name="Aïcha")
    sender = Sent(503)  # rien ne part : on lit les messages préparés, image comprise
    monkeypatch.setattr(ma, "http_post", sender)
    for word in ("mes ventes", "ventes à confirmer", "commandes"):
        await _say(client, db_session, headers, _msg(word))
    sales, pending, orders = await _replies(db_session, tenant)
    _png(sales.payload) and _png(pending.payload) and _png(orders.payload)
    assert sales.payload["texte"].startswith("Vos ventes confirmées :")  # le texte complet reste (repli)
    assert pending.payload["texte"].startswith("Ventes à confirmer (1) :")
    assert [b["titre"] for b in pending.payload["boutons"]] == ["Vendu", "Annuler"]  # boutons d'action sous la carte
    assert orders.payload["texte"].startswith("Commandes du jour (1) :") and "À confirmer" in orders.payload["texte"]
    assert [b["id"] for b in orders.payload["boutons"]] == ["bob:menu:a-confirmer"]
    assert "2250700001111" not in json.dumps([sales.payload["texte"], pending.payload["texte"], orders.payload["texte"]])


@pytest.mark.asyncio
async def test_stock_and_waiting_cards_and_the_image_is_not_kept(client, db_session, assistant_env, cards_for):
    tenant, _, headers, _ = await _linked(client, db_session)
    cards_for("on")
    db_session.add_all([Product(tenant_id=tenant.id, sku="A", name="Écharpe soie", price=1, currency="XOF", stock_quantity=0, active=True),
                        Product(tenant_id=tenant.id, sku="B", name="Sac cabas", price=1, currency="XOF", stock_quantity=2, active=True)])
    await db_session.commit()
    await _say(client, db_session, headers, _msg("stock"))
    await _say(client, db_session, headers, _msg("qui attend"))
    sent = [c[1] for c in assistant_env.sender.calls]
    assert all("image_png" in body for body in sent)  # parti chez Naomy avec la carte
    _png(sent[0])
    assert "Épuisés" in sent[0]["texte"] and "Écharpe soie" in sent[0]["texte"]
    rows = (await db_session.execute(select(AssistantOutbox).where(AssistantOutbox.tenant_id == tenant.id)
                                     .execution_options(populate_existing=True))).scalars().all()
    assert rows and all(r.status == "SENT" and "image_png" not in r.payload for r in rows)  # rien n'est gardé


@pytest.mark.asyncio
async def test_a_card_that_fails_leaves_the_text_alone(client, db_session, assistant_env, cards_for, monkeypatch):
    tenant, _, headers, _ = await _linked(client, db_session)
    cards_for("on")

    def broken(*a, **kw):
        raise RuntimeError("police introuvable")

    monkeypatch.setattr(cards, "sales_card", broken)
    await _say(client, db_session, headers, _msg("mes ventes"))
    [reply] = await _replies(db_session, tenant)
    assert "image_png" not in reply.payload and reply.payload["texte"].startswith("Vos ventes confirmées :")


@pytest.mark.asyncio
async def test_queue_never_mixes_a_list_with_buttons_or_an_image(client, db_session, assistant_env):
    tenant, _, _, connection = await _linked(client, db_session)
    item = ma.queue(db_session, connection, "x", [("bob:a", "A")], now=NOW, image=b"\x89PNG", liste=ma.MENU_LIST)
    assert "boutons" not in item.payload and "image_png" not in item.payload and item.payload["liste"]["lignes"]
    big = ma.queue(db_session, connection, "y", now=NOW, image=b"0" * (5 * 1024 * 1024 + 1))
    assert "image_png" not in big.payload  # au-delà de 5 Mo : le texte seul


def test_cards_render_with_long_values_and_many_rows():
    class T:
        name, business_type, poster_accent, country, currency = "Un nom de boutique vraiment très long" * 2, "ONLINE_STORE", "or", "CI", "XOF"

    from datetime import datetime, timezone

    now = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
    summary = {"currency": "XOF", "today": {"sales": 1, "revenue": "1 250 000"}, "week": {"sales": 99, "revenue": "12 345 678"},
               "month": {"sales": 999, "revenue": "123 456 789"}, "top": []}
    for picture in (cards.sales_card(T(), summary, now),
                    cards.pending_card(T(), [{"customer": f"Client {i}", "items": "1 × Robe", "total": "1 000", "currency": "XOF"}
                                             for i in range(30)], now),
                    cards.waiting_card(T(), [], now), cards.stock_card(T(), [], [], now), cards.orders_card(T(), [], now)):
        image = Image.open(io.BytesIO(picture))
        assert image.width == 1080 and image.height <= 1920


def test_orders_words_and_the_stock_card_puts_out_of_stock_first():
    assert ma.match_command("mes commandes")[0] == "orders" and ma.match_command("commandes")[0] == "orders"
    assert ma.match_command("ventes à confirmer")[0] == "to_confirm"

    class T:
        name, business_type, poster_accent, country, currency = "Boutique", "ONLINE_STORE", "or", "CI", "XOF"

    from datetime import datetime, timezone

    now = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
    empty = Image.open(io.BytesIO(cards.stock_card(T(), [], [], now)))
    only_out = Image.open(io.BytesIO(cards.stock_card(T(), ["Robe", "Sac"], [], now)))
    assert only_out.height > empty.height + 100  # la section « Épuisés » est bien dessinée
