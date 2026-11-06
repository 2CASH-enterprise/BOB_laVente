"""
Lot 108 b — supports imprimés du restaurant (maquettes validées le 06/11) : affiche vitrine, carte au comptoir,
affiche d'un plat (plat du jour, spécialité, nouveauté), chevalets des tables 4 par page, image « Plat du jour » à
partager sur WhatsApp (dessinée dans le tableau de bord, sans QR code).
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models.appointment_settings import TenantAppointmentSettings
from app.models.product import Product
from app.models.product_qr_code import ProductQrCode
from app.models.table_qr_code import TableQrCode
from app.services import resto_posters as rp
from app.services.business_type import ONLINE_STORE, RESTAURANT
from app.tests.test_lot53_courtier import _headers
from app.tests.test_lot59_excel_kit import HTML, _connected, _function
from app.tests.test_lot106_restaurant import SERVICES

PHOTO = "https://cdn.example.ci/poulet.jpg"


async def _resto(db, **fields):
    tenant = await _connected(db, RESTAURANT)
    tenant.name, tenant.currency = "Chez Tante Awa", "XOF"
    tenant.delivery_enabled, tenant.pickup_enabled, tenant.dine_in_enabled = True, True, True
    for key, value in fields.items():
        setattr(tenant, key, value)
    await db.commit()
    return tenant


async def _dish(db, tenant, name="Poulet braisé", code="GRILLADE", sheet=None, **extra):
    dish = Product(tenant_id=tenant.id, sku=f"P{uuid.uuid4().hex[:6]}", name=name, price=4500, currency="XOF",
                   bob_category=code, sheet=sheet, **{"active": True, "stock_quantity": 1, **extra})
    db.add(dish)
    await db.commit()
    return dish


@pytest.mark.asyncio
async def test_the_restaurant_window_and_counter_posters(client, db_session):
    tenant = await _resto(db_session, kitchen_hours={str(d): [["11:00", "23:00"]] for d in range(7)})
    await _dish(db_session, tenant, image_url=PHOTO)
    await _dish(db_session, tenant, name="Bissap", code="BOISSON")
    await _dish(db_session, tenant, name="Kedjenou", code="PLAT", stock_quantity=0)  # indisponible : sa rubrique n'apparaît pas
    headers = await _headers(client, tenant)
    options = (await client.get("/api/v1/kit/poster-options", headers=headers)).json()
    assert [m["code"] for m in options["models"]][:2] == ["resto_vitrine", "resto_comptoir"]
    assert options["accents"][0]["code"] == "safran"
    vitrine = await client.get(f"/api/v1/kit/poster?model=resto_vitrine&accent=safran&photo={options['photos'][0]['id']}", headers=headers)
    assert vitrine.status_code == 200 and "size: 210mm 297mm" in vitrine.text
    page = vitrine.text
    assert "Commandez <span" in page and "réservez" not in page  # pas de réservation en ligne : on ne la promet pas
    assert f'src="{PHOTO}"' in page and "Cuisine ouverte de 11 h à 23 h, 7 jours sur 7" in page
    assert "À emporter" in page and "Livraison" in page and "Sur place" in page and "Proposé par" in page
    assert "+225 07 07 00 00 99" in page and "#C9913D" in page
    counter = (await client.get("/api/v1/kit/poster?model=resto_comptoir", headers=headers)).text
    assert "size: 148mm 210mm" in counter and "Grillades · Boissons" in counter and "Notre carte" in counter
    db_session.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True, opening_hours=SERVICES, slot_minutes=30, capacity=40))
    await db_session.commit()
    assert "Commandez et réservez" in (await client.get("/api/v1/kit/poster?model=resto_vitrine", headers=headers)).text
    shop = await _connected(db_session, ONLINE_STORE)
    assert (await client.get("/api/v1/kit/poster?model=resto_vitrine", headers=await _headers(client, shop))).status_code == 422
    shop_options = (await client.get("/api/v1/kit/poster-options", headers=await _headers(client, shop))).json()
    assert "safran" not in [a["code"] for a in shop_options["accents"]]


@pytest.mark.asyncio
async def test_the_dish_poster(client, db_session):
    tenant = await _resto(db_session, address_form="TU")  # le tutoiement ne vaut pas pour un restaurant (lot 106)
    dish = await _dish(db_session, tenant, sheet={"composition": "Mariné aux épices, <oignons>", "sides": "attiéké, alloco ou riz"},
                       image_url=PHOTO)
    qr = ProductQrCode(tenant_id=tenant.id, product_id=dish.id, code=uuid.uuid4().hex[:8])
    db_session.add(qr)
    await db_session.commit()
    headers = await _headers(client, tenant)
    r = await client.get(f"/api/v1/qr-codes/{qr.id}/poster?model=plat&banner=specialite", headers=headers)
    assert r.status_code == 200, r.text
    page = r.text
    assert "Notre spécialité" in page and "À la carte" in page and "Poulet braisé" in page
    assert "Mariné aux épices, &lt;oignons&gt;" in page and "<oignons>" not in page  # échappé
    assert "Au choix" in page and "alloco" in page and "4 500" in page and "FCFA" in page
    assert "border-radius:50%" in page and f'src="{PHOTO}"' in page and "Commandez-le sur WhatsApp" in page
    today = (await client.get(f"/api/v1/qr-codes/{qr.id}/poster?model=plat", headers=headers)).text
    assert "Plat du jour" in today and rp.today_line(tenant) in today
    shop = await _connected(db_session, ONLINE_STORE)
    product = Product(tenant_id=shop.id, sku="R1", name="Robe", price=1000, currency="XOF", active=True)
    db_session.add(product)
    await db_session.flush()
    shop_qr = ProductQrCode(tenant_id=shop.id, product_id=product.id, code=uuid.uuid4().hex[:8])
    db_session.add(shop_qr)
    await db_session.commit()
    assert (await client.get(f"/api/v1/qr-codes/{shop_qr.id}/poster?model=plat", headers=await _headers(client, shop))).status_code == 422


def test_a_dish_without_photo_or_price_leaves_nothing_empty():
    tenant = type("T", (), {"name": "Maquis", "business_type": RESTAURANT, "address_form": "VOUS", "pickup_enabled": True,
                            "delivery_enabled": False, "dine_in_enabled": False, "country": "CI"})()
    dish = Product(name="Garba", price=0, currency="XOF", sheet=None)
    page = rp.dish_poster(tenant, dish, "+225 01", "<svg width=\"10\" height=\"10\"></svg>", None, "nouveau")
    assert "Nouveau" in page and "<img" not in page and "FCFA" not in page and "Au choix" not in page
    assert "À emporter" in page and "Livraison" not in page


@pytest.mark.asyncio
async def test_table_cards_four_per_page(client, db_session):
    tenant = await _resto(db_session)
    headers = await _headers(client, tenant)
    assert (await client.get("/api/v1/table-qr-codes/sheet", headers=headers)).status_code == 422  # pas encore de tables
    for n in range(1, 7):
        db_session.add(TableQrCode(tenant_id=tenant.id, code=f"T{n}{uuid.uuid4().hex[:5]}", label=str(n), scan_count=0))
    await db_session.commit()
    r = await client.get("/api/v1/table-qr-codes/sheet?accent=sauge", headers=headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    page = r.text
    assert page.count('<div class="sheet">') == 2 and page.count('class="card"') == 6  # 4 + 2
    assert "QR code de la table 6" in page and "Commandez<br><span" in page and "depuis votre table" in page
    assert "Scannez" in page and "On vous sert" in page and "break-after: page" in page and "#A3B1A0" in page
    shop = await _connected(db_session, ONLINE_STORE)
    assert (await client.get("/api/v1/table-qr-codes/sheet", headers=await _headers(client, shop))).status_code == 403


def test_short_kitchen_hours_and_dates():
    t = type("T", (), {})()
    t.kitchen_hours = {"0": [["11:00", "15:00"], ["18:30", "23:00"]], "1": [["11:00", "15:00"], ["18:30", "23:00"]]}
    assert rp.short_hours(t) == "Cuisine ouverte de 11 h à 15 h et de 18 h 30 à 23 h"
    t.kitchen_hours = {"0": [["11:00", "15:00"]], "1": [["12:00", "15:00"]]}
    assert rp.short_hours(t) is None  # des horaires différents : on n'écrit rien plutôt qu'une erreur
    t.kitchen_hours = {}
    assert rp.short_hours(t) is None
    t.country = "CI"
    assert rp.today_line(t, datetime(2026, 11, 6, 10, tzinfo=timezone.utc)) == "Vendredi 6 novembre"


@pytest.mark.asyncio
async def test_dashboard_dish_image_and_print_buttons(client):
    share = _function("shareDishImage")
    assert "Plat du jour" in share and "Commandez sur WhatsApp" in share and "kit.phone" in share
    assert "navigator.share" in share and "qr" not in share.lower()  # sur le téléphone : pas de QR code
    assert "/api/v1/tenants/me/fulfillment" in share
    assert "openTableSheet()" in HTML and "/api/v1/table-qr-codes/sheet" in _function("openTableSheet")
    assert "openQrPoster(${jsArg(c.id)}, 'plat')" in HTML and 'id="qr-banner"' in HTML and "params.banner" in _function("openQrPoster")
    assert "shareDishImage(${jsArg(p.id)})" in _function("dishToday")
    font = await client.get("/fonts/jost-latin-400-normal.woff2")
    assert font.status_code == 200 and len(font.content) > 1000
