import io

import pytest

from app.core.security import hash_password
from app.models.tenant import Tenant
from app.models.user import Role, User


async def _setup_tenant(db_session, email: str) -> Tenant:
    tenant = Tenant(name=f"Tenant {email}", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    user = User(
        tenant_id=tenant.id,
        email=email,
        hashed_password=hash_password("secret123456"),
        full_name="User",
        role=Role.MANAGER,
    )
    db_session.add(user)
    await db_session.commit()
    return tenant


async def _login(client, email: str) -> str:
    response = await client.post("/api/v1/auth/login", data={"username": email, "password": "secret123456"})
    return response.json()["access_token"]


CSV_CONTENT = """SKU,NAME,DESCRIPTION,CATEGORY,PRICE,CURRENCY,STOCK,IMAGE_URL,ACTIVE
SAM-A56,Samsung A56,Le dernier Samsung,Téléphones,280000,XOF,12,,true
SAM-A36,Samsung A36,Version économique,Téléphones,220000,XOF,0,,true
IPH-15,iPhone 15,,Téléphones,650000,XOF,5,,false
BAD-ROW,,Sans nom,,100,XOF,1,,true
"""


@pytest.mark.asyncio
async def test_csv_import_creates_products_and_categories(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    files = {"file": ("catalogue.csv", io.BytesIO(CSV_CONTENT.encode("utf-8")), "text/csv")}
    response = await client.post("/api/v1/catalog/import-csv", files=files, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total_rows"] == 4
    assert body["imported"] == 3  # 3 lignes valides
    assert body["failed"] == 1  # la ligne BAD-ROW (sans SKU) échoue
    assert body["available"] == 1  # seul SAM-A56 est actif ET en stock
    assert body["unavailable"] == 2  # SAM-A36 (stock 0) et iPhone 15 (inactif)

    list_response = await client.get("/api/v1/products", params={"query": ""}, headers=headers)
    names = {p["name"] for p in list_response.json() if p["active"]}
    assert "Samsung A56" in names
    assert "Samsung A36" in names  # actif même à stock 0, il apparaît toujours dans le catalogue

    categories = await client.get("/api/v1/categories", headers=headers)
    category_names = {c["name"] for c in categories.json()}
    assert category_names == {"Téléphones"}


@pytest.mark.asyncio
async def test_csv_import_upserts_on_second_import(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    files = {"file": ("catalogue.csv", io.BytesIO(CSV_CONTENT.encode("utf-8")), "text/csv")}
    await client.post("/api/v1/catalog/import-csv", files=files, headers=headers)

    updated_csv = CSV_CONTENT.replace("280000", "260000")  # baisse de prix du Samsung A56
    files2 = {"file": ("catalogue.csv", io.BytesIO(updated_csv.encode("utf-8")), "text/csv")}
    second = await client.post("/api/v1/catalog/import-csv", files=files2, headers=headers)

    assert second.status_code == 200
    assert second.json()["updated"] == 3  # les 3 mêmes produits sont mis à jour, pas dupliqués
    assert second.json()["imported"] == 0

    products = await client.get("/api/v1/products", params={"query": "Samsung A56"}, headers=headers)
    assert float(products.json()[0]["price"]) == 260000.0


@pytest.mark.asyncio
async def test_csv_import_rejects_non_csv_file(client, db_session, unique_email):
    await _setup_tenant(db_session, unique_email)
    token = await _login(client, unique_email)
    headers = {"Authorization": f"Bearer {token}"}

    files = {"file": ("catalogue.txt", io.BytesIO(b"not a csv"), "text/plain")}
    response = await client.post("/api/v1/catalog/import-csv", files=files, headers=headers)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_csv_import_requires_manager_role(client, db_session, unique_email):
    tenant = Tenant(name="T", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email=unique_email,
        hashed_password=hash_password("secret123456"),
        full_name="Agent",
        role=Role.AGENT,
    )
    db_session.add(user)
    await db_session.commit()

    token = await _login(client, unique_email)
    files = {"file": ("catalogue.csv", io.BytesIO(CSV_CONTENT.encode("utf-8")), "text/csv")}
    response = await client.post(
        "/api/v1/catalog/import-csv", files=files, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
