"""
Lot 73 c — la connexion WhatsApp ne « disparaît » plus : carte toujours visible (connecté → numéro, nom, reconnexion ;
à vérifier → Réessayer), reconnexion ou changement de numéro sur demande explicite, bandeau sur l'accueil.
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.integrations.whatsapp.dependency import get_meta_oauth_client
from app.main import app
from app.models.audit_log import AuditLog
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.tests.test_embedded_signup import FakeMetaOAuthClient

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
CALLBACK = "/api/v1/whatsapp/embedded-signup/callback"


async def _shop(db, email, is_demo=False):
    tenant = Tenant(name="Boutique", country="CI", currency="XOF", email=email, is_demo=is_demo)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Awa", role=Role.OWNER))
    await db.commit()
    return tenant.id


async def _headers(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _connect(client, headers, fake, **body):
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        return await client.post(CALLBACK, headers=headers, json={"code": "c", "waba_id": "w1", **body})
    finally:
        del app.dependency_overrides[get_meta_oauth_client]


@pytest.mark.asyncio
async def test_reconnect_needs_explicit_choice_then_renews_or_replaces(client, db_session, unique_email):
    tenant_id = await _shop(db_session, unique_email)
    headers = await _headers(client, unique_email)
    assert (await _connect(client, headers, FakeMetaOAuthClient(token="t1"), phone_number_id="p1")).status_code == 200
    await client.put("/api/v1/whatsapp/account/display-phone-number", headers=headers,
                     json={"display_phone_number": "+225 07 00 00 00 01"})

    # Sans choix explicite : refusé comme avant
    again = await _connect(client, headers, FakeMetaOAuthClient(token="t2"), phone_number_id="p1")
    assert again.status_code == 409

    # Même numéro : nouveau jeton, même ligne, numéro affiché gardé
    renewed = await _connect(client, headers, FakeMetaOAuthClient(token="t2"), phone_number_id="p1", replace=True)
    assert renewed.status_code == 200, renewed.text
    rows = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))).scalars().all()
    assert len(rows) == 1
    await db_session.refresh(rows[0])
    first_id = rows[0].id
    assert rows[0].system_user_token == "t2" and rows[0].display_phone_number == "+2250700000001"

    # Autre numéro : tout est remplacé (numéro affiché à ressaisir, nom à revérifier)
    changed = await _connect(client, headers, FakeMetaOAuthClient(token="t3"), phone_number_id="p2", waba_id="w2", replace=True)
    assert changed.status_code == 200 and changed.json()["phone_number_id"] == "p2"
    db_session.expire_all()
    row = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))).scalar_one()
    assert row.id == first_id and row.waba_id == "w2" and row.display_phone_number is None
    assert row.display_name_status == "PENDING" and row.system_user_token == "t3"
    actions = [a.action for a in (await db_session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id))).scalars()]
    assert actions.count("WHATSAPP_RECONNECTED") == 2 and actions.count("WHATSAPP_CONNECTED") == 1


@pytest.mark.asyncio
async def test_cannot_take_the_number_of_another_shop_nor_lose_ours_on_failure(client, db_session, unique_email):
    other = f"o{uuid.uuid4().hex[:6]}@x.ci"
    await _shop(db_session, unique_email)
    await _shop(db_session, other)
    mine, theirs = await _headers(client, unique_email), await _headers(client, other)
    await _connect(client, theirs, FakeMetaOAuthClient(token="x"), phone_number_id="pX")
    await _connect(client, mine, FakeMetaOAuthClient(token="m1"), phone_number_id="pM")

    taken = await _connect(client, mine, FakeMetaOAuthClient(token="m2"), phone_number_id="pX", replace=True)
    assert taken.status_code == 409 and "autre compte Bob" in taken.json()["detail"]
    failed = await _connect(client, mine, FakeMetaOAuthClient(raise_on_exchange=True), phone_number_id="pN", replace=True)
    assert failed.status_code == 400
    account = (await client.get("/api/v1/whatsapp/account", headers=mine)).json()
    assert account["phone_number_id"] == "pM"  # rien n'a bougé


@pytest.mark.asyncio
async def test_home_says_whether_whatsapp_is_connected(client, db_session, unique_email):
    await _shop(db_session, unique_email)
    headers = await _headers(client, unique_email)
    assert (await client.get("/api/v1/analytics/home", headers=headers)).json()["whatsapp_connected"] is False
    await _connect(client, headers, FakeMetaOAuthClient(), phone_number_id="pH")
    assert (await client.get("/api/v1/analytics/home", headers=headers)).json()["whatsapp_connected"] is True

    demo = f"demo-{uuid.uuid4().hex[:6]}@bob-demo.internal"
    await _shop(db_session, demo, is_demo=True)
    assert "whatsapp_connected" not in (await client.get("/api/v1/analytics/home", headers=await _headers(client, demo))).json()


def test_dashboard_card_states_and_banner():
    html = DASH[DASH.index('id="wa-card"'):DASH.index('id="wa-templates-card"')]
    assert 'id="wa-connect-choices" class="hidden"' in html  # plus de clignotement : caché tant que l'état n'est pas connu
    assert 'id="btn-wa-reconnect"' in html and 'id="btn-wa-retry"' in html and 'id="wa-number"' in html
    js = DASH[DASH.index("async function loadWhatsAppStatus()"):DASH.index("// Lot 68 — modèles de message WhatsApp")]
    assert "e.status === 404" in js and "Impossible de vérifier" in js and "bobConfirm(" in js
    assert "esc(label)" in js and "Connecté — numéro" not in DASH
    assert "replace: waReplace," in DASH and "err.status = r.status;" in DASH
    home = DASH[DASH.index("function renderHome(h)"):]
    assert "h.whatsapp_connected === false" in home and 'onclick="openWhatsAppCard()"' in home
