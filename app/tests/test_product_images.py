"""
Lot 26c — Bob envoie la vraie photo d'un produit (incident du 29/09 : « il ne peut pas me donner
d'image »). Seulement les photos enregistrées par la boutique, jamais une adresse inventée.
"""
import uuid

import pytest
from sqlalchemy import select

from app.agents.orchestrator import FAILURE_LOOP, generate_ai_reply_detailed
from app.agents.prompts import BASE_RULES, DEALERSHIP_RULES
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor
from app.main import app
from app.models.conversation import Conversation, Message
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE, tools_for
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response
from app.tests.test_handoff_rules import _payload, _setup, wire  # noqa: F401


async def _shop(db_session, email):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO)
    db_session.add(tenant)
    await db_session.flush()
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
    db_session.add(conversation)
    await db_session.commit()
    return tenant, conversation


def _product(tenant, sku, image_url="https://cdn.example.com/p.jpg", active=True, name=None):
    return Product(tenant_id=tenant.id, sku=sku, name=name or f"Produit {sku}", price=15000000, currency="XOF",
                   stock_quantity=1, image_url=image_url, active=active)


def test_the_tool_exists_for_every_business_type():
    for business_type in (ONLINE_STORE, CAR_DEALERSHIP):
        assert "send_product_images" in {t["name"] for t in tools_for(business_type, TOOL_DEFINITIONS)}


def test_rules_forbid_saying_no_photo_without_trying():
    for rules in (BASE_RULES, DEALERSHIP_RULES):
        assert "send_product_images" in rules and "Ne dis jamais que tu ne peux pas envoyer de" in rules


@pytest.mark.asyncio
async def test_only_real_https_photos_of_this_shop_are_queued(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    other, _ = await _shop(db_session, f"other_{unique_email}")
    car = _product(tenant, "A", name="Peugeot 5008")
    no_photo = _product(tenant, "B", image_url=None)
    plain_http = _product(tenant, "C", image_url="http://insecure.example.com/p.jpg")
    hidden = _product(tenant, "D", active=False)
    foreign = _product(other, "E")
    db_session.add_all([car, no_photo, plain_http, hidden, foreign])
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("send_product_images", {"product_ids": [
        str(car.id), str(no_photo.id), str(plain_http.id)]})
    others = await executor.execute("send_product_images", {"product_ids": [str(hidden.id), str(foreign.id), "pas-un-id"]})

    assert result["photos_sent_after_reply"] == ["Peugeot 5008"]
    assert set(result["without_photo"]) == {"Produit B", "Produit C"} and "pas encore de photo" in result["instruction"]
    assert set(others["unknown_products"]) == {str(hidden.id), str(foreign.id), "pas-un-id"}
    assert others["status"] == "no_photo_sent"
    assert executor.pending_images == [{"product_id": str(car.id), "link": "https://cdn.example.com/p.jpg",
                                        "caption": "Peugeot 5008 — 15 000 000 XOF"}]


@pytest.mark.asyncio
async def test_at_most_three_photos_and_no_duplicates(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    products = [_product(tenant, f"P{i}", image_url=f"https://cdn.example.com/{i}.jpg") for i in range(5)]
    db_session.add_all(products)
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation)

    await executor.execute("send_product_images", {"product_ids": [str(products[0].id), str(products[0].id), str(products[1].id)]})
    await executor.execute("send_product_images", {"product_ids": [str(p.id) for p in products[2:]]})

    assert len(executor.pending_images) == 3  # plafond par réponse, même sur plusieurs appels
    assert len({img["product_id"] for img in executor.pending_images}) == 3


@pytest.mark.asyncio
async def test_empty_or_malformed_request_is_refused(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    assert "error" in await executor.execute("send_product_images", {"product_ids": []})
    assert "error" in await executor.execute("send_product_images", {"product_ids": "abc"})


@pytest.mark.asyncio
async def test_no_photo_leaves_when_bob_fails_to_answer(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    car = _product(tenant, "A")
    db_session.add(car)
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([
        tool_use_response("send_product_images", {"product_ids": [str(car.id)]}),
        text_response(""), text_response(""),
    ])

    _, failure = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                  incoming_text="Une photo ?", llm_client=llm, image_outbox=outbox)

    assert failure == FAILURE_LOOP and outbox == []


# --- Webhook : incident rejoué --------------------------------------------------------------------

class _WhatsAppRecorder:
    texts: list = []
    images: list = []
    fail_images = False

    def __init__(self, *a, **kw):
        pass

    async def send_text_message(self, to, body):
        _WhatsAppRecorder.texts.append(body)
        return {}

    async def send_image_message(self, to, link, caption=None):
        if _WhatsAppRecorder.fail_images:
            raise RuntimeError("Meta indisponible")
        _WhatsAppRecorder.images.append({"to": to, "link": link, "caption": caption})
        return {}


@pytest.fixture
def recorder(monkeypatch, wire):
    _WhatsAppRecorder.texts, _WhatsAppRecorder.images, _WhatsAppRecorder.fail_images = [], [], False
    state = {"wire": wire}
    # `wire` remplace le client par un faux silencieux : on met le nôtre par-dessus.
    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _WhatsAppRecorder)
    return state


@pytest.mark.asyncio
async def test_incident_customer_asks_for_a_photo_and_receives_it(client, db_session, unique_email, recorder):
    tenant = await _setup(db_session, unique_email, "pn-img-1")
    car = _product(tenant, "P5008", image_url="https://cdn.example.com/5008.jpg", name="Peugeot 5008")
    db_session.add(car)
    await db_session.commit()
    recorder["wire"]({"intents": ["AUTRE"], "objections": []}, [
        tool_use_response("send_product_images", {"product_ids": [str(car.id)]}),
        text_response("Voici la Peugeot 5008 !"),
    ])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-img-1", "221700000401", "Je peux voir une photo ?"))

    assert r.json()["images_sent"] == 1
    assert _WhatsAppRecorder.texts == ["Voici la Peugeot 5008 !"]
    assert _WhatsAppRecorder.images == [{"to": "221700000401", "link": "https://cdn.example.com/5008.jpg",
                                         "caption": "Peugeot 5008 — 15 000 000 XOF"}]
    stored = (await db_session.execute(select(Message).where(Message.message_type == "image"))).scalar_one()
    assert stored.content == "📷 Photo envoyée : Peugeot 5008 — 15 000 000 XOF"
    assert stored.message_metadata == {"image_url": "https://cdn.example.com/5008.jpg", "product_id": str(car.id)}


@pytest.mark.asyncio
async def test_refused_photo_is_not_recorded_and_the_reply_still_goes(client, db_session, unique_email, recorder):
    tenant = await _setup(db_session, unique_email, "pn-img-2")
    car = _product(tenant, "X")
    db_session.add(car)
    await db_session.commit()
    _WhatsAppRecorder.fail_images = True
    recorder["wire"]({"intents": ["AUTRE"], "objections": []}, [
        tool_use_response("send_product_images", {"product_ids": [str(car.id)]}),
        text_response("Voici la photo."),
    ])

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-img-2", "221700000402", "Photo ?"))

    assert r.status_code == 200 and r.json()["images_sent"] == 0
    assert _WhatsAppRecorder.texts == ["Voici la photo."]
    assert (await db_session.execute(select(Message).where(Message.message_type == "image"))).scalars().all() == []


# --- Lot 26d : Bob savait chercher la 5008, mais ignorait qu'elle avait une photo -------------------

@pytest.mark.asyncio
async def test_bob_is_told_whether_a_photo_exists_never_its_address(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add_all([_product(tenant, "A", name="Peugeot 5008"),
                        _product(tenant, "B", image_url=None, name="Renault Clio"),
                        _product(tenant, "C", image_url="http://x.example.com/c.jpg", name="Dacia Duster")])
    await db_session.commit()
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("search_products", {"query": ""})

    photos = {r["name"]: r["photo"] for r in result["results"]}
    assert photos["Peugeot 5008"].startswith("disponible") and "send_product_images" in photos["Peugeot 5008"]
    assert photos["Renault Clio"] == "aucune" and photos["Dacia Duster"] == "aucune"
    assert "cdn.example.com" not in str(result) and "image_url" not in str(result)
    assert list(executor.products_with_photo.values()) == ["Peugeot 5008"]


@pytest.mark.asyncio
async def test_incident_bob_denies_an_existing_photo_then_sends_it(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    car = _product(tenant, "P5008", name="Peugeot 5008")
    db_session.add(car)
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([
        tool_use_response("search_products", {"query": "5008"}),
        text_response("Je n’ai malheureusement pas de photo disponible pour ce modèle."),
        tool_use_response("send_product_images", {"product_ids": [str(car.id)]}, tool_use_id="t2"),
        text_response("Voici la Peugeot 5008 !"),
    ])

    text, failure = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                     incoming_text="Je veux une photo de la 5008", llm_client=llm,
                                                     image_outbox=outbox)

    assert (text, failure) == ("Voici la Peugeot 5008 !", None)
    assert [img["product_id"] for img in outbox] == [str(car.id)]
    assert "tu as dit qu'il n'y avait pas de photo" in llm.received_systems[2]
    assert f"Peugeot 5008 (product_id {car.id})" in llm.received_systems[2]


@pytest.mark.asyncio
async def test_photo_correction_happens_once_and_never_without_a_real_photo(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add_all([_product(tenant, "A", name="Peugeot 5008"), _product(tenant, "B", image_url=None, name="Clio")])
    await db_session.commit()

    stubborn = FakeLLMClient([
        tool_use_response("search_products", {"query": "5008"}),
        text_response("Pas de photo, désolé."),
        text_response("Toujours pas de photo."),
    ])
    text, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                               incoming_text="Photo ?", llm_client=stubborn, image_outbox=[])
    assert text == "Toujours pas de photo." and stubborn.call_count == 3

    honest = FakeLLMClient([
        tool_use_response("search_products", {"query": "Clio"}),
        text_response("La Clio n'a pas encore de photo."),
    ])
    text, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                               incoming_text="Photo de la Clio ?", llm_client=honest, image_outbox=[])
    assert text == "La Clio n'a pas encore de photo." and honest.call_count == 2


# --- Lot 26e : incident du 29/09, 19 h 36 — Bob recopie « pas de photo » sans rien vérifier ---------

@pytest.mark.asyncio
async def test_incident_bob_repeats_no_photo_from_history_then_checks_and_sends(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    car = _product(tenant, "P5008", name="Peugeot 5008")
    db_session.add(car)
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([
        text_response("Je n’ai malheureusement pas de photo disponible pour ce modèle dans notre système."),
        tool_use_response("search_products", {"query": "5008"}),
        tool_use_response("send_product_images", {"product_ids": [str(car.id)]}, tool_use_id="t2"),
        text_response("Voici la Peugeot 5008 !"),
    ])

    text, failure = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                     incoming_text="Je veux une photo de la 5008", llm_client=llm,
                                                     image_outbox=outbox)

    assert (text, failure) == ("Voici la Peugeot 5008 !", None)
    assert [img["product_id"] for img in outbox] == [str(car.id)]
    assert "sans consulter le catalogue" in llm.received_systems[1]


@pytest.mark.asyncio
async def test_each_photo_correction_happens_at_most_once(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(_product(tenant, "A", name="Peugeot 5008"))
    await db_session.commit()
    llm = FakeLLMClient([
        text_response("Pas de photo."),                        # sans vérifier → consigne 1
        tool_use_response("search_products", {"query": "5008"}),
        text_response("Toujours pas de photo."),               # photo existante niée → consigne 2
        text_response("Vraiment aucune photo."),               # on s'arrête là
    ])
    text, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                               incoming_text="Photo ?", llm_client=llm, image_outbox=[])
    assert text == "Vraiment aucune photo." and llm.call_count == 4


@pytest.mark.asyncio
async def test_a_stubborn_no_photo_without_lookup_is_returned_not_looped(db_session, unique_email):
    tenant, conversation = await _shop(db_session, unique_email)
    llm = FakeLLMClient([text_response("Pas de photo."), text_response("Je n'ai pas de photo, désolé.")])
    text, failure = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                     incoming_text="Photo ?", llm_client=llm, image_outbox=[])
    assert (text, failure) == ("Je n'ai pas de photo, désolé.", None) and llm.call_count == 2
