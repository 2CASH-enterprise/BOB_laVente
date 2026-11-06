"""
Lot 109 — le restaurant dans la démo instantanée (carte d'exemple prête), la pré-inscription et la prospection, présenté
avec l'équipe Bob + Naomy (Naomy publie sur les réseaux, Bob sert sur WhatsApp). Jamais de prix dans les messages.
"""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.appointment_settings import TenantAppointmentSettings
from app.models.product import Product
from app.models.prospect import Prospect
from app.models.tenant import Tenant
from app.services import preregistration as pre
from app.services import prospect_mailer as mailer
from app.services import prospection as pros
from app.services import restaurant, restaurant_demo
from app.services.business_type import RESTAURANT

ROOT = Path(__file__).resolve().parents[1]
DEMO = (ROOT / "static" / "instant-demo" / "index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_restaurant_demo_comes_with_a_ready_menu(client, db_session):
    r = await client.post("/api/v1/demo/create", data={"company_name": "Maquis Chez Awa", "currency": "XOF", "country": "CI",
                                                       "business_type": RESTAURANT})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == len(restaurant_demo.MENU) == r.json()["available"]
    tenant = (await db_session.execute(select(Tenant).where(Tenant.name == "Maquis Chez Awa"))).scalar_one()
    assert tenant.is_demo and tenant.business_type == RESTAURANT
    assert restaurant.modes(tenant) == ["PICKUP", "DELIVERY", "DINE_IN"] and tenant.kitchen_hours["0"] == [["11:00", "23:00"]]
    assert [a["name"] for a in tenant.delivery_areas] == ["Cocody", "Plateau", "Marcory", "Yopougon"]
    dishes = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars().all()
    poulet = next(d for d in dishes if d.name == "Poulet braisé")
    assert float(poulet.price) == 4500 and poulet.currency == "XOF" and poulet.bob_category == "GRILLADE"
    assert poulet.sheet["sides"] == "attiéké, alloco ou riz" and all(d.stock_quantity == 1 for d in dishes)
    booking = (await db_session.execute(select(TenantAppointmentSettings).where(
        TenantAppointmentSettings.tenant_id == tenant.id))).scalar_one()
    assert booking.online_booking and booking.capacity == 40 and booking.opening_hours["5"] == [["12:00", "15:00"], ["19:00", "23:00"]]
    assert await restaurant_demo.seed(db_session, tenant) == 0  # jamais deux fois


@pytest.mark.asyncio
async def test_a_demo_outside_fcfa_countries_converts_prices_and_areas(client, db_session):
    r = await client.post("/api/v1/demo/create", data={"company_name": "Bistrot Test", "currency": "EUR", "country": "FR",
                                                       "business_type": RESTAURANT})
    assert r.status_code == 200
    tenant = (await db_session.execute(select(Tenant).where(Tenant.name == "Bistrot Test"))).scalar_one()
    poulet = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id, Product.name == "Poulet braisé"))).scalar_one()
    assert float(poulet.price) == 7.0 and poulet.currency == "EUR"  # 4 500 FCFA ≈ 6,86 € → 7,00
    assert [a["name"] for a in tenant.delivery_areas][0] == "Centre-ville"
    assert restaurant_demo.local_price(500, "EUR") == restaurant_demo.local_price(500, "eur") > 0


def test_the_demo_page_offers_the_restaurant_and_the_team():
    assert "chooseSector('RESTAURANT')" in DEMO and 'RESTAURANT: {' in DEMO and 'restaurant: "RESTAURANT"' in DEMO
    assert 'id="naomy-team"' in DEMO and "Avec Naomy, l'équipe est complète." in DEMO and "!sector.team" in DEMO
    assert 'code !== "INSURANCE_BROKER"' in DEMO  # le statut du courtier n'apparaît plus pour un restaurant
    assert 'else if (!noCatalog) formData.append("file", selectedFile);' in DEMO
    assert "FCFA" not in DEMO.split("RESTAURANT: {")[1].split("INSURANCE_BROKER: {")[0]  # aucun prix d'offre


def test_preregistration_speaks_restaurant():
    c = pre.content(RESTAURANT)
    assert len(c["does"]) == 4 and "Naomy" in c["does"][3] and c["products_label"] == "Votre carte"
    assert c["size_label"] == "Nombre de plats" and "carte" in c["prepare"][1]
    home = pre.example_home(RESTAURANT, "Awa", "XOF")
    assert home["business_type"] == RESTAURANT and "paid" in home["kpis"] and len(home["daily"]) == 30
    assert any("Naomy" in a["detail"] for a in home["activity"]) and len(home["funnel"]) == 4


def test_prospection_presents_the_team_without_any_price():
    assert pros.SECTORS["RESTAURANT"] == "Restaurant" and pros.SECTOR_SLUGS["RESTAURANT"] == "restaurant"
    assert pros.visual_url("RESTAURANT").endswith("/visuels/prospection-restaurant.jpg")
    assert (ROOT / "static" / "visuels" / "prospection-restaurant.jpg").stat().st_size > 20000
    p = Prospect(company="Maquis <Awa>", contact_name="Awa Koné", sector="RESTAURANT", code="r109", country="CI")
    text = pros.message(p, "Thérèse")
    assert text.startswith("Bonjour Awa, je suis Thérèse d'AgenC'AI. Pour Maquis <Awa>, nous proposons une équipe de deux agents")
    assert "Naomy" in text and "Bob" in text and "secteur=restaurant" in text
    for wording in [text] + [subject + body for subject, body in mailer.DEFAULTS["RESTAURANT"]]:
        assert "FCFA" not in wording and "15 000" not in wording and "10 000" not in wording  # rien n'est définitif
    assert len(mailer.DEFAULTS["RESTAURANT"]) == 3 and "{lien}" in mailer.DEFAULTS["RESTAURANT"][0][1]
    for word in ("Restaurant Le Baobab", "Maquis du coin", "Grill & Co", "Traiteur Saveurs", "Fast-food Express"):
        assert pros.guess_sector(word, "ONLINE_STORE") == "RESTAURANT", word
    assert pros.guess_sector("Garage Auto Plus", "ONLINE_STORE") == "CAR_DEALERSHIP"


@pytest.mark.asyncio
async def test_the_shared_link_preview_names_the_team(client, db_session):
    from app.api.prospects.routes import PREVIEW_TEXTS

    title, text = PREVIEW_TEXTS["RESTAURANT"]
    assert title == "Bob et Naomy, l'équipe de votre restaurant" and "Naomy publie" in text and "FCFA" not in text
    assert (await client.get("/visuels/prospection-restaurant.jpg")).status_code == 200
