"""Lot 75 — réponses courtes et ciblées (incident MS4 Africa du 16/10 : une fiche entière pour « en stock ? »)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents import prompts
from app.agents.prompts import STYLE_RULES, build_system_prompt
from app.integrations.whatsapp.formatting import to_whatsapp
from app.models.tenant import Tenant
from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE

DASHBOARD = Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html"


def _tenant(business_type):
    return Tenant(name="MS4 Africa", country="CI", currency="XOF", email="x@example.com", business_type=business_type)


@pytest.mark.parametrize("business_type", [ONLINE_STORE, CAR_DEALERSHIP, INSURANCE_BROKER])
def test_every_sector_gets_the_style_rules_after_its_own_rules(business_type):
    prompt = build_system_prompt(_tenant(business_type))
    assert STYLE_RULES in prompt
    assert prompt.index("IDENTITÉ") < prompt.index("STYLE DES RÉPONSES")  # les règles du secteur restent devant


def test_a_future_sector_gets_the_style_rules_too(monkeypatch):
    """Lot à venir : immobilier. Le style est ajouté autour de n'importe quelles consignes de secteur."""
    monkeypatch.setattr(prompts, "_sector_prompt", lambda *a, **k: "IDENTITÉ\n\nTu es Bob, agent immobilier.\n")
    assert build_system_prompt(SimpleNamespace(name="Agence")).endswith(STYLE_RULES + "\n")


def test_style_rules_say_what_the_incident_needed():
    rules = STYLE_RULES
    for must in ("UNIQUEMENT à la dernière question", "Trois phrases courtes au maximum", "Ne recopie jamais une fiche",
                 "UNE seule question", "Un emoji au plus", "UNE seule étoile",
                 "Le PRIX seulement si le client le demande"):  # test réel du 16/10 : prix donné sans demande
        assert must in rules


def test_customer_memory_stays_last_for_the_cache():
    prompt = build_system_prompt(_tenant(ONLINE_STORE), [], "\nMÉMOIRE CLIENT\nAwa, cherche une moto\n")
    assert prompt.index("STYLE DES RÉPONSES") < prompt.index("MÉMOIRE CLIENT") and prompt.endswith("une moto\n")


def test_knowledge_base_is_still_given_to_bob():
    entry = SimpleNamespace(category=SimpleNamespace(value="HORAIRES"), title="Horaires", content="10 h – 18 h")
    prompt = build_system_prompt(_tenant(ONLINE_STORE), [entry])
    assert "10 h – 18 h" in prompt and prompt.index("BASE DE CONNAISSANCES") < prompt.index("STYLE DES RÉPONSES")


@pytest.mark.parametrize("markdown, whatsapp", [
    # Incident du 16/10 : gras à l'intérieur d'un titre → étoiles orphelines sur WhatsApp.
    ("### 🏍️ **Motos Électriques Professionnelles**\n1. **InfinX EV125**",
     "*🏍️ Motos Électriques Professionnelles*\n1. *InfinX EV125*"),
    ("**InfinX EV125* • Usage", "*InfinX EV125* • Usage"),
    ("## Nos __offres__", "*Nos offres*"),
])
def test_no_more_stray_double_stars(markdown, whatsapp):
    assert to_whatsapp(markdown) == whatsapp
    assert "**" not in to_whatsapp(markdown)


def test_dashboard_shows_line_breaks_like_the_phone():
    html = DASHBOARD.read_text(encoding="utf-8")
    rule = html.split("  .msg {", 1)[1].split("}", 1)[0]
    assert "white-space: pre-line" in rule


# --- Incident du 15/10 : « Vous avez des photos ? » → 8 appels à l'IA puis transfert « IA bloquée » ----------------

@pytest.mark.asyncio
async def test_after_the_tool_turns_bob_must_answer_with_what_he_knows(db_session, unique_email):
    from app.agents.orchestrator import FINAL_ANSWER_INSTRUCTION, generate_ai_reply_detailed
    from app.core.config import get_settings
    from app.tests.fakes import FakeLLMClient, text_response, tool_use_response
    from app.tests.test_product_images import _shop

    tenant, conversation = await _shop(db_session, unique_email)
    turns = get_settings().max_tool_iterations
    llm = FakeLLMClient([tool_use_response("search_products", {"query": "photo"}, tool_use_id=f"t{i}") for i in range(turns)]
                        + [text_response("Je n'ai pas de photo de ce modèle ici. Il vous intéresse pour quel usage ?")])
    reply, failure = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                      incoming_text="Vous avez des photos ?", llm_client=llm, image_outbox=[])
    assert failure is None and reply.startswith("Je n'ai pas de photo")
    assert llm.call_count == turns + 1 and FINAL_ANSWER_INSTRUCTION in llm.received_systems[-1]
    assert FINAL_ANSWER_INSTRUCTION not in llm.received_systems[-2]


@pytest.mark.asyncio
async def test_last_answer_of_a_broker_still_never_gives_an_amount(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.core.config import get_settings
    from app.services import insurance as insurance_service
    from app.tests.fakes import FakeLLMClient, text_response, tool_use_response
    from app.tests.test_product_images import _shop

    tenant, conversation = await _shop(db_session, unique_email)
    tenant.business_type = INSURANCE_BROKER
    turns = get_settings().max_tool_iterations
    llm = FakeLLMClient([tool_use_response("search_products", {"query": "auto"}, tool_use_id=f"t{i}") for i in range(turns)]
                        + [text_response("L'assurance auto coûte 85 000 FCFA par an.")])
    reply, failure = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                      incoming_text="Combien ?", llm_client=llm, image_outbox=[])
    assert failure is None and reply == insurance_service.AMOUNT_FALLBACK


# --- Cohérence (test réel du 16/10, Boutique Demo) ------------------------------------------------------------------

from app.services.reply_cleanup import clean_ai_reply  # noqa: E402


@pytest.mark.parametrize("raw, clean", [
    ("Voici la photo de la *Chemise homme jaune* :\n\n📸 *[Photo envoyée séparément]*\n\nVous la voulez ?",
     "Voici la photo de la *Chemise homme jaune* :\n\nVous la voulez ?"),
    ("Voici la photo [image envoyée juste après] du sac.", "Voici la photo du sac."),
    ("Bonjour ! 😊 Super 👍🏽 choix 🇸🇳 !", "Bonjour ! 😊 Super choix !"),
    ("• *Sac* – 25 000 XOF\n  • cuir", "• *Sac* – 25 000 XOF\n  • cuir"),
    ("Total : 2 [options] possibles", "Total : 2 [options] possibles"),
])
def test_ai_reply_cleanup(raw, clean):
    assert clean_ai_reply(raw) == clean


def test_order_needs_a_recap_and_a_clear_yes():
    from app.agents.prompts import BASE_RULES
    assert "récapitule la commande (produit, quantité, total)" in BASE_RULES
    assert "« Ça m'intéresse »" in BASE_RULES and "N'écris jamais « dites simplement Oui »" in BASE_RULES
    assert "payment_link_sent = true" in BASE_RULES


def test_photos_only_when_asked_and_no_placeholder():
    assert "Ne parle des photos que si le client en demande" in STYLE_RULES


@pytest.mark.asyncio
async def test_catalog_overview_is_stable_grouped_and_without_prices(db_session, unique_email):
    from app.models.category import Category
    from app.models.product import Product
    from app.services.catalog_overview import catalog_overview
    from app.tests.test_product_images import _shop

    tenant, _ = await _shop(db_session, unique_email)
    shirts = Category(tenant_id=tenant.id, name="Chemises")
    bags = Category(tenant_id=tenant.id, name="Sacs")
    db_session.add_all([shirts, bags])
    await db_session.flush()

    def p(sku, name, cat, stock=3, active=True):
        return Product(tenant_id=tenant.id, sku=sku, name=name, price=15000, currency="XOF", stock_quantity=stock,
                       category_id=cat.id if cat else None, active=active)
    db_session.add_all([p("1", "Chemise jaune", shirts), p("2", "Chemise rayée", shirts), p("3", "Sac à main", bags),
                        p("4", "Sac épuisé", bags, stock=0), p("5", "Sac caché", bags, active=False),
                        p("6", "Ceinture", None)])
    await db_session.commit()

    first = await catalog_overview(db_session, tenant.id)
    assert first == await catalog_overview(db_session, tenant.id)  # même question, même réponse
    assert "- Chemises (2 disponibles) : Chemise jaune, Chemise rayée" in first
    assert "- Sacs (1 disponible) : Sac à main" in first and "épuisé" not in first and "caché" not in first
    assert first.index("Sacs") < first.index("Autres produits")  # « Autres » toujours en dernier
    assert "15" not in first and "XOF" not in first  # jamais de prix


@pytest.mark.asyncio
async def test_catalog_overview_goes_before_style_and_memory(db_session, unique_email):
    from app.services.catalog_overview import catalog_overview
    from app.tests.test_product_images import _product, _shop

    tenant, _ = await _shop(db_session, unique_email)
    assert await catalog_overview(db_session, tenant.id) == ""  # catalogue vide : rien
    db_session.add(_product(tenant, "Z", name="Moto InfinX"))
    await db_session.commit()
    catalog = await catalog_overview(db_session, tenant.id)
    prompt = build_system_prompt(tenant, [], "\nMÉMOIRE CLIENT\nAwa\n", catalog=catalog)
    assert prompt.index("CATALOGUE EN BREF") < prompt.index("STYLE DES RÉPONSES") < prompt.index("MÉMOIRE CLIENT")
    assert "- Moto InfinX" in prompt


@pytest.mark.asyncio
async def test_order_result_tells_bob_whether_a_payment_link_exists(db_session, unique_email):
    from app.agents.tools import ToolExecutor
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    product = _product(tenant, "BAG", name="Sac à main dame")
    other = _product(tenant, "BAG2", name="Sac de voyage")
    db_session.add_all([product, other])
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    result = await executor.execute("create_order", {"items": [{"product_id": str(product.id), "quantity": 1}]})
    assert result["payment_link_sent"] is False and "Ne parle pas de lien de paiement" in result["payment_note"]

    tenant.payment_link = "https://pay.example/boutique"
    await db_session.commit()
    result = await executor.execute("create_order", {"items": [{"product_id": str(other.id), "quantity": 1}]})
    assert result.get("payment_link_sent") is True, result
    assert result["payment_link_sent"] is True and "payment_note" not in result


@pytest.mark.asyncio
async def test_webhook_cleans_the_ai_text_and_bob_sees_the_catalog(client, db_session, unique_email):
    from app.agents.dependency import get_llm_client
    from app.main import app
    from app.models.product import Product
    from app.tests.fakes import FakeLLMClient, text_response
    from app.tests.test_freemium_webhook import _payload, _setup

    tenant = await _setup(db_session, unique_email, "phone_lot75_clean", is_paid=True)
    db_session.add(Product(tenant_id=tenant.id, sku="M1", name="Moto InfinX", price=900000, currency="XOF", stock_quantity=2))
    await db_session.commit()
    fake = FakeLLMClient([text_response("Voici la photo 😊 :\n\n📸 *[Photo envoyée séparément]*\n\nElle vous plaît ? 👍")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        reply = (await client.post("/webhooks/whatsapp", json=_payload("phone_lot75_clean", "221700007501", "Photo ?"))).json()["ai_reply"]
    finally:
        del app.dependency_overrides[get_llm_client]
    assert reply == "Voici la photo 😊 :\n\nElle vous plaît ?"
    assert "CATALOGUE EN BREF" in fake.received_systems[0] and "Moto InfinX" in fake.received_systems[0]


# --- Test réel du 16/10, 13:49 : « Okay » après la commande → 2e commande identique, photo envoyée 3 fois ---------

@pytest.mark.asyncio
async def test_same_order_twice_is_refused_but_a_different_one_goes(db_session, unique_email):
    from sqlalchemy import func, select

    from app.agents.tools import ToolExecutor
    from app.models.order import Order
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    bag, belt = _product(tenant, "BAG", name="Sac à main dame"), _product(tenant, "BELT", name="Ceinture")
    bag.stock_quantity = belt.stock_quantity = 5
    db_session.add_all([bag, belt])
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    first = await executor.execute("create_order", {"items": [{"product_id": str(bag.id), "quantity": 1}]})
    again = await executor.execute("create_order", {"items": [{"product_id": str(bag.id), "quantity": 1}]})
    assert again["status"] == "already_ordered" and again["order_id"] == first["order_id"]
    assert "ne la recrée pas" in again["instruction"]
    two = await executor.execute("create_order", {"items": [{"product_id": str(bag.id), "quantity": 2}]})
    other = await executor.execute("create_order", {"items": [{"product_id": str(belt.id), "quantity": 1}]})
    assert "order_id" in two and two.get("status") != "already_ordered" and other.get("status") != "already_ordered"
    count = (await db_session.execute(select(func.count()).select_from(Order).where(Order.tenant_id == tenant.id))).scalar()
    assert count == 3


@pytest.mark.asyncio
async def test_a_photo_already_sent_is_not_sent_again_unless_asked(db_session, unique_email):
    from app.agents.tools import ToolExecutor
    from app.models.conversation import Message, MessageSender
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    bag = _product(tenant, "BAG", name="Sac à main dame")
    db_session.add(bag)
    await db_session.flush()
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI, message_type="image",
                           content="📷 Photo envoyée : Sac", message_metadata={"product_id": str(bag.id)}))
    await db_session.commit()

    executor = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    executor.incoming_text = "Okay"
    result = await executor.execute("send_product_images", {"product_ids": [str(bag.id)]})
    assert executor.pending_images == [] and result["already_sent_recently"] == ["Sac à main dame"]

    asked = ToolExecutor(db_session, tenant.id, conversation, business_type=ONLINE_STORE)
    asked.incoming_text = "Vous pouvez renvoyer la photo ?"
    await asked.execute("send_product_images", {"product_ids": [str(bag.id)]})
    assert [img["product_id"] for img in asked.pending_images] == [str(bag.id)]


def test_after_an_order_ok_gets_a_short_answer():
    from app.agents.prompts import BASE_RULES
    assert "ne refais JAMAIS de récapitulatif ni de nouvelle commande" in BASE_RULES
