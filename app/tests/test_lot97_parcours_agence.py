"""
Lot 97 — parcours à boutons des agences immobilières : projet (acheter, louer, vendre, mettre en location), type de
bien, quartier, budget, cartes avec prix + charges + honoraires toujours ensemble, visite sur créneau libre,
estimation sur place (jamais de chiffre). Même outil de rendez-vous que Bob ; aucun appel d'IA.
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.customer import Customer
from app.models.product import Product
from app.models.prospect_profile import ProspectProfile
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import guided_flow as gf
from app.services import immo_flow as imf
from app.tests.test_booking import ALL_DAY
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot94_parcours_boutons import _WA, _tap, _text, guided_on  # noqa: F401 — fixture

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


def _bien(tenant, name, price, tx="Location", code="APPARTEMENT", district="Cocody", fees="1 mois de loyer", photo=True, **sheet):
    return Product(tenant_id=tenant.id, sku=f"B{uuid.uuid4().hex[:6]}", name=name, price=price, currency="XOF",
                   stock_quantity=1, bob_category=code,
                   image_url=f"https://cdn.example.com/{uuid.uuid4().hex[:6]}.jpg" if photo else None,
                   sheet={"transaction": tx, "city": "Abidjan", "district": district, **({"fees": fees} if fees else {}), **sheet})


async def _agence(db, email, pnid=None, biens=None, activities=None, booking=True):
    tenant = Tenant(name="Agence du Parc", country="CI", currency="XOF", email=email, plan=TenantPlan.PRO,
                    business_type="REAL_ESTATE", real_estate_profile={"activities": activities} if activities else None)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    if pnid:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pnid, system_user_token="t"))
    if booking:
        db.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True, opening_hours=ALL_DAY, slot_minutes=60, capacity=1))
    items = biens(tenant) if biens else [
        _bien(tenant, "Appartement 3 pièces, Riviera 3", 350_000, surface=85, bedrooms=2, floor="2e", charges="25 000",
              fees="1 mois de loyer, à la charge du locataire"),
        _bien(tenant, "Appartement 2 pièces, Angré", 250_000, surface=60, bedrooms=1, charges="comprises", fees="1 mois de loyer"),
        _bien(tenant, "Studio Marcory", 150_000, district="Marcory", fees=None),
        _bien(tenant, "Villa 5 pièces, Bingerville", 95_000_000, tx="Vente", code="MAISON", district="Bingerville",
              fees="5 % à la charge de l'acquéreur", surface=180),
    ]
    db.add_all(items)
    await db.commit()
    return tenant, items


async def _client(db, tenant):
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2250{uuid.uuid4().int % 10**9:09d}")
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


async def _projects(db):
    profile = (await db.execute(select(ProspectProfile))).scalar_one()
    await db.refresh(profile)
    return profile.real_estate["projects"]


# --- Accueil --------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_greeting_offers_the_projects_of_the_agency(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [menu] = (await _go(db_session, tenant, customer, conv, text="Bonsoir")).actions
    assert menu["type"] == "list" and menu["body"].startswith("Bonjour et bienvenue à *Agence du Parc* !")
    assert [r[1] for r in menu["rows"]] == ["Acheter", "Louer", "Vendre ou faire estimer", "Mettre en location",
                                            "Parler à un conseiller"]
    assert await _go(db_session, tenant, customer, conv, text="Bonjour, je cherche un appartement") is None  # → l'IA


@pytest.mark.asyncio
async def test_a_rental_only_agency_shows_only_its_activities(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email, activities=["LOCATION"])
    customer, conv = await _client(db_session, tenant)
    [menu] = (await _go(db_session, tenant, customer, conv, text="Bonjour")).actions
    assert [r[1] for r in menu["rows"]] == ["Louer", "Parler à un conseiller"]
    [again] = (await _go(db_session, tenant, customer, conv, "g:pv")).actions  # ancien bouton : retour à l'accueil
    assert again["rows"] == menu["rows"]


# --- Recherche ------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rent_type_then_district_from_the_listings(db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [places] = (await _go(db_session, tenant, customer, conv, "g:pl")).actions  # un seul type à louer : sauté
    assert places["body"] == "Dans quel quartier ?"
    assert [r[1:] for r in places["rows"]] == [("Cocody", "2 appartements à louer"), ("Marcory", "1 appartement à louer"),
                                               ("Peu importe", "3 appartements à louer")]
    actions = (await _go(db_session, tenant, customer, conv, places["rows"][0][0])).actions
    names = sorted(c["name"] for c in actions[0]["cards"])
    assert names == ["Appartement 2 pièces, Angré", "Appartement 3 pièces, Riviera 3"]
    projects = await _projects(db_session)
    assert projects["LOCATION"]["property_type"] == "APPARTEMENT" and projects["LOCATION"]["zone"] == "Cocody"


@pytest.mark.asyncio
async def test_type_question_lists_only_the_types_available(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email, biens=lambda t: [
        _bien(t, "Appartement A", 300_000), _bien(t, "Appartement B", 320_000, district="Marcory"),
        _bien(t, "Villa C", 900_000, code="MAISON"), _bien(t, "Villa D", 950_000, code="MAISON", district="Marcory")])
    customer, conv = await _client(db_session, tenant)
    [types] = (await _go(db_session, tenant, customer, conv, "g:pl")).actions
    assert types["body"] == "Quel type de bien ?" and _titles(types) == ["Appartement", "Maison ou villa"]
    [places] = (await _go(db_session, tenant, customer, conv, types["buttons"][1][0])).actions
    assert [r[1:] for r in places["rows"]] == [("Cocody", "1 maison à louer"), ("Marcory", "1 maison à louer"),
                                               ("Peu importe", "2 maisons à louer")]
    actions = (await _go(db_session, tenant, customer, conv, places["rows"][2][0])).actions
    assert sorted(c["name"] for c in actions[0]["cards"]) == ["Villa C", "Villa D"]


@pytest.mark.asyncio
async def test_cards_always_show_price_charges_and_fees_together(db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    actions = (await _go(db_session, tenant, customer, conv, "g:il:tL:0")).actions
    cards = {c["name"]: c for c in actions[0]["cards"]}
    # 160 caractères au plus par carte : le détail saute, jamais le prix ni les honoraires (il reste dans « Voir détails »).
    assert cards["Appartement 3 pièces, Riviera 3"]["body"].splitlines() == [
        "Appartement 3 pièces, Riviera 3", "*350 000 XOF* / mois, charges 25 000 XOF",
        "Honoraires : 1 mois de loyer, à la charge du locataire"]
    assert cards["Appartement 2 pièces, Angré"]["body"].splitlines()[2:] == ["*250 000 XOF* / mois, charges comprises",
                                                                            "Honoraires : 1 mois de loyer"]
    # Sans honoraires dans la fiche : jamais de prix.
    assert cards["Studio Marcory"]["body"].splitlines()[-1] == "Prix et honoraires : demandez au conseiller"
    assert "150 000" not in cards["Studio Marcory"]["body"]
    assert cards["Studio Marcory"]["buttons"] == [(f"g:d:{items[2].id}", "Voir détails"),
                                                  (f"g:vi:{items[2].id}", "Demander une visite")]
    assert all(len(c["body"]) <= 160 for c in cards.values())


def test_a_long_card_drops_the_detail_never_the_fees():
    product = Product(name="Appartement de standing 4 pièces avec piscine et vue lagune, Riviera Golf", price=900_000,
                      currency="XOF", bob_category="APPARTEMENT",
                      sheet={"transaction": "Location", "district": "Riviera Golf", "surface": 160, "bedrooms": 3,
                             "fees": "1 mois de loyer à la charge du locataire"})
    body = imf.card_body(product)
    assert len(body) <= 160 and "Honoraires : 1 mois de loyer" in body and "160 m²" not in body
    product.sheet = {**product.sheet, "fees": "x" * 150}
    assert imf.card_body(product).endswith("Prix et honoraires : voir détails")


@pytest.mark.asyncio
async def test_buy_budget_ranges_and_more_than_ten(db_session, unique_email):
    prices = [40, 50, 60, 70, 90, 100, 110, 130, 160, 180, 210, 250]
    tenant, _ = await _agence(db_session, unique_email, biens=lambda t: [
        _bien(t, f"Maison {i}", p * 1_000_000, tx="Vente", code="MAISON", fees="5 %") for i, p in enumerate(prices)])
    customer, conv = await _client(db_session, tenant)
    [budget] = (await _go(db_session, tenant, customer, conv, "g:pa")).actions  # un type, un quartier : sautés
    assert budget["body"] == "Quel budget environ ?"
    assert [r[1:] for r in budget["rows"]] == [("Moins de 90 millions", "4 maisons à vendre"),
                                               ("90 à 200 millions", "6 maisons à vendre"),
                                               ("Plus de 200 millions", "2 maisons à vendre"),
                                               ("Tous les budgets", "12 maisons à vendre")]
    await _go(db_session, tenant, customer, conv, budget["rows"][1][0])
    assert (await _projects(db_session))["ACHAT"]["budget"] == "90 à 200 millions"
    actions = (await _go(db_session, tenant, customer, conv, "g:il:tV:0")).actions
    assert _titles(actions[-1]) == ["Voir plus", "Autre recherche", "Un conseiller"]


@pytest.mark.asyncio
async def test_details_and_foreign_listing(db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email)
    _, foreign = await _agence(db_session, "x" + unique_email)
    customer, conv = await _client(db_session, tenant)
    [card] = (await _go(db_session, tenant, customer, conv, f"g:d:{items[0].id}")).actions
    assert card["body"].splitlines()[:4] == ["*Appartement 3 pièces, Riviera 3*", "85 m² · 2 chambres · 2e étage · Cocody",
                                             "*350 000 XOF* / mois, charges 25 000 XOF",
                                             "Honoraires : 1 mois de loyer, à la charge du locataire"]
    assert _titles(card) == ["Demander une visite", "Voir d'autres biens", "Un conseiller"]
    [back] = (await _go(db_session, tenant, customer, conv, f"g:d:{foreign[0].id}")).actions
    assert back["type"] == "list" and back["rows"][0][1] == "Acheter"


# --- Visite ---------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_visit_on_a_free_slot_is_booked(db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [slots] = (await _go(db_session, tenant, customer, conv, f"g:vi:{items[0].id}")).actions
    assert slots["body"] == "Visite : *Appartement 3 pièces, Riviera 3*. Quand êtes-vous disponible ?"
    turn = await _go(db_session, tenant, customer, conv, slots["rows"][0][0])
    [appointment] = turn.booked
    assert appointment.kind == "VISITE" and appointment.status == "CONFIRMED" and appointment.product_id == items[0].id
    assert turn.actions[0]["body"].startswith("Bonjour ! Votre visite (Appartement 3 pièces, Riviera 3) est confirmée le ")
    assert _titles(turn.actions[1]) == ["Voir d'autres biens", "Un conseiller"]


@pytest.mark.asyncio
async def test_visit_at_another_moment_is_sent_to_an_advisor(db_session, unique_email):
    tenant, items = await _agence(db_session, unique_email, booking=False)
    customer, conv = await _client(db_session, tenant)
    [ask] = (await _go(db_session, tenant, customer, conv, f"g:vi:{items[1].id}")).actions
    assert ask["type"] == "text" and ask["body"].startswith("Visite : *Appartement 2 pièces, Angré*. Quel jour")
    turn = await _go(db_session, tenant, customer, conv, text="samedi matin")
    assert turn.handoff and turn.handoff_done
    assert turn.handoff_text.startswith("Demande de visite envoyée : Appartement 2 pièces, Angré, samedi matin.")


# --- Vendre ou mettre en location -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_journey_never_gives_a_figure(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [types] = (await _go(db_session, tenant, customer, conv, "g:pv")).actions
    assert types["body"] == "Avec plaisir. Quel type de bien ?"
    assert [r[1] for r in types["rows"]] == ["Appartement", "Maison ou villa", "Terrain", "Bureau ou local"]
    [ask] = (await _go(db_session, tenant, customer, conv, "g:ot:V:MAISON")).actions
    assert ask["body"].startswith("Dans quel quartier, et quelle surface environ (et celle du terrain) ?")
    assert await gf.expects_text(db_session, tenant, customer, "Bingerville, 180 m² sur 500 m² de terrain")
    [slots] = (await _go(db_session, tenant, customer, conv, text="Bingerville, 180 m² sur 500 m² de terrain")).actions
    assert slots["body"] == "Merci.\nPour une estimation juste, un conseiller vient voir votre bien. Quand êtes-vous disponible ?"
    project = (await _projects(db_session))["VENTE"]
    assert project["property_type"] == "MAISON" and project["surface"] == 180
    assert project["address"] == "Bingerville, 180 m² sur 500 m² de terrain"
    turn = await _go(db_session, tenant, customer, conv, slots["rows"][0][0])
    [appointment] = turn.booked
    assert appointment.kind == "ESTIMATION"
    assert appointment.notes == "Vente de son bien · Maison · Bingerville, 180 m² sur 500 m² de terrain"
    assert "XOF" not in str(turn.actions) and "valeur" not in str(turn.actions).lower()


@pytest.mark.asyncio
async def test_owner_question_goes_to_the_ai(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    await _go(db_session, tenant, customer, conv, "g:pg")
    await _go(db_session, tenant, customer, conv, "g:ot:G:APPARTEMENT")
    assert await _go(db_session, tenant, customer, conv, text="Combien vous prenez pour la gestion ?") is None
    assert not await gf.expects_text(db_session, tenant, customer, "Cocody, 70 m²")
    assert (await _projects(db_session))["GESTION"]["property_type"] == "APPARTEMENT"


# --- Webhook, réglage, tableau de bord ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whatsapp_visit_alerts_the_agency_without_ai(client, db_session, unique_email, guided_on):
    tenant, items = await _agence(db_session, unique_email, pnid="pn-l97-1")
    state = guided_on({"intents": [], "objections": []}, [])
    number = "2250700009701"
    await client.post("/webhooks/whatsapp", json=_text("pn-l97-1", number, "Bonjour"))
    await client.post("/webhooks/whatsapp", json=_tap("pn-l97-1", number, f"g:vi:{items[0].id}", "Demander une visite"))
    slot_id = _WA.sent[-1]["rows"][0][0]
    r = await client.post("/webhooks/whatsapp", json=_tap("pn-l97-1", number, slot_id, "Mercredi", "list_reply"))
    assert r.json()["guided"] is True
    assert [e["subject"].startswith("Nouveau rendez-vous : +2250700009701") for e in state["outbox"]] == [True]
    from app.models.message_signal import MessageSignal

    assert (await db_session.execute(select(MessageSignal))).first() is None
    assert (await db_session.execute(select(AppointmentRequest))).scalar_one().kind == "VISITE"


@pytest.mark.asyncio
async def test_agency_switch_dashboard_and_fees_alert(client, db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email)
    token = (await client.post("/api/v1/auth/login", data={"username": unique_email, "password": "x"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/tenants/me/guided-flow", headers=headers)).json() == {"enabled": True}
    assert (await client.put("/api/v1/tenants/me/guided-flow", json={"enabled": False}, headers=headers)).json() == {"enabled": False}
    assert "if (isRdvSector()) loadGuidedFlow();" in DASH
    assert "Honoraires à renseigner : sans eux, le prix n'apparaît pas sur WhatsApp" in DASH
    tenant = await db_session.get(Tenant, tenant.id)
    tenant.guided_flow_enabled = False
    assert not gf.applies(tenant)


def test_filters_and_titles():
    filters = {"t": "L", "p": "APPARTEMENT", "q": imf._place_key("Cocody Angré"), "l": 100_000, "h": 300_000}
    assert imf.decode(imf.encode(filters)) == filters and len(f"g:il:{imf.encode(filters)}:12") <= 200
    assert imf._place_key("Cocody") == imf._place_key("  cocody ")
    for title in ("Demander une visite", "Voir d'autres biens", "Autre recherche", "Maison ou villa", "Bureau ou local"):
        assert len(title) <= 20, title
    for row in ("Vendre ou faire estimer", "Mettre en location", "Choisir un quartier", "Parler à un conseiller"):
        assert len(row) <= 24, row
    assert Message and Customer
