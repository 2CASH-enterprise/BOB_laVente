"""
Lot 115 — fin de commande du restaurant en formulaire WhatsApp (mêmes réglages que le lot 114) : « Commander » →
« Finaliser » ouvre un écran (panier, mode, commune + adresse ou table, heure, paiement) ; la réponse est relue et
vérifiée, ce qui manque est redemandé par l'étape habituelle, puis le récapitulatif avec « Valider ma commande ».
"""
import json
import uuid

import pytest
from sqlalchemy import select

from app.models.cart import Cart
from app.models.order import Order
from app.models.whatsapp_flow import WhatsAppFlow
from app.services import guided_flow, resto_flow, wa_flows
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot53_courtier import _conversation
from app.tests.test_lot94_parcours_boutons import guided_on  # noqa: F401 — fixture
from app.tests.test_lot106_restaurant import _resto
from app.tests.test_lot107_parcours_restaurant import _closed_hours, _dish, _guided, _tap  # noqa: F401 (fixture)
from app.tests.test_lot113_position import COCODY, _located
from app.tests.test_lot114_formulaire_reservation import FakeFlows


async def _ready(db, mode="ON", **fields):
    tenant = await _resto(db, wa_flows=mode, payment_link="https://pay.example/awa", **fields)
    for key in ("booking", "order"):
        db.add(WhatsAppFlow(tenant_id=tenant.id, key=key, version=1, status="PUBLISHED", meta_id=f"F-{key}"))
    await db.commit()
    dish = await _dish(db, tenant)
    customer, conversation = await _conversation(db, tenant)
    await _tap(db, tenant, customer, conversation, f"fq:{dish.id}:-:2")
    return tenant, customer, conversation


async def _answer(db, tenant, customer, conversation, **fields):
    cart = (await db.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    answer = {"flow_token": (cart.meta or {}).get("order_flow"), **fields}
    turn = await guided_flow.handle(db, tenant, "🧾", None, None, False, customer, conversation, entry={"flow": answer})
    await db.commit()
    return turn


def test_the_order_form_screen():
    flow = wa_flows.order_flow_json()
    screen = flow["screens"][0]
    assert flow["version"] == "7.0" and screen["id"] == "COMMANDE" and screen["terminal"] is True
    form = screen["layout"]["children"][0]["children"]
    assert [c["type"] for c in form] == ["TextBody", "RadioButtonsGroup", "If", "Dropdown", "Footer"]
    branch = form[2]
    assert branch["condition"] == "${form.mode} == 'l'"
    assert [c["name"] for c in branch["then"]] == ["area", "address", "pay_l"] and branch["then"][1]["required"] is False
    assert branch["else"][0]["type"] == "If" and branch["else"][0]["then"][0]["name"] == "table"
    assert branch["else"][1]["name"] == "pay_o"
    payload = form[4]["on-click-action"]["payload"]
    assert form[4]["label"] == "Continuer" and set(payload) == {"mode", "area", "address", "table", "time", "pay_l", "pay_o"}
    assert set(screen["data"]) == {"intro", "modes", "areas", "times", "pay_l", "pay_o"}
    json.dumps(flow)


@pytest.mark.asyncio
async def test_the_order_form_is_created_with_its_own_name(db_session):
    tenant = await _resto(db_session)
    fake = FakeFlows(result={"id": "ORD1"})
    assert await wa_flows.ensure(db_session, tenant, client=fake, key=wa_flows.ORDER_KEY) == "ORD1"
    name, flow, category = fake.calls[0]
    assert name == "bob_commande_v1" and category == "OTHER" and flow["screens"][0]["id"] == "COMMANDE"
    rows = (await db_session.execute(select(WhatsAppFlow).where(WhatsAppFlow.tenant_id == tenant.id))).scalars().all()
    assert [(r.key, r.status) for r in rows] == [("order", "PUBLISHED")]


@pytest.mark.asyncio
async def test_commander_opens_the_form_with_everything_needed(db_session):
    tenant, customer, conversation = await _ready(db_session)
    form = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    assert form["type"] == "flow" and form["cta"] == "Finaliser" and form["flow_id"] == "F-order" and form["screen"] == "COMMANDE"
    assert "récapitulatif" in form["body"]
    data = form["data"]
    assert "Poulet braisé × 2 — 9 000 XOF" in data["intro"] and "Sous-total : 9 000 XOF" in data["intro"]
    assert data["modes"] == [{"id": "e", "title": "À emporter"}, {"id": "l", "title": "Livraison"}, {"id": "p", "title": "Sur place"}]
    assert data["areas"] == [{"id": "0", "title": "Cocody · 1 000 XOF"}, {"id": "1", "title": "Yopougon · 2 000 XOF"}]
    assert data["times"][0] == {"id": "now", "title": "Dès que possible"}
    assert data["pay_l"] == [{"id": "d", "title": "À la livraison"}, {"id": "k", "title": "Par lien de paiement"}]
    assert data["pay_o"] == [{"id": "r", "title": "Au restaurant"}, {"id": "k", "title": "Par lien de paiement"}]
    assert wa_flows.is_order_token(form["flow_token"])
    fallback = guided_flow.degraded(form)
    assert fallback["type"] == "buttons" and [b[1] for b in fallback["buttons"]] == ["À emporter", "Livraison", "Sur place"]


@pytest.mark.asyncio
async def test_delivery_answer_goes_straight_to_the_recap_then_the_order(db_session):
    tenant, customer, conversation = await _ready(db_session)
    await _tap(db_session, tenant, customer, conversation, "fo")
    turn = await _answer(db_session, tenant, customer, conversation, mode="l", area="1", address="  Siporex,  près de la pharmacie ",
                         time="now", pay_l="d", pay_o="", table="")
    recap = turn.actions[0]
    assert "Livraison (Yopougon) : 2 000 XOF" in recap["body"] and "Adresse : Siporex, près de la pharmacie" in recap["body"]
    assert "Paiement : à la livraison" in recap["body"] and "Total : 11 000 XOF" in recap["body"]
    assert [b[1] for b in recap["buttons"]][0].startswith("Valider")  # rien n'est commandé sans ce dernier appui
    assert (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one_or_none() is None
    await _tap(db_session, tenant, customer, conversation, "fw")
    order = (await db_session.execute(select(Order).where(Order.tenant_id == tenant.id))).scalar_one()
    assert order.delivery_area == "Yopougon" and float(order.delivery_fee) == 2000 and order.payment_method == "À la livraison"


@pytest.mark.asyncio
async def test_an_empty_address_asks_the_position_and_keeps_the_form_choices(db_session):
    tenant, customer, conversation = await _ready(db_session)
    await _tap(db_session, tenant, customer, conversation, "fo")
    ask = (await _answer(db_session, tenant, customer, conversation, mode="l", area="1", address="", time="now", pay_l="k")).actions[0]
    assert ask["type"] == "location"
    recap = (await _located(db_session, tenant, customer, conversation, COCODY)).actions[0]
    # La commune choisie dans le formulaire (Yopougon) est gardée : pas de nouvelle question, ni de paiement redemandé.
    assert "Livraison (Yopougon) : 2 000 XOF" in recap["body"] and "Position partagée" in recap["body"]
    assert "https://pay.example/awa" in recap["body"] or "lien" in recap["body"].lower()


@pytest.mark.asyncio
async def test_pickup_dine_in_and_wrong_answers(db_session):
    tenant, customer, conversation = await _ready(db_session)
    await _tap(db_session, tenant, customer, conversation, "fo")
    recap = (await _answer(db_session, tenant, customer, conversation, mode="p", table=" t7 ", time="now", pay_o="r")).actions[0]
    assert "Sur place, table T7" in recap["body"] and "Paiement : au restaurant" in recap["body"]
    await _tap(db_session, tenant, customer, conversation, "fo")
    no_table = (await _answer(db_session, tenant, customer, conversation, mode="p", table="", time="now", pay_o="r")).actions[0]
    assert "numéro de votre table" in no_table["body"] or no_table["type"] in ("text", "buttons")
    await _tap(db_session, tenant, customer, conversation, "fo")
    no_pay = (await _answer(db_session, tenant, customer, conversation, mode="e", time="now", pay_o="d")).actions[0]
    assert [b[1] for b in no_pay["buttons"]] == ["Payer au retrait", "Payer par lien"]  # « à la livraison » refusé : redemandé
    await _tap(db_session, tenant, customer, conversation, "fo")
    bad_mode = (await _answer(db_session, tenant, customer, conversation, mode="z", time="now")).actions[0]
    assert bad_mode["type"] == "flow"  # le formulaire repart
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    stale = {"flow_token": "bobc:ancien", "mode": "e", "time": "now", "pay_o": "r"}
    turn = await guided_flow.handle(db_session, tenant, "x", None, None, False, customer, conversation, entry={"flow": stale})
    assert turn.actions[0]["type"] == "flow" and cart.meta.get("order_flow") != "bobc:ancien"


@pytest.mark.asyncio
async def test_kitchen_closed_offers_only_later_times(db_session):
    tenant, customer, conversation = await _ready(db_session, kitchen_hours=_closed_hours())
    later = resto_flow.later_times(tenant)
    await _tap(db_session, tenant, customer, conversation, f"ft:{later[1].strftime('%Y%m%d%H%M')}")
    form = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    times = form["data"]["times"]
    assert all(t["id"] != "now" for t in times) and times[0]["id"] == later[1].strftime("%Y%m%d%H%M")
    recap = (await _answer(db_session, tenant, customer, conversation, mode="e", time=times[0]["id"], pay_o="r")).actions[0]
    assert "Pour : " in recap["body"]
    await _tap(db_session, tenant, customer, conversation, "fo")
    closed = (await _answer(db_session, tenant, customer, conversation, mode="e", time="now", pay_o="r")).actions[0]
    assert "La cuisine est fermée" in closed["body"]


@pytest.mark.asyncio
async def test_off_keeps_the_questions(db_session):
    tenant, customer, conversation = await _ready(db_session, mode="OFF")
    ask = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    assert ask["type"] == "buttons" and [b[1] for b in ask["buttons"]] == ["À emporter", "Livraison", "Sur place"]


@pytest.mark.asyncio
async def test_the_webhook_takes_the_order_form_answer(client, db_session, guided_on):  # noqa: F811
    from app.models.conversation import Message
    from app.tests.test_lot94_parcours_boutons import _WA

    tenant, customer, conversation = await _ready(db_session)
    customer.whatsapp_number = "2250102030477"
    await db_session.commit()
    form = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    guided_on({"intents": [], "objections": []}, [])
    response = json.dumps({"flow_token": form["flow_token"], "mode": "e", "time": "now", "pay_o": "r"})
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": tenant.pnid}, "messages": [{
        "from": "2250102030477", "id": f"wamid.{uuid.uuid4().hex}", "type": "interactive", "timestamp": "1",
        "interactive": {"type": "nfm_reply", "nfm_reply": {"name": "flow", "body": "Envoyé", "response_json": response}}}]}}]}]}
    r = await client.post("/webhooks/whatsapp", json=payload)
    assert r.json()["guided"] is True and "Total : 9 000 XOF" in _WA.sent[-1]["body"]
    trace = (await db_session.execute(select(Message).where(Message.tenant_id == tenant.id, Message.message_type == "interactive"))).scalars().all()
    assert any(m.content == "🧾 Formulaire de commande envoyé · à emporter" for m in trace)


@pytest.mark.asyncio
async def test_a_mode_the_restaurant_does_not_offer_is_refused(db_session):
    tenant, customer, conversation = await _ready(db_session, delivery_enabled=False)
    form = (await _tap(db_session, tenant, customer, conversation, "fo")).actions[0]
    assert [m["id"] for m in form["data"]["modes"]] == ["e", "p"]
    again = (await _answer(db_session, tenant, customer, conversation, mode="l", area="0", address="Rue 12", time="now",
                           pay_l="d")).actions[0]
    assert again["type"] == "flow"  # livraison non proposée : le formulaire repart, rien n'est retenu
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    assert cart.fulfillment != "DELIVERY" and cart.address is None
