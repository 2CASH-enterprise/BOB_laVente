"""
Lot 125 (contrat 1.7, décision de Thérèse du 14/11) — un seul interrupteur pour l'assistant : Bob fait foi, Naomy n'a
qu'un voyant. L'assistant est allumé d'office pour toute liaison avec Naomy ; `assistant_actif` dans la fiche Boutique ;
événement `assistant.modifie` quand le commerçant change l'interrupteur dans Bob.
"""
import json

import pytest
from sqlalchemy import select

from app.models.naomy import NaomyConnection, NaomyEvent
from app.models.tenant import TenantPlan
from app.services import naomy_bridge as nb
from app.tests.test_lot89_connecteur_naomy import _login, _shop, _signed, conforms, naomy_env  # noqa: F401
from app.tests.test_lot118_equipe_naomy import _create
from app.tests.test_lot121_mon_equipe import _pair_with


async def _events(db, tenant_id, kind="assistant.modifie"):
    rows = (await db.execute(select(NaomyEvent).where(NaomyEvent.tenant_id == tenant_id, NaomyEvent.type == kind)
                             .order_by(NaomyEvent.created_at))).scalars().all()
    return [r.data for r in rows]


@pytest.fixture
def assistant(naomy_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(naomy_env, "assistant_enabled", True)
    return naomy_env


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["rencontrer", "page_complete", "page_assistant", "demande"])
async def test_the_assistant_is_on_for_every_link(client, db_session, assistant, path):
    from app.models.naomy import NaomyPairingRequest

    tenant, email = await _shop(db_session, plan=TenantPlan.PRO)
    headers = await _login(client, email)
    if path == "rencontrer":
        url = (await client.post("/api/v1/team/naomy", headers=headers)).json()["url"]
    elif path == "demande":  # après un 409 boutique_existante : le commerçant accepte dans Bob
        request = NaomyPairingRequest(tenant_id=tenant.id, naomy_account="nao_1", shop_name="X", status="PENDING")
        db_session.add(request)
        await db_session.commit()
        r = await client.post(f"/api/v1/naomy-connection/request/{request.id}", headers=headers, json={"accept": True, "scope": "complete"})
        url = r.json()["url"]
    else:
        scope = "complete" if path == "page_complete" else "assistant"
        url = (await client.post("/api/v1/naomy-connection/pairing", headers=headers, json={"scope": scope})).json()["url"]
    paired = await _pair_with(client, url)
    assert paired.status_code == 200
    data = paired.json()
    conforms(data["boutique"], "Boutique")
    assert data["boutique"]["assistant_actif"] is True
    connection = (await db_session.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant.id))).scalar_one()
    assert connection.assistant_enabled and connection.assistant_enabled_at is not None
    shop = (await client.get("/api/v1/naomy/boutique", headers={"Authorization": f"Bearer {data['cle_api']}"})).json()
    assert shop["assistant_actif"] is True
    assert await _events(db_session, tenant.id) == []  # à la liaison, l'état est dans la réponse : pas d'événement


@pytest.mark.asyncio
async def test_an_account_created_by_naomy_has_it_on(client, db_session, assistant):
    data = (await _create(client)).json()
    conforms(data, "CompteCree")
    assert data["boutique"]["assistant_actif"] is True


@pytest.mark.asyncio
async def test_the_merchant_switch_tells_naomy(client, db_session, assistant):
    tenant, email = await _shop(db_session, plan=TenantPlan.PRO)
    headers = await _login(client, email)
    url = (await client.post("/api/v1/team/naomy", headers=headers)).json()["url"]
    key = (await _pair_with(client, url)).json()["cle_api"]
    naomy = {"Authorization": f"Bearer {key}"}
    off = await client.put("/api/v1/naomy-connection/assistant", headers=headers, json={"enabled": False})
    assert off.status_code == 200 and off.json()["assistant_enabled"] is False
    assert (await client.get("/api/v1/naomy/boutique", headers=naomy)).json()["assistant_actif"] is False
    await client.put("/api/v1/naomy-connection/assistant", headers=headers, json={"enabled": False})  # rien ne change : rien ne part
    await client.put("/api/v1/naomy-connection/assistant", headers=headers, json={"summary": False})  # le bilan n'est pas l'interrupteur
    await client.put("/api/v1/naomy-connection/assistant", headers=headers, json={"enabled": True})
    assert await _events(db_session, tenant.id) == [{"actif": False}, {"actif": True}]
    assert (await client.get("/api/v1/naomy/boutique", headers=naomy)).json()["assistant_actif"] is True
    # Les événements partent signés, avec le type et les données du contrat.
    sent = []

    async def sender(url, body, headers):
        sent.append(json.loads(body))
        return 200

    await nb.dispatch(db_session, sender=sender)
    mine = [e for e in sent if e["type"] == "assistant.modifie"]
    assert [e["donnees"] for e in mine] == [{"actif": False}, {"actif": True}]
    for event in mine:
        conforms(event, "Evenement")
        conforms(event["donnees"], "DonneesAssistantModifie")


@pytest.mark.asyncio
async def test_an_assistant_only_link_also_gets_the_event(client, db_session, assistant):
    tenant, email = await _shop(db_session, plan=TenantPlan.FREE)
    headers = await _login(client, email)
    url = (await client.post("/api/v1/naomy-connection/pairing", headers=headers, json={"scope": "assistant"})).json()["url"]
    assert (await _pair_with(client, url)).json()["portee"] == "assistant"
    await client.put("/api/v1/naomy-connection/assistant", headers=headers, json={"enabled": False})
    connection = (await db_session.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant.id))).scalar_one()
    nb.add_event(db_session, tenant.id, "produit.cree", {}, connection_id=connection.id)
    await db_session.commit()
    sent = []

    async def sender(url, body, headers):
        sent.append(json.loads(body)["type"])
        return 200

    await nb.dispatch(db_session, sender=sender)
    assert "assistant.modifie" in sent and "produit.cree" not in sent  # le catalogue reste hors de la portée assistant


@pytest.mark.asyncio
async def test_the_real_state_follows_the_general_switch(client, db_session, naomy_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(naomy_env, "assistant_enabled", False)  # interrupteur général de Bob éteint
    tenant, email = await _shop(db_session, plan=TenantPlan.PRO)
    headers = await _login(client, email)
    url = (await client.post("/api/v1/naomy-connection/pairing", headers=headers, json={"scope": "complete"})).json()["url"]
    data = (await _pair_with(client, url)).json()
    assert data["boutique"]["assistant_actif"] is False
    connection = (await db_session.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant.id))).scalar_one()
    assert connection.assistant_enabled  # prêt pour le jour où l'interrupteur général s'allume
    monkeypatch.setattr(naomy_env, "assistant_enabled", True)
    assert (await nb.shop_view(db_session, tenant))["assistant_actif"] is True


@pytest.mark.asyncio
async def test_a_shop_without_a_link_says_off(db_session, assistant):
    tenant, _ = await _shop(db_session)
    view = await nb.shop_view(db_session, tenant)
    conforms(view, "Boutique")
    assert view["assistant_actif"] is False
