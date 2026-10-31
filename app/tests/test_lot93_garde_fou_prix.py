"""
Lot 93 — garde-fou des prix (constat du 31/10 : des prix annoncés par Bob ne correspondaient pas au catalogue).

Le code relit chaque montant écrit par l'IA d'une boutique : un montant inconnu demande une correction, puis la
phrase est retirée ; si rien ne reste, les cartes (qui portent maintenant le prix exact de la fiche) partent.
"""
from decimal import Decimal

import pytest

from app.agents.orchestrator import generate_ai_reply_detailed
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.services import price_guard
from app.services.product_cards import card_body, money_text, price_line
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response
from app.tests.test_product_images import _shop

D = Decimal


def _robe(tenant, **kw):
    fields = dict(tenant_id=tenant.id, sku="R1", name="Robe wax Awa", price=15000, currency="XOF", stock_quantity=4,
                  image_url="https://cdn.example.com/r.jpg", active=True)
    fields.update(kw)
    return Product(**fields)


# --- Lecture des montants --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, amounts", [
    ("La robe coûte 15 000 FCFA.", ["15000.00"]),
    ("Prix : 15.000F", ["15000.00"]),
    ("Total : 30 000", ["30000.00"]),
    ("49,90 € seulement", ["49.90"]),
    ("Total 45k FCFA", ["45000.00"]),
    ("Le total de vos 2 articles est 30 000 XOF.", ["30000.00"]),
    ("Rendez-vous le 12 novembre à 10 h, taille 42.", []),
    ("Livraison 1 500 F CFA", ["1500.00"]),
])
def test_amounts_are_read_like_a_customer_reads_them(text, amounts):
    assert [str(v) for v, _ in price_guard.amounts_in(text)] == amounts


@pytest.mark.asyncio
async def test_known_amounts_come_from_the_shop_and_the_conversation(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(_robe(tenant, sheet={"compare_at_price": 20000},
                         variants=[{"options": {"Taille": "46"}, "stock": 1, "price": 17000}]))
    db_session.add(_robe(tenant, sku="OLD", price=99000, active=False))  # inactif : son prix n'existe plus
    db_session.add(Order(tenant_id=tenant.id, customer_id=conversation.customer_id, status=OrderStatus.PENDING,
                         total_amount=31500, currency="XOF"))
    await db_session.commit()
    allowed = await price_guard.known_amounts(db_session, tenant.id, conversation.customer_id,
                                              ["Livraison à Dakar : 1 500 FCFA", "je propose 12 000"])
    for ok in ("15000", "20000", "17000", "30000", "45000", "31500", "1500", "12000", "16500"):  # 16 500 = robe + livraison
        assert D(ok).quantize(D("0.01")) in allowed, ok
    for bad in ("99000", "14000", "15500.5"):
        assert D(bad).quantize(D("0.01")) not in allowed, bad
    assert price_guard.wrong_amounts("La robe est à 14 000 FCFA, livrée.", allowed) == ["14 000 FCFA"]
    assert price_guard.without_wrong_amounts("Bonjour ! La robe est à 14 000 FCFA. Quelle taille ?", allowed) == \
        "Bonjour ! Quelle taille ?"
    assert price_guard.without_wrong_amounts("La robe est à 14 000 FCFA.", allowed) == ""


# --- Dans la réponse de Bob ----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_right_price_goes_through_untouched(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(_robe(tenant))
    await db_session.commit()
    llm = FakeLLMClient([text_response("La *Robe wax Awa* est à 15 000 FCFA. Quelle taille ?")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Prix de la robe ?", llm)
    assert text == "La *Robe wax Awa* est à 15 000 FCFA. Quelle taille ?" and llm.call_count == 1


@pytest.mark.asyncio
async def test_a_wrong_price_is_corrected_by_a_second_try(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    robe = _robe(tenant)
    db_session.add(robe)
    await db_session.commit()
    llm = FakeLLMClient([text_response("La robe est à 12 000 FCFA."),
                         tool_use_response("get_product_price", {"product_id": str(robe.id)}),
                         text_response("La robe est à 15 000 FCFA.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Prix de la robe ?", llm)
    assert text == "La robe est à 15 000 FCFA." and llm.call_count == 3
    assert "« 12 000 FCFA »" in price_guard.correction(["12 000 FCFA"]) and "get_product_price" in price_guard.correction(["x"])


@pytest.mark.asyncio
async def test_a_price_still_wrong_is_removed(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(_robe(tenant))
    await db_session.commit()
    llm = FakeLLMClient([text_response("Bonjour ! La robe est à 12 000 FCFA. Quelle taille ?"),
                         text_response("Bonjour ! La robe est à 13 000 FCFA. Quelle taille ?")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Prix de la robe ?", llm)
    assert text == "Bonjour ! Quelle taille ?"


@pytest.mark.asyncio
async def test_when_nothing_is_left_the_card_gives_the_exact_price(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    robe = _robe(tenant)
    db_session.add(robe)
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([tool_use_response("search_products", {"query": "robe"}),
                         text_response("La Robe wax Awa est à 12 000 FCFA."),
                         text_response("La Robe wax Awa coûte 11 000 FCFA.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Prix de la robe ?", llm,
                                               image_outbox=outbox)
    assert text == price_guard.FALLBACK_WITH_CARDS
    [card] = outbox
    assert card["card"] == "Robe wax Awa\n*15 000 XOF*"


@pytest.mark.asyncio
async def test_without_a_product_bob_asks_which_one(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    llm = FakeLLMClient([text_response("Elle coûte 9 000 FCFA."), text_response("Elle coûte 9 500 FCFA.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Prix ?", llm)
    assert text == price_guard.FALLBACK_NO_PRODUCT


@pytest.mark.asyncio
async def test_the_customer_own_figure_and_a_correction_text_are_not_a_free_pass(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(_robe(tenant))
    await db_session.commit()
    llm = FakeLLMClient([text_response("Je ne peux pas descendre à 10 000 FCFA, désolé.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Je propose 10 000", llm)
    assert text == "Je ne peux pas descendre à 10 000 FCFA, désolé."  # le chiffre du client lui-même
    llm = FakeLLMClient([text_response("Elle est à 12 000 FCFA."), text_response("Elle est à 12 000 FCFA.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Prix ?", llm)
    assert "12 000" not in text  # la consigne de correction ne rend pas le montant faux acceptable


@pytest.mark.asyncio
async def test_other_sectors_keep_their_own_guards(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    tenant.business_type = "CAR_DEALERSHIP"
    await db_session.commit()
    llm = FakeLLMClient([text_response("Ce véhicule est à 12 000 €.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Prix ?", llm)
    assert text == "Ce véhicule est à 12 000 €." and llm.call_count == 1


# --- Le prix sur la carte -------------------------------------------------------------------------------------------

def test_card_price_comes_from_the_product_sheet():
    robe = Product(name="Robe wax Awa", price=15000, currency="XOF", sheet={"compare_at_price": 20000})
    assert price_line(robe) == "*15 000 XOF* ~20 000 XOF~"
    assert card_body(robe, with_price=True) == "Robe wax Awa\n*15 000 XOF* ~20 000 XOF~"
    assert card_body(robe) == "Robe wax Awa"  # sans prix là où il n'en faut pas (agence, courtier)
    sizes = Product(name="Robe", price=15000, currency="XOF",
                    variants=[{"options": {"Taille": "38"}, "stock": 1, "price": 12000},
                              {"options": {"Taille": "46"}, "stock": 0, "price": 9000}])
    assert price_line(sizes) == "À partir de *12 000 XOF*"  # une variante épuisée ne compte pas
    assert money_text(49.9, "eur") == "49,90 EUR" and money_text(15000, "XOF") == "15 000 XOF"
    assert price_line(Product(name="x", price=0, currency="XOF")) is None


@pytest.mark.asyncio
async def test_cards_carry_the_price_for_a_shop_not_for_an_agency(db_session, unique_email):
    from app.agents.tools import ToolExecutor

    tenant, conversation = await _shop(db_session, unique_email)
    robe = _robe(tenant)
    db_session.add(robe)
    await db_session.commit()
    shop = ToolExecutor(db_session, tenant.id, conversation, business_type="ONLINE_STORE")
    await shop.execute("send_product_images", {"product_ids": [str(robe.id)]})
    assert shop.pending_images[0]["card"] == "Robe wax Awa\n*15 000 XOF*"
    agency = ToolExecutor(db_session, tenant.id, conversation, business_type="REAL_ESTATE")
    await agency.execute("send_product_images", {"product_ids": [str(robe.id)]})
    assert "15 000" not in agency.pending_images[0]["card"]


def test_a_lower_old_price_is_never_shown_as_a_discount():
    assert price_line(Product(name="x", price=15000, currency="XOF", sheet={"compare_at_price": 12000})) == "*15 000 XOF*"


def test_a_reply_reduced_to_a_letter_is_dropped():
    allowed = {D("15000.00")}
    assert price_guard.without_wrong_amounts("A. La robe est à 14 000 FCFA.", allowed) == ""


@pytest.mark.asyncio
async def test_amounts_read_by_a_tool_this_turn_are_accepted(db_session, unique_email, monkeypatch):
    from app.agents.tools import ToolExecutor

    tenant, conversation = await _shop(db_session, unique_email)
    original = ToolExecutor.execute

    async def fake_execute(self, name, tool_input):
        if name == "check_order_status":
            return {"status": "PENDING", "total": "13 750 XOF"}  # un montant que seul l'outil connaît
        return await original(self, name, tool_input)

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    llm = FakeLLMClient([tool_use_response("check_order_status", {}),
                         text_response("Votre commande de 13 750 FCFA est en attente.")])
    text, _ = await generate_ai_reply_detailed(db_session, tenant, conversation, [], "Ma commande ?", llm)
    assert text == "Votre commande de 13 750 FCFA est en attente." and llm.call_count == 2
