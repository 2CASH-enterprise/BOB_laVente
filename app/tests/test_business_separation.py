"""
Lot 32 — type d'activité choisi une seule fois (changé ensuite uniquement par l'Admin), et
fonctions réservées à une activité refusées par le serveur pour l'autre.
"""
import io
import uuid

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.audit_log import AuditLog
from app.models.product import Product
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.business_type import CAR_DEALERSHIP, ONLINE_STORE


async def _tenant(db_session, email, business_type=None, chosen=True):
    from datetime import datetime, timezone

    tenant = Tenant(name="Boutique <b>X</b>", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO,
                    business_type=business_type or ONLINE_STORE,
                    business_type_chosen_at=datetime.now(timezone.utc) if chosen else None)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    return tenant


async def _auth(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _admin(client, db_session):
    email = f"admin-{uuid.uuid4().hex[:6]}@bob.internal"
    db_session.add(SuperAdminUser(email=email, hashed_password=hash_password("supersecret123"), full_name="Admin"))
    await db_session.commit()
    token = (await client.post("/api/v1/superadmin/login", data={"username": email, "password": "supersecret123"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Choix unique, puis Admin uniquement ------------------------------------------------------

@pytest.mark.asyncio
async def test_business_type_is_chosen_once_then_locked(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email, chosen=False)
    headers = await _auth(client, unique_email)

    first = await client.put("/api/v1/tenants/me/business-type", json={"business_type": CAR_DEALERSHIP}, headers=headers)
    assert first.status_code == 200 and first.json()["chosen"] is True

    again = await client.put("/api/v1/tenants/me/business-type", json={"business_type": ONLINE_STORE}, headers=headers)
    assert again.status_code == 409 and "support Bob" in again.json()["detail"]
    await db_session.refresh(tenant)
    assert tenant.business_type == CAR_DEALERSHIP


@pytest.mark.asyncio
async def test_admin_changes_the_business_type_and_it_is_logged(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    headers = await _admin(client, db_session)

    r = await client.put(f"/api/v1/superadmin/tenants/{tenant.id}/business-type", json={"business_type": CAR_DEALERSHIP}, headers=headers)

    assert r.status_code == 200 and r.json()["business_type"] == CAR_DEALERSHIP
    await db_session.refresh(tenant)
    assert tenant.business_type == CAR_DEALERSHIP and tenant.business_type_chosen_at is not None
    [log] = (await db_session.execute(select(AuditLog).where(AuditLog.action == "BUSINESS_TYPE_CHANGED_BY_ADMIN"))).scalars().all()
    assert log.tenant_id == tenant.id and log.actor.startswith("superadmin:")
    listed = (await client.get("/api/v1/superadmin/tenants", headers=headers)).json()
    assert listed[0]["business_type"] == CAR_DEALERSHIP


@pytest.mark.asyncio
async def test_admin_route_refuses_bad_values_and_tenant_tokens(client, db_session, unique_email):
    tenant = await _tenant(db_session, unique_email)
    admin = await _admin(client, db_session)
    bad = await client.put(f"/api/v1/superadmin/tenants/{tenant.id}/business-type", json={"business_type": "GARAGE"}, headers=admin)
    assert bad.status_code == 400
    missing = await client.put(f"/api/v1/superadmin/tenants/{uuid.uuid4()}/business-type", json={"business_type": CAR_DEALERSHIP}, headers=admin)
    assert missing.status_code == 404
    as_tenant = await client.put(f"/api/v1/superadmin/tenants/{tenant.id}/business-type", json={"business_type": CAR_DEALERSHIP},
                                 headers=await _auth(client, unique_email))
    assert as_tenant.status_code in (401, 403)
    await db_session.refresh(tenant)
    assert tenant.business_type == ONLINE_STORE


# --- Fonctions réservées : refus côté serveur -------------------------------------------------

STORE_ONLY = [
    ("GET", "/api/v1/orders", None),
    ("POST", "/api/v1/orders", {"customer_id": str(uuid.uuid4()), "items": []}),
    ("GET", "/api/v1/tenants/me/negotiation-settings", None),
    ("PUT", "/api/v1/tenants/me/negotiation-settings", {"enabled": True}),
    ("GET", "/api/v1/analytics/sales", None),
    ("POST", "/api/v1/analytics/sales/refresh", None),
    ("GET", f"/api/v1/products/{uuid.uuid4()}/complements", None),
]
DEALER_ONLY = [
    ("GET", "/api/v1/appointments", None),
    ("GET", "/api/v1/appointments/settings", None),
    ("PUT", "/api/v1/appointments/settings", {"online_booking": False, "opening_hours": {}}),
    ("POST", f"/api/v1/appointments/{uuid.uuid4()}/cancel", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path, body", STORE_ONLY)
async def test_store_features_refused_to_a_dealership(client, db_session, unique_email, method, path, body):
    await _tenant(db_session, unique_email, CAR_DEALERSHIP)
    r = await client.request(method, path, json=body, headers=await _auth(client, unique_email))
    assert r.status_code == 403 and "type d'activité" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path, body", DEALER_ONLY)
async def test_dealership_features_refused_to_an_online_store(client, db_session, unique_email, method, path, body):
    await _tenant(db_session, unique_email, ONLINE_STORE)
    r = await client.request(method, path, json=body, headers=await _auth(client, unique_email))
    assert r.status_code == 403 and "type d'activité" in r.json()["detail"]


@pytest.mark.asyncio
async def test_each_business_keeps_its_own_features(client, db_session, unique_email):
    await _tenant(db_session, unique_email, ONLINE_STORE)
    await _tenant(db_session, f"concession-{unique_email}", CAR_DEALERSHIP)
    store = await _auth(client, unique_email)
    dealer = await _auth(client, f"concession-{unique_email}")
    assert (await client.get("/api/v1/orders", headers=store)).status_code == 200
    assert (await client.get("/api/v1/tenants/me/negotiation-settings", headers=store)).status_code == 200
    assert (await client.get("/api/v1/appointments", headers=dealer)).status_code == 200
    assert (await client.get("/api/v1/appointments/settings", headers=dealer)).status_code == 200


@pytest.mark.asyncio
async def test_vehicle_sheet_refused_to_an_online_store(client, db_session, unique_email):
    await _tenant(db_session, unique_email, ONLINE_STORE)
    headers = await _auth(client, unique_email)
    product = {"sku": "R1", "name": "Robe", "price": 1000, "currency": "XOF", "stock_quantity": 1}
    refused = await client.post("/api/v1/products", json={**product, "vehicle": {"brand": "Peugeot"}}, headers=headers)
    assert refused.status_code == 403
    created = (await client.post("/api/v1/products", json=product, headers=headers)).json()
    patched = await client.put(f"/api/v1/products/{created['id']}", json={"vehicle": {"year": 2020}}, headers=headers)
    assert patched.status_code == 403
    cleared = await client.put(f"/api/v1/products/{created['id']}", json={"vehicle": None, "name": "Robe bleue"}, headers=headers)
    assert cleared.status_code == 200


@pytest.mark.asyncio
async def test_vehicle_sheet_still_works_for_a_dealership(client, db_session, unique_email):
    await _tenant(db_session, unique_email, CAR_DEALERSHIP)
    r = await client.post("/api/v1/products", headers=await _auth(client, unique_email), json={
        "sku": "P5008", "name": "Peugeot 5008", "price": 1000, "currency": "XOF", "stock_quantity": 1,
        "vehicle": {"brand": "Peugeot", "body_type": "SUV"}})
    assert r.status_code == 201 and r.json()["vehicle"]["body_type"] == "SUV"


CSV = "SKU,NAME,PRICE,CURRENCY,STOCK,MARQUE,CARROSSERIE\nP1,Peugeot 5008,1000,XOF,1,Peugeot,SUV\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("business_type, expected", [(ONLINE_STORE, None), (CAR_DEALERSHIP, {"brand": "Peugeot", "body_type": "SUV"})])
async def test_csv_vehicle_columns_only_read_for_a_dealership(client, db_session, unique_email, business_type, expected):
    tenant = await _tenant(db_session, unique_email, business_type)
    files = {"file": ("catalogue.csv", io.BytesIO(CSV.encode()), "text/csv")}
    r = await client.post("/api/v1/catalog/import-csv", files=files, headers=await _auth(client, unique_email))
    assert r.status_code == 200 and r.json()["imported"] == 1
    product = (await db_session.execute(select(Product).where(Product.tenant_id == tenant.id))).scalar_one()
    assert product.vehicle == expected


def test_superadmin_console_escapes_merchant_data():
    html = open("app/static/superadmin/index.html", encoding="utf-8").read()
    assert "${esc(t.name)}" in html and "${esc(t.email)}" in html and "${t.name}" not in html
    assert "changeBusinessType(" in html and "confirm(" in html
