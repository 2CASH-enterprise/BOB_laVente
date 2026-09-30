"""Lot 33 — démo par secteur (boutique / concession), pays, et fin identique à la vraie inscription."""
import io
import re
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.main import app
from app.models.appointment_request import AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User
from app.services import booking
from app.services.csv_column_mapper import map_csv_to_canonical_format
from app.services.local_time import tenant_zone
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

HTML = open("app/static/instant-demo/index.html", encoding="utf-8").read()
DEALER_SAMPLE = re.search(r"CAR_DEALERSHIP: \{.*?sample: `(.*?)`", HTML, re.S).group(1)
STORE_SAMPLE = re.search(r"ONLINE_STORE: \{.*?sample: `(.*?)`", HTML, re.S).group(1)


async def _create(client, csv_text, **data):
    files = {"file": ("catalogue.csv", io.BytesIO(csv_text.encode()), "text/csv")}
    return await client.post("/api/v1/demo/create", data={"company_name": "Auto Plus", "currency": "XOF", **data}, files=files)


async def _tenant(db_session, tenant_id):
    return (await db_session.execute(select(Tenant).where(Tenant.id == uuid.UUID(tenant_id))
                                     .execution_options(populate_existing=True))).scalar_one()


@pytest.mark.asyncio
async def test_dealership_demo_keeps_vehicle_sheets_and_opens_slots(client, db_session):
    r = await _create(client, DEALER_SAMPLE, business_type="CAR_DEALERSHIP", country="FR", currency="EUR")

    assert r.status_code == 200 and r.json()["imported"] == 6 and r.json()["failed"] == 0
    tenant = await _tenant(db_session, r.json()["tenant_id"])
    assert (tenant.business_type, tenant.country, tenant.currency) == ("CAR_DEALERSHIP", "FR", "EUR")
    assert tenant.business_type_chosen_at is not None and tenant.is_demo
    products = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().all()
    assert all(p.vehicle and p.vehicle.get("body_type") for p in products)
    rav4 = next(p for p in products if "RAV4" in p.name)
    assert rav4.vehicle == {"brand": "Toyota", "model": "RAV4", "year": 2022, "mileage_km": 28000, "fuel": "HYBRIDE",
                            "gearbox": "AUTOMATIQUE", "body_type": "SUV", "color": "Blanc"}
    settings = (await db_session.execute(select(TenantAppointmentSettings).where(
        TenantAppointmentSettings.tenant_id == tenant.id))).scalar_one()
    assert settings.online_booking and settings.opening_hours["5"] == [["09:00", "12:00"], ["14:00", "18:00"]]
    assert "6" not in settings.opening_hours  # dimanche fermé


@pytest.mark.asyncio
async def test_store_demo_ignores_vehicle_columns_and_keeps_old_defaults(client, db_session):
    r = await _create(client, DEALER_SAMPLE)  # sans secteur ni pays : comme les anciennes démos

    tenant = await _tenant(db_session, r.json()["tenant_id"])
    assert (tenant.business_type, tenant.country) == ("ONLINE_STORE", "SN")
    products = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().all()
    assert products and all(p.vehicle is None for p in products)
    assert (await db_session.execute(select(TenantAppointmentSettings))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"business_type": "GARAGE"}, {"country": "France"}, {"country": "1X"}])
async def test_bad_sector_or_country_refused(client, db_session, data):
    assert (await _create(client, STORE_SAMPLE, **data)).status_code == 400


def test_mapper_keeps_vehicle_columns_only_when_asked():
    kept = map_csv_to_canonical_format(DEALER_SAMPLE, "XOF", keep_vehicle=True).splitlines()[0]
    dropped = map_csv_to_canonical_format(DEALER_SAMPLE, "XOF").splitlines()[0]
    assert kept.endswith("brand,model,year,mileage_km,fuel,gearbox,body_type,color")
    assert dropped == "SKU,NAME,DESCRIPTION,CATEGORY,PRICE,CURRENCY,STOCK,IMAGE_URL,ACTIVE"


@pytest.fixture
def llm():
    def use(responses):
        app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient(responses)

    yield use
    app.dependency_overrides.pop(get_llm_client, None)


@pytest.mark.asyncio
async def test_dealership_demo_books_a_slot_and_shows_the_fixed_confirmation(client, db_session, llm):
    created = (await _create(client, DEALER_SAMPLE, business_type="CAR_DEALERSHIP")).json()
    tenant = await _tenant(db_session, created["tenant_id"])
    settings = await booking.load_settings(db_session, tenant.id)
    [slot] = await booking.free_slots(db_session, tenant, settings, tenant_zone(tenant), datetime.now(timezone.utc), limit=1)
    label = booking.slots_for_ai([slot], tenant_zone(tenant))[0]["label"]
    llm([
        tool_use_response("request_appointment", {"kind": "ESSAI", "slot": booking.slot_id(slot), "vehicle": "Toyota RAV4"}),
        text_response("Parfait, **c'est noté** !"),
    ])

    r = await client.post("/api/v1/demo/chat", json={"message": "Samedi 10 h"},
                          headers={"Authorization": f"Bearer {created['demo_token']}"})

    body = r.json()
    assert body["reply"] == "Parfait, *c'est noté* !"
    assert body["extra_messages"][0] == f"Bonjour ! Votre essai (Toyota RAV4) est confirmé le {label}. À bientôt chez Auto Plus !"
    # Lot 34b : puis la demande d'email, comme sur WhatsApp.
    assert body["extra_messages"][1].startswith("Souhaitez-vous recevoir un rappel par email") and len(body["extra_messages"]) == 2


@pytest.mark.asyncio
async def test_promotion_asks_the_full_name_and_cancels_demo_appointments(client, db_session, llm):
    created = (await _create(client, DEALER_SAMPLE, business_type="CAR_DEALERSHIP")).json()
    headers = {"Authorization": f"Bearer {created['demo_token']}"}
    tenant = await _tenant(db_session, created["tenant_id"])
    settings = await booking.load_settings(db_session, tenant.id)
    [slot] = await booking.free_slots(db_session, tenant, settings, tenant_zone(tenant), datetime.now(timezone.utc), limit=1)
    llm([tool_use_response("request_appointment", {"kind": "ESSAI", "slot": booking.slot_id(slot)}), text_response("Noté !")])
    await client.post("/api/v1/demo/chat", json={"message": "Samedi"}, headers=headers)

    missing = await client.post("/api/v1/demo/promote", json={"email": "a@example.com", "password": "supersecret123"}, headers=headers)
    assert missing.status_code == 422

    r = await client.post("/api/v1/demo/promote", headers=headers,
                          json={"full_name": " Awa Diop ", "email": "awa-demo@example.com", "password": "supersecret123"})
    assert r.status_code == 200
    owner = (await db_session.execute(select(User).where(User.email == "awa-demo@example.com")
                                      .execution_options(populate_existing=True))).scalar_one()
    assert owner.full_name == "Awa Diop"
    [appointment] = (await db_session.execute(select(AppointmentRequest).execution_options(populate_existing=True))).scalars().all()
    assert appointment.status == "CANCELLED"  # aucun rappel ne partira vers le nouveau compte
    bt = (await client.get("/api/v1/tenants/me/business-type", headers={"Authorization": f"Bearer {r.json()['access_token']}"})).json()
    assert bt == {**bt, "business_type": "CAR_DEALERSHIP", "chosen": True}  # pas de nouvel écran de choix


def test_demo_page_offers_both_sectors_and_escapes_bob_replies():
    assert "chooseSector('ONLINE_STORE')" in HTML and "chooseSector('CAR_DEALERSHIP')" in HTML
    assert 'formData.append("business_type", selectedSector);' in HTML and 'formData.append("country", country);' in HTML
    assert 'if (role === "ai" || role === "system") div.innerHTML = waFormat(text);' in HTML
    assert "return esc(value).replace(" in HTML
    assert 'id="promote-name"' in HTML and "full_name: fullName" in HTML
    for code in ("SN", "CI", "CM", "FR", "MA"):
        assert f'["{code}", ' in HTML
