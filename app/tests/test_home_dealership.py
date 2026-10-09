"""Lot 47 — accueil adapté à la concession (rendez-vous et ventes, jamais « encaissé ») et textes moins techniques."""
import uuid
from pathlib import Path

import pytest

from app.models.appointment_request import AppointmentRequest
from app.models.message_signal import MessageSignal
from app.models.order import OrderStatus
from app.services.business_type import CAR_DEALERSHIP
from app.services.home_service import home_summary
from app.tests.test_sales_opportunities import NOW, _headers, _shop, ago

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


async def _dealer(db_session, email="auto@home.sn"):
    shop = await _shop(db_session, email=email)
    shop.tenant.business_type = CAR_DEALERSHIP
    await db_session.commit()
    return shop


def _appointment(shop, customer, conversation, created, outcome=None, outcome_at=None, status="REQUESTED", vehicle="Peugeot 3008"):
    shop.db.add(AppointmentRequest(tenant_id=shop.tenant.id, customer_id=customer.id, conversation_id=conversation.id,
                                   kind="ESSAI", vehicle_label=vehicle, availability="samedi", status=status,
                                   created_at=created, outcome=outcome, outcome_at=outcome_at))


async def summary(shop):
    await shop.compute()
    return await home_summary(shop.db, shop.tenant.id, now=NOW)


async def _scenario(shop):
    """4 conversations actives ce mois-ci (2 avec rendez-vous), 2 le mois d'avant (1 avec rendez-vous)."""
    convs = []
    for i in range(4):
        customer, conv = await shop.customer(number=f"22170000{i:04d}")
        shop.msg(conv, ago(5 + i))
        convs.append((customer, conv))
    _appointment(shop, *convs[0], ago(4), outcome="SOLD", outcome_at=ago(1), status="CONFIRMED")
    _appointment(shop, *convs[1], ago(3), outcome="NO_SHOW", outcome_at=ago(2), status="CONFIRMED")
    _appointment(shop, *convs[1], ago(2))  # même conversation : 2 rendez-vous, 1 conversation
    for i in range(2):
        customer, conv = await shop.customer(number=f"22171000{i:04d}")
        shop.msg(conv, ago(40 + i))
        if i == 0:
            _appointment(shop, customer, conv, ago(39), outcome="SOLD", outcome_at=ago(38), status="CONFIRMED")
    return convs


@pytest.mark.asyncio
async def test_dealership_kpis_are_appointments_and_sales(db_session):
    shop = await _dealer(db_session)
    await _scenario(shop)

    s = await summary(shop)

    assert s["business_type"] == "CAR_DEALERSHIP"
    k = s["kpis"]
    assert "paid" not in k and "pending" not in k and "conversion" not in k
    assert k["appointments"] == {"value": 3, "delta_pct": 200.0, "to_confirm": 1}
    assert k["sold"] == {"value": 1, "delta_pct": 0.0}
    assert k["appointment_rate"] == {"value": 50.0, "delta_points": 0.0, "with_appointment": 2, "conversations": 4}
    assert k["conversations"]["value"] == 4


@pytest.mark.asyncio
async def test_dealership_funnel_goes_to_the_sale(db_session):
    shop = await _dealer(db_session, "f@home.sn")
    await _scenario(shop)

    funnel = (await summary(shop))["funnel"]

    assert [(f["label"], f["value"]) for f in funnel] == [
        ("Conversations", 4), ("Rendez-vous demandés", 3), ("Venus au rendez-vous", 1), ("Vendus", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, visited", [("SOLD", 1), ("FOLLOW_UP", 1), ("NOT_INTERESTED", 1), ("NO_SHOW", 0), (None, 0)])
async def test_who_counts_as_visited(db_session, outcome, visited):
    shop = await _dealer(db_session, f"v{uuid.uuid4().hex[:6]}@home.sn")
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    _appointment(shop, customer, conv, ago(2), outcome=outcome, outcome_at=ago(1) if outcome else None)

    funnel = {f["key"]: f["value"] for f in (await summary(shop))["funnel"]}

    assert funnel["visited"] == visited and funnel["sold"] == (1 if outcome == "SOLD" else 0)


@pytest.mark.asyncio
async def test_no_rate_without_conversation(db_session):
    shop = await _dealer(db_session, "empty@home.sn")
    k = (await summary(shop))["kpis"]
    assert k["appointment_rate"]["value"] is None and k["appointment_rate"]["delta_points"] is None
    assert k["appointments"]["delta_pct"] is None and k["sold"]["delta_pct"] is None


@pytest.mark.asyncio
async def test_days_with_an_appointment_are_marked(db_session):
    shop = await _dealer(db_session, "d@home.sn")
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    _appointment(shop, customer, conv, ago(2))

    daily = (await summary(shop))["daily"]

    marked = [d["date"] for d in daily if d["appointment"]]
    assert marked == [ago(2).date().isoformat()]


@pytest.mark.asyncio
async def test_orders_never_show_on_a_dealership_home(db_session):
    shop = await _dealer(db_session, "o@home.sn")
    customer, conv = await shop.customer()
    shop.msg(conv, ago(10))
    shop.order(customer, conv, ago(10), status=OrderStatus.PENDING)  # vieille commande d'avant le changement d'activité
    shop.order(customer, conv, ago(3), status=OrderStatus.PAID)

    s = await summary(shop)

    assert all(t["kind"] != "ORDER" for t in s["todo"])
    assert all(a["kind"] not in ("ORDER", "PAYMENT") for a in s["activity"])


@pytest.mark.asyncio
async def test_dealership_activity(db_session):
    shop = await _dealer(db_session, "a@home.sn")
    customer, conv = await shop.customer(first_name="Nouro")
    shop.msg(conv, ago(3))
    _appointment(shop, customer, conv, ago(2), outcome="SOLD", outcome_at=ago(1), status="CONFIRMED")
    _appointment(shop, customer, conv, ago(20))  # trop ancien pour l'activité récente
    _appointment(shop, customer, conv, ago(30), outcome="SOLD", outcome_at=ago(20), status="CONFIRMED", vehicle="Ancienne vente")
    await shop.db.flush()
    from app.models.conversation import Message, MessageSender

    message = Message(tenant_id=shop.tenant.id, conversation_id=conv.id, sender=MessageSender.CUSTOMER,
                      message_type="text", content="Elle est vendue ?", created_at=ago(0.5))
    shop.db.add(message)
    await shop.db.flush()
    shop.db.add(MessageSignal(tenant_id=shop.tenant.id, conversation_id=conv.id, message_id=message.id, intents=["AUTRE"],
                              objections=["RUPTURE_STOCK"], model="f", taxonomy_version="v1.3auto",
                              message_created_at=message.created_at))

    activity = (await summary(shop))["activity"]

    kinds = [(a["kind"], a["title"], a["detail"]) for a in activity if a["kind"] in ("APPOINTMENT", "SALE", "OBJECTION")]
    assert kinds == [
        ("OBJECTION", "Objection : véhicule plus disponible", "Détectée par Bob"),
        ("SALE", "Vente conclue", "Nouro · Peugeot 3008"),
        ("APPOINTMENT", "Rendez-vous demandé", "Nouro · Essai · Peugeot 3008"),
    ]


@pytest.mark.asyncio
async def test_other_dealerships_are_never_counted(db_session):
    shop = await _dealer(db_session, "iso1@home.sn")
    other = await _dealer(db_session, "iso2@home.sn")
    customer, conv = await other.customer(number="221799999999")
    other.msg(conv, ago(3))
    _appointment(other, customer, conv, ago(2), outcome="SOLD", outcome_at=ago(1))

    s = await summary(shop)

    assert s["kpis"]["appointments"]["value"] == 0 and s["kpis"]["sold"]["value"] == 0
    assert not any(a["kind"] in ("APPOINTMENT", "SALE") for a in s["activity"])


@pytest.mark.asyncio
async def test_store_home_is_unchanged(db_session):
    shop = await _shop(db_session, "store@home.sn")
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    shop.order(customer, conv, ago(3), status=OrderStatus.PAID, total=5000)

    s = await summary(shop)

    assert s["business_type"] == "ONLINE_STORE"
    assert set(s["kpis"]) == {"paid", "pending", "conversion", "conversations"}
    assert [f["label"] for f in s["funnel"]] == ["Conversations", "Opportunités de vente", "Commandes", "Payées"]
    assert all(d["appointment"] is False for d in s["daily"])


@pytest.mark.asyncio
async def test_home_api_for_a_dealership(client, db_session):
    await _dealer(db_session, "api@home.sn")
    home = (await client.get("/api/v1/analytics/home", headers=await _headers(client, "api@home.sn"))).json()
    assert home["business_type"] == "CAR_DEALERSHIP" and "appointments" in home["kpis"]


# --- Tableau de bord ------------------------------------------------------------------------------------------

def test_dashboard_home_speaks_dealership():
    body = HTML[HTML.index("function renderHome(h) {"):HTML.index("async function loadHome() {")]
    assert 'const dealer = h.business_type === "CAR_DEALERSHIP" || h.business_type === "INSURANCE_BROKER";' in body  # lot 53
    dealer = body[body.index("const kpis = dealer ? ["):body.index("].join(\"\") : [")]
    assert "Rendez-vous obtenus" in dealer and "Véhicules vendus" in dealer and "Taux de rendez-vous" in dealer
    assert "Encaissé" not in dealer and "paiement" not in dealer.lower() and "commande" not in dealer.lower()
    for text in ("Parcours des prospects", "De la première question à la vente", "Jour avec rendez-vous",
                 "demande de rendez-vous sont mis en évidence", 'dealer ? "les rendez-vous" : "les commandes"'):
        assert text in body, text
    assert "APPOINTMENT: [\"calendar\"" in HTML and "SALE: [\"key\"" in HTML


def test_no_more_technical_wording_on_the_dashboard():
    assert "section 56" not in HTML and "Commercial autorisé" not in HTML
    assert "<th>SKU</th>" not in HTML and 'placeholder="SKU"' not in HTML and '"SKU, nom' not in HTML
    assert 'data-label="SKU"' not in HTML and 'data-bob-product="SKU"' not in HTML


def test_api_messages_without_jargon():
    root = Path(__file__).resolve().parents[1]
    for path in ("api/catalog/products.py", "api/whatsapp/routes.py", "api/tenants/routes.py", "api/demo/routes.py"):
        text = (root / path).read_text(encoding="utf-8")
        assert "pour ce tenant" not in text and 'detail="Tenant introuvable"' not in text and "Ce tenant" not in text, path
        assert "avec le SKU" not in text, path


@pytest.mark.asyncio
async def test_duplicate_reference_message(client, db_session):
    await _shop(db_session, "dup@home.sn")
    headers = await _headers(client, "dup@home.sn")
    body = {"sku": "REF-1", "name": "Sac", "price": 1000, "currency": "XOF", "stock_quantity": 1}
    await client.post("/api/v1/products", json=body, headers=headers)
    r = await client.post("/api/v1/products", json=body, headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "Un produit avec la référence « REF-1 » existe déjà"
