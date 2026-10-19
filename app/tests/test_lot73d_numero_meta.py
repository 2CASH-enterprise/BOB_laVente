"""
Lot 73 d — numéro affiché, nom vérifié et statut du nom demandés à Meta (au branchement, sur la page, chaque nuit),
au lieu d'un « en cours de vérification » qui ne changeait jamais.
"""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.integrations.whatsapp.dependency import get_meta_oauth_client
from app.main import app
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import monitoring, wa_account_info
from app.tests.test_embedded_signup import FakeMetaOAuthClient

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


class FakeInfo(FakeMetaOAuthClient):
    def __init__(self, info=None, fail=False, **kw):
        super().__init__(**kw)
        self.info, self.fail, self.calls = info or {}, fail, []

    async def phone_number_info(self, phone_number_id, access_token):
        self.calls.append((phone_number_id, access_token))
        if self.fail:
            raise RuntimeError("Meta injoignable")
        return self.info


async def _shop(db, email):
    tenant = Tenant(name="MS4 Africa", country="CI", currency="XOF", email=email)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Awa", role=Role.OWNER))
    await db.commit()
    return tenant.id


async def _headers(client, email):
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_connection_fetches_number_and_name_from_meta(client, db_session, unique_email):
    await _shop(db_session, unique_email)
    headers = await _headers(client, unique_email)
    fake = FakeInfo({"display_phone_number": "+225 07 00 00 00 01", "verified_name": "MS4 Africa", "name_status": "APPROVED"},
                    token="tok")
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        r = await client.post("/api/v1/whatsapp/embedded-signup/callback", headers=headers,
                              json={"code": "c", "waba_id": "w", "phone_number_id": "p1"})
    finally:
        del app.dependency_overrides[get_meta_oauth_client]
    body = r.json()
    assert r.status_code == 200 and fake.calls == [("p1", "tok")]
    assert (body["display_phone_number"], body["display_name"], body["display_name_status"]) == (
        "+2250700000001", "MS4 Africa", "APPROVED")


@pytest.mark.asyncio
async def test_refresh_endpoint_is_limited_and_keeps_manual_number(client, db_session, unique_email):
    tenant_id = await _shop(db_session, unique_email)
    headers = await _headers(client, unique_email)
    db_session.add(WhatsAppAccount(tenant_id=tenant_id, waba_id="w", phone_number_id="p9", system_user_token="t",
                                   display_phone_number="+2250500000000"))
    await db_session.commit()
    fake = FakeInfo({"display_phone_number": "+225 07 00 00 00 01", "verified_name": "MS4", "name_status": "DECLINED"})
    app.dependency_overrides[get_meta_oauth_client] = lambda: fake
    try:
        first = (await client.post("/api/v1/whatsapp/account/refresh", headers=headers)).json()
        await client.post("/api/v1/whatsapp/account/refresh", headers=headers)  # dans les 5 minutes : Meta pas rappelé
    finally:
        del app.dependency_overrides[get_meta_oauth_client]
    assert len(fake.calls) == 1
    assert first["display_phone_number"] == "+2250500000000"  # saisi à la main : jamais écrasé
    assert first["display_name_status"] == "REJECTED" and first["display_name"] == "MS4"


@pytest.mark.asyncio
async def test_meta_errors_never_break_anything(db_session, unique_email):
    tenant_id = await _shop(db_session, unique_email)
    account = WhatsAppAccount(tenant_id=tenant_id, waba_id="w", phone_number_id="p5", system_user_token="t")
    db_session.add(account)
    await db_session.commit()
    assert await wa_account_info.refresh(db_session, account, FakeInfo(fail=True)) is False
    assert account.display_name_status == "PENDING"
    for meta, ours in [("AVAILABLE_WITHOUT_REVIEW", "APPROVED"), ("EXPIRED", "REJECTED"), ("PENDING_REVIEW", "PENDING")]:
        await wa_account_info.refresh(db_session, account, FakeInfo({"name_status": meta}))
        assert account.display_name_status == ours
    report = await wa_account_info.refresh_all(db_session, FakeInfo({"name_status": "APPROVED"}))
    assert report["refreshed"] >= 1
    stored = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.id == account.id))).scalar_one()
    assert stored.display_name_status == "APPROVED"


def test_nightly_task_and_card():
    from app.workers.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["wa-accounts-refresh"]
    assert entry["task"] in monitoring.TASK_LABELS
    js = DASH[DASH.index("async function loadWhatsAppStatus()"):DASH.index("// Lot 68 — modèles de message WhatsApp")]
    assert '"/api/v1/whatsapp/account/refresh"' in js and "renderWaAccount(fresh, false)" in js
    assert "esc(account.display_name)" in js and "Numéro en cours de récupération auprès de Meta" in js
