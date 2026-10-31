"""
Lot 96 — parcours à boutons des concessions : accueil en liste, neuf/occasion, budget calculé depuis le stock,
cartes avec « Réserver un essai », créneaux réellement libres, reprise en 3 questions, financement, rappel.
Le même outil que Bob enregistre le rendez-vous ; jamais de mensualité ni de valeur de reprise ; aucun appel d'IA.
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.cart import Cart
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.product import Product
from app.models.prospect_profile import ProspectProfile
from app.models.tenant import Tenant
from app.services import auto_flow as af
from app.services import guided_flow as gf
from app.services import vehicle as vs
from app.tests.test_booking import ALL_DAY, _dealer
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot94_parcours_boutons import _WA, _tap, _text, guided_on  # noqa: F401 — fixture

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


def _car(tenant, name, price, km, year=2019, photo=True, **vehicle):
    return Product(tenant_id=tenant.id, sku=f"V{uuid.uuid4().hex[:6]}", name=name, price=price, currency="XOF",
                   stock_quantity=1, image_url=f"https://cdn.example.com/{uuid.uuid4().hex[:6]}.jpg" if photo else None,
                   vehicle={"year": year, "mileage_km": km, "fuel": "ESSENCE", "gearbox": "AUTOMATIQUE",
                            "body_type": "SUV", **vehicle})


async def _concession(db, email, pnid=None, cars=None, booking=True):
    tenant = await _dealer(db, email, hours=None if booking else False, phone_number_id=pnid)
    items = cars(tenant) if cars else [
        _car(tenant, "Toyota RAV4", 12_500_000, 68_000),
        _car(tenant, "Hyundai Tucson", 11_900_000, 54_000, year=2020, fuel="DIESEL", gearbox="MANUELLE"),
        _car(tenant, "Kia Sportage neuf", 19_000_000, 10, year=2026),
    ]
    db.add_all(items)
    await db.commit()
    return tenant, items


async def _client(db, tenant):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    return customer, conversation


async def _go(db, tenant, customer, conv, reply_id=None, text=None):
    return await gf.handle(db, tenant, text, reply_id, customer=customer, conversation=conv)


def _titles(action):
    return [b[1] for b in action["buttons"]]


# --- Fiche véhicule : état neuf / occasion --------------------------------------------------------------------------

def test_condition_is_read_or_deduced_from_the_mileage():
    assert vs.normalize_vehicle({"condition": "neuve"}) == ({"condition": "NEUF"}, [])
    assert vs.normalize_vehicle({"condition": "Occasion"}) == ({"condition": "OCCASION"}, [])
    assert vs.normalize_vehicle({"condition": "abîmée"})[1] == ["État inconnu : abîmée (neuf ou occasion)"]
    assert vs.condition_of({"mileage_km": 12}) == "NEUF" and vs.condition_of({"mileage_km": 45_000}) == "OCCASION"
    assert vs.condition_of({"mileage_km": 5, "condition": "OCCASION"}) == "OCCASION"  # la fiche prime
    assert vs.condition_of({}) is None
    assert vs.for_ai({"condition": "NEUF"}) == {"État": "Neuf"}
    assert vs.vehicle_from_csv_row({"Etat": "neuf"}) == {"condition": "neuf"}
    assert 'select("condition", "État' in DASH and '"gearbox", "condition"].forEach' in DASH


# --- Accueil et véhicules -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_greeting_opens_a_list_of_five_choices(db_session, unique_email):
    tenant, _ = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [menu] = (await _go(db_session, tenant, customer, conv, text="Bonjour")).actions
    assert menu["type"] == "list" and menu["button"] == "Choisir"
    assert [r[1] for r in menu["rows"]] == ["Voir les véhicules", "Réserver un essai", "Faire estimer ma voiture",
                                            "Financement", "Parler à un conseiller"]
    assert menu["body"].startswith("Bonjour et bienvenue chez *Auto Plus* !")
    assert await _go(db_session, tenant, customer, conv, text="Bonjour, vous avez des SUV ?") is None  # → l'IA


@pytest.mark.asyncio
async def test_new_or_used_then_cards_with_trial_button(db_session, unique_email):
    tenant, items = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [question] = (await _go(db_session, tenant, customer, conv, "g:veh")).actions
    assert question["body"] == "Neuf ou occasion ?" and _titles(question) == ["Neuf", "Occasion", "Peu importe"]
    actions = (await _go(db_session, tenant, customer, conv, "g:vc:O")).actions
    carousel = actions[0]
    assert [c["name"] for c in carousel["cards"]] == ["Toyota RAV4", "Hyundai Tucson"] or \
        sorted(c["name"] for c in carousel["cards"]) == ["Hyundai Tucson", "Toyota RAV4"]
    rav = next(c for c in carousel["cards"] if c["name"] == "Toyota RAV4")
    assert "12 500 000 XOF" in rav["body"] and "68 000 km" in rav["body"]
    assert rav["buttons"] == [(f"g:d:{items[0].id}", "Voir détails"), (f"g:e:{items[0].id}", "Réserver un essai")]
    assert "mois" not in str(actions).lower()  # jamais de mensualité
    profile = (await db_session.execute(select(ProspectProfile))).scalar_one()
    assert profile.condition == "OCCASION"


@pytest.mark.asyncio
async def test_new_only_shows_new_cars(db_session, unique_email):
    tenant, items = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [card] = (await _go(db_session, tenant, customer, conv, "g:vc:N")).actions
    assert card["type"] == "buttons" and card["product_id"] == str(items[2].id)  # un seul véhicule neuf : ses détails


@pytest.mark.asyncio
async def test_an_empty_budget_range_is_not_offered(db_session, unique_email):
    prices = [5_100_000] * 4 + [5_200_000] * 4 + [30_000_000] * 4
    tenant, _ = await _concession(db_session, unique_email,
                                  cars=lambda t: [_car(t, f"Auto {i}", p, 60_000) for i, p in enumerate(prices)])
    customer, conv = await _client(db_session, tenant)
    [budget] = (await _go(db_session, tenant, customer, conv, "g:veh")).actions
    assert [r[1:] for r in budget["rows"]] == [("Moins de 5,5 millions", "8 véhicules"), ("Plus de 30 millions", "4 véhicules"),
                                               ("Tous les budgets", "12 véhicules")]


@pytest.mark.asyncio
async def test_a_stock_with_only_used_cars_skips_the_question(db_session, unique_email):
    tenant, _ = await _concession(db_session, unique_email, cars=lambda t: [_car(t, "A", 5_000_000, 90_000),
                                                                             _car(t, "B", 6_000_000, 80_000)])
    customer, conv = await _client(db_session, tenant)
    actions = (await _go(db_session, tenant, customer, conv, "g:veh")).actions
    assert actions[0]["type"] == "carousel"


@pytest.mark.asyncio
async def test_budget_ranges_come_from_the_stock(db_session, unique_email):
    prices = [4_000_000, 5_000_000, 6_000_000, 7_000_000, 9_000_000, 10_000_000, 11_000_000, 13_000_000,
              16_000_000, 18_000_000, 21_000_000, 25_000_000]
    tenant, _ = await _concession(db_session, unique_email,
                                  cars=lambda t: [_car(t, f"Auto {i}", p, 60_000) for i, p in enumerate(prices)])
    customer, conv = await _client(db_session, tenant)
    [budget] = (await _go(db_session, tenant, customer, conv, "g:veh")).actions
    assert budget["body"] == "Quel budget environ ?" and budget["button"] == "Choisir un budget"
    assert [r[1:] for r in budget["rows"]] == [("Moins de 9 millions", "4 véhicules"), ("9 à 20 millions", "6 véhicules"),
                                               ("Plus de 20 millions", "2 véhicules"), ("Tous les budgets", "12 véhicules")]
    actions = (await _go(db_session, tenant, customer, conv, budget["rows"][1][0])).actions
    assert len(actions[0]["cards"]) == 6
    assert all(9_000_000 <= int(c["body"].split("*")[1].replace(" ", "").replace("XOF", "")) < 20_000_000
               for c in actions[0]["cards"])
    profile = (await db_session.execute(select(ProspectProfile))).scalar_one()
    assert profile.budget == "9 à 20 millions"


@pytest.mark.asyncio
async def test_more_than_ten_offers_voir_plus_and_refine(db_session, unique_email):
    tenant, _ = await _concession(db_session, unique_email, cars=lambda t: (
        [_car(t, f"SUV {i}", 8_000_000 + i, 60_000) for i in range(8)]
        + [_car(t, f"Berline {i}", 7_000_000 + i, 60_000, body_type="BERLINE", fuel="DIESEL") for i in range(5)]))
    customer, conv = await _client(db_session, tenant)
    actions = (await _go(db_session, tenant, customer, conv, "g:vl:cA:0")).actions
    nav = actions[-1]
    assert _titles(nav) == ["Voir plus", "Affiner", "Un conseiller"] and nav["body"] == "Il reste 3 véhicules."
    [refine] = (await _go(db_session, tenant, customer, conv, "g:af:cA")).actions
    assert [r[1:] for r in refine["rows"]] == [("Berline", "5 véhicules"), ("SUV", "8 véhicules"),
                                               ("Diesel", "5 véhicules"), ("Essence", "8 véhicules")]
    actions = (await _go(db_session, tenant, customer, conv, refine["rows"][0][0])).actions
    assert sorted(c["name"] for c in actions[0]["cards"]) == [f"Berline {i}" for i in range(5)]


@pytest.mark.asyncio
async def test_details_show_the_sheet_and_never_another_dealers_car(db_session, unique_email):
    tenant, items = await _concession(db_session, unique_email)
    other, foreign = await _concession(db_session, "x" + unique_email)
    customer, conv = await _client(db_session, tenant)
    [card] = (await _go(db_session, tenant, customer, conv, f"g:d:{items[0].id}")).actions
    assert card["body"].splitlines()[:3] == ["*Toyota RAV4 (2019)*", "SUV · 2019 · 68 000 km · Essence · Automatique",
                                             "*12 500 000 XOF*"]
    assert _titles(card) == ["Réserver un essai", "Autres véhicules", "Un conseiller"]
    [back] = (await _go(db_session, tenant, customer, conv, f"g:d:{foreign[0].id}")).actions
    assert back["body"] == "Neuf ou occasion ?"


# --- Essai ------------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trial_on_a_free_slot_is_booked_and_confirmed(db_session, unique_email):
    tenant, items = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [slots] = (await _go(db_session, tenant, customer, conv, f"g:e:{items[0].id}")).actions
    assert slots["body"] == "Essai du *Toyota RAV4 (2019)*. Quand êtes-vous disponible ?"
    assert len(slots["rows"]) == 4 and slots["rows"][-1][1] == "Autre moment"
    turn = await _go(db_session, tenant, customer, conv, slots["rows"][0][0])
    [appointment] = turn.booked
    assert appointment.status == "CONFIRMED" and appointment.kind == "ESSAI" and appointment.product_id == items[0].id
    confirmation, after = turn.actions
    assert confirmation["message_type"] == "appointment_confirmed"
    assert confirmation["body"].startswith("Bonjour ! Votre essai (Toyota RAV4) est confirmé le ")
    assert after["body"] == "Autre chose ?" and _titles(after) == ["Estimer ma voiture", "Financement", "Autres véhicules"]
    # Le même créneau touché à nouveau (capacité 1) : refusé, d'autres créneaux libres sont proposés.
    [again] = (await _go(db_session, tenant, customer, conv, slots["rows"][0][0])).actions
    assert again["body"].startswith("Ce créneau vient d'être pris.") and slots["rows"][0][0] not in [r[0] for r in again["rows"]]


@pytest.mark.asyncio
async def test_other_moment_is_written_and_sent_to_an_advisor(db_session, unique_email):
    tenant, items = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [ask] = (await _go(db_session, tenant, customer, conv, f"g:sl:E:{items[0].id}:x")).actions
    assert ask["type"] == "text" and "samedi matin" in ask["body"]
    assert await gf.expects_text(db_session, tenant, customer, "dimanche en fin de journée")
    turn = await _go(db_session, tenant, customer, conv, text="dimanche en fin de journée")
    assert turn.handoff and turn.handoff_done
    assert turn.handoff_text == ("Demande d'essai envoyée : Toyota RAV4 (2019), dimanche en fin de journée. "
                                 "Un conseiller vous confirme l'heure ici.")
    appointment = (await db_session.execute(select(AppointmentRequest))).scalar_one()
    assert appointment.status == "REQUESTED" and appointment.availability == "dimanche en fin de journée"
    assert conv.status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_without_online_booking_the_availability_is_asked(db_session, unique_email):
    tenant, items = await _concession(db_session, unique_email, booking=False)
    customer, conv = await _client(db_session, tenant)
    [which] = (await _go(db_session, tenant, customer, conv, "g:e:-")).actions
    assert _titles(which) == ["Choisir un véhicule", "Pas encore choisi"]
    [ask] = (await _go(db_session, tenant, customer, conv, "g:e:x")).actions
    assert ask["type"] == "text" and ask["body"].startswith("Essai d'un véhicule. Quel jour et à quel moment")


@pytest.mark.asyncio
async def test_a_question_while_waiting_goes_to_the_ai(db_session, unique_email):
    tenant, items = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    await _go(db_session, tenant, customer, conv, f"g:sl:E:{items[0].id}:x")
    assert await _go(db_session, tenant, customer, conv, text="Elle a combien de propriétaires ?") is None
    assert not await gf.expects_text(db_session, tenant, customer, "samedi")  # Bob n'attend plus
    await _go(db_session, tenant, customer, conv, f"g:sl:E:{items[0].id}:x")
    assert not await gf.expects_text(db_session, tenant, customer, "Elle a combien de propriétaires ?")
    await _go(db_session, tenant, customer, conv, f"g:sl:E:{items[0].id}:x")
    cart = (await db_session.execute(select(Cart).where(Cart.customer_id == customer.id))).scalar_one()
    cart.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
    assert not await gf.expects_text(db_session, tenant, customer, "samedi")  # attente de 2 h dépassée


# --- Reprise, financement, conseiller -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trade_in_in_three_questions_without_any_value(db_session, unique_email):
    tenant, _ = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [ask] = (await _go(db_session, tenant, customer, conv, "g:tr")).actions
    assert "marque et le modèle" in ask["body"]
    [years] = (await _go(db_session, tenant, customer, conv, text="Toyota Corolla")).actions
    assert years["body"].startswith("Question 2 sur 3") and years["rows"][2][1] == "2015 à 2018"
    [kms] = (await _go(db_session, tenant, customer, conv, "g:ty:2")).actions
    assert kms["body"].startswith("Question 3 sur 3") and kms["rows"][2][1] == "100 000 à 150 000 km"
    [slots] = (await _go(db_session, tenant, customer, conv, "g:tk:2")).actions
    assert slots["body"] == "Estimation de votre voiture. Quand êtes-vous disponible ?"
    profile = (await db_session.execute(select(ProspectProfile))).scalar_one()
    assert profile.trade_in == "Toyota Corolla · 2015 à 2018 · 100 000 à 150 000 km"
    turn = await _go(db_session, tenant, customer, conv, slots["rows"][0][0])
    [appointment] = turn.booked
    assert appointment.kind == "ESTIMATION_REPRISE" and appointment.trade_in == profile.trade_in
    assert "XOF" not in str(turn.actions) and "valeur" not in str(turn.actions).lower()
    assert _titles(turn.actions[-1]) == ["Voir les véhicules", "Réserver un essai", "Financement"]
    # Le client change d'avis sur l'année : la nouvelle remplace l'ancienne.
    await _go(db_session, tenant, customer, conv, "g:ty:3")
    await db_session.refresh(profile)
    assert profile.trade_in == "Toyota Corolla · 2019 à 2021"
    # « Je ne sais pas » : rien d'inventé.
    await _go(db_session, tenant, customer, conv, "g:tr")
    await _go(db_session, tenant, customer, conv, text="Peugeot 308")
    await _go(db_session, tenant, customer, conv, "g:ty:x")
    await _go(db_session, tenant, customer, conv, "g:tk:x")
    await db_session.refresh(profile)
    assert profile.trade_in == "Peugeot 308"


@pytest.mark.asyncio
async def test_financing_is_noted_and_callback_is_a_transfer(db_session, unique_email):
    tenant, _ = await _concession(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [msg] = (await _go(db_session, tenant, customer, conv, "g:fi")).actions
    assert msg["body"].startswith("Un conseiller vous présente les solutions de financement")
    assert _titles(msg) == ["Être rappelé", "Réserver un essai", "Voir les véhicules"]
    assert (await db_session.execute(select(ProspectProfile))).scalar_one().payment == "FINANCEMENT"
    turn = await _go(db_session, tenant, customer, conv, "g:cb")
    assert turn.handoff and not turn.handoff_done
    assert turn.handoff_reason == "Le client souhaite être rappelé pour un financement"


# --- Par le webhook -------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whatsapp_trial_booking_alerts_the_dealer_without_ai(client, db_session, unique_email, guided_on):
    tenant, items = await _concession(db_session, unique_email, pnid="pn-l96-1")
    state = guided_on({"intents": [], "objections": []}, [])
    number = "2210700009601"
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l96-1", number, "Bonjour"))
    assert r.json()["guided"] is True and _WA.sent[-1]["kind"] == "list"
    r = await client.post("/webhooks/whatsapp", json=_tap("pn-l96-1", number, f"g:e:{items[0].id}", "Réserver un essai"))
    slot_id = _WA.sent[-1]["rows"][0][0]
    r = await client.post("/webhooks/whatsapp", json=_tap("pn-l96-1", number, slot_id, "Lundi", "list_reply"))
    assert r.json()["guided"] is True
    assert [s["kind"] for s in _WA.sent[-2:]] == ["text", "buttons"]
    assert [e["subject"].startswith("Nouveau rendez-vous : +2210700009601") for e in state["outbox"]] == [True]
    trace = (await db_session.execute(select(Message).where(Message.message_type == "appointment_confirmed"))).scalar_one()
    assert "est confirmé" in trace.content
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal))).first() is None


@pytest.mark.asyncio
async def test_whatsapp_written_moment_is_one_transfer_with_details(client, db_session, unique_email, guided_on):
    tenant, items = await _concession(db_session, unique_email, pnid="pn-l96-3")
    state = guided_on({"intents": [], "objections": []}, [])
    number = "2210700009603"
    await client.post("/webhooks/whatsapp", json=_tap("pn-l96-3", number, f"g:sl:E:{items[0].id}:x", "Autre moment", "list_reply"))
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l96-3", number, "dimanche après la messe"))
    assert r.json()["ai_reply"].startswith("Demande d'essai envoyée : Toyota RAV4 (2019), dimanche après la messe.")
    [handoff] = (await db_session.execute(select(Message).where(Message.message_type == "handoff"))).scalars().all()
    assert handoff.content.startswith("Transfert vers un humain : Rendez-vous à confirmer — Essai — Toyota RAV4")
    assert state["outbox"]


@pytest.mark.asyncio
async def test_whatsapp_callback_alerts_with_the_reason(client, db_session, unique_email, guided_on):
    tenant, _ = await _concession(db_session, unique_email, pnid="pn-l96-2")
    state = guided_on({"intents": [], "objections": []}, [])
    r = await client.post("/webhooks/whatsapp", json=_tap("pn-l96-2", "2210700009602", "g:cb", "Être rappelé"))
    assert r.json()["ai_reply"] == "C'est noté : un conseiller vous rappelle sur ce numéro dès que possible."
    handoff = (await db_session.execute(select(Message).where(Message.message_type == "handoff"))).scalar_one()
    assert handoff.content == "Transfert vers un humain : Le client souhaite être rappelé pour un financement"
    assert state["outbox"]  # la concession est prévenue


# --- Réglage, tableau de bord, limites WhatsApp ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dealer_can_switch_it_off_and_card_is_shown(client, db_session, unique_email):
    tenant, _ = await _concession(db_session, unique_email)
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/tenants/me/guided-flow", headers=headers)).json() == {"enabled": True}
    assert (await client.put("/api/v1/tenants/me/guided-flow", json={"enabled": False}, headers=headers)).json() == {"enabled": False}
    tenant = await db_session.get(Tenant, tenant.id)
    await db_session.refresh(tenant)
    assert not gf.applies(tenant)
    assert 'class="card guided-card"' in DASH and "if (isRdvSector()) loadGuidedFlow();" in DASH


def test_amounts_filters_and_titles_fit_whatsapp():
    assert af.amount_label(12_500_000, "XOF") == "12,5 millions" and af.amount_label(1_000_000, "XOF") == "1 million"
    assert af.amount_label(18_000, "EUR") == "18 000 EUR"
    assert af._nice(8_260_000) == 8_500_000 and af._nice(12_300_000) == 15_000_000
    filters = {"c": "O", "l": 8_000_000, "h": 15_000_000, "b": "PICK_UP", "f": "DIESEL", "g": "AUTOMATIQUE"}
    assert af.decode(af.encode(filters)) == filters
    assert len(f"g:vl:{af.encode(filters)}:12") <= 200
    for title in ("Voir les véhicules", "Réserver un essai", "Estimer ma voiture", "Autres véhicules", "Un conseiller",
                  "Être rappelé", "Choisir un véhicule", "Pas encore choisi", "Peu importe", "Voir détails"):
        assert len(title) <= 20, title
    for row in ("Faire estimer ma voiture", "Parler à un conseiller", "Choisir un créneau", "Choisir un budget"):
        assert len(row) <= 24, row
    assert ALL_DAY and TenantAppointmentSettings
