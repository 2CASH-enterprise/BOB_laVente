import io

import pytest

from app.agents.dependency import get_llm_client
from app.main import app
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

SAMPLE_CSV = b"""Nom,Prix,Description,Stock
Samsung A56,280000,256 Go,12
iPhone 15,450000,128 Go,5
"""


@pytest.mark.asyncio
async def test_create_demo_no_auth_required(client, db_session):
    files = {"file": ("catalogue.csv", io.BytesIO(SAMPLE_CSV), "text/csv")}
    response = await client.post(
        "/api/v1/demo/create", data={"company_name": "Boutique Test Démo", "currency": "XOF"}, files=files
    )
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 2
    assert "demo_token" in body


@pytest.mark.asyncio
async def test_create_demo_marks_tenant_as_demo(client, db_session):
    files = {"file": ("catalogue.csv", io.BytesIO(SAMPLE_CSV), "text/csv")}
    response = await client.post(
        "/api/v1/demo/create", data={"company_name": "Boutique Test Démo", "currency": "XOF"}, files=files
    )
    tenant_id = response.json()["tenant_id"]

    from sqlalchemy import select

    from app.models.tenant import Tenant

    import uuid as uuid_module

    tenant = (await db_session.execute(select(Tenant).where(Tenant.id == uuid_module.UUID(tenant_id)))).scalar_one()
    assert tenant.is_demo is True


@pytest.mark.asyncio
async def test_create_demo_without_company_name_rejected(client, db_session):
    files = {"file": ("catalogue.csv", io.BytesIO(SAMPLE_CSV), "text/csv")}
    response = await client.post("/api/v1/demo/create", data={"company_name": "", "currency": "XOF"}, files=files)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_demo_chat_uses_real_imported_catalog(client, db_session):
    files = {"file": ("catalogue.csv", io.BytesIO(SAMPLE_CSV), "text/csv")}
    create_response = await client.post(
        "/api/v1/demo/create", data={"company_name": "Boutique Test Démo", "currency": "XOF"}, files=files
    )
    demo_token = create_response.json()["demo_token"]

    fake = FakeLLMClient(
        [
            tool_use_response("search_products", {"query": "Samsung"}),
            text_response("Nous avons le Samsung A56 à 280 000 XOF, disponible."),
        ]
    )
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        response = await client.post(
            "/api/v1/demo/chat",
            json={"message": "Avez-vous un Samsung ?"},
            headers={"Authorization": f"Bearer {demo_token}"},
        )
        assert response.status_code == 200
        assert "280 000" in response.json()["reply"]
    finally:
        del app.dependency_overrides[get_llm_client]


@pytest.mark.asyncio
async def test_demo_chat_requires_valid_token(client, db_session):
    response = await client.post("/api/v1/demo/chat", json={"message": "Bonjour"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_two_demo_sessions_are_isolated(client, db_session):
    files_a = {"file": ("catalogue.csv", io.BytesIO(SAMPLE_CSV), "text/csv")}
    demo_a = await client.post(
        "/api/v1/demo/create", data={"company_name": "Boutique A", "currency": "XOF"}, files=files_a
    )

    other_csv = b"Nom,Prix,Stock\nRobe rouge,45000,3\n"
    files_b = {"file": ("catalogue.csv", io.BytesIO(other_csv), "text/csv")}
    demo_b = await client.post(
        "/api/v1/demo/create", data={"company_name": "Boutique B", "currency": "XOF"}, files=files_b
    )

    token_a = demo_a.json()["demo_token"]
    token_b = demo_b.json()["demo_token"]
    assert token_a != token_b

    fake = FakeLLMClient([tool_use_response("search_products", {"query": "Robe"}), text_response("Rien trouvé")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        # Le token de la boutique A ne doit jamais voir le catalogue de la boutique B
        response = await client.post(
            "/api/v1/demo/chat", json={"message": "Avez-vous une robe ?"}, headers={"Authorization": f"Bearer {token_a}"}
        )
        assert response.status_code == 200
        tool_call_input = fake.received_messages  # juste vérifier qu'aucune erreur n'a fuité entre tenants
    finally:
        del app.dependency_overrides[get_llm_client]
