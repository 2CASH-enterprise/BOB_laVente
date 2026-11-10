"""
Lot 118 — « Mon équipe » par Naomy (contrat 1.4) : POST /comptes crée la boutique Bob d'un commerçant qui choisit
« Naomy + Bob » chez Naomy, la relie aussitôt (portée complète, assistant allumé), essai équipe de 7 jours, lien de
connexion à usage unique qui ouvre « Relier WhatsApp ». Jamais de doublon : boutique existante → 409 et demande
« Accepter / Refuser ». Pré-inscription : même règle que chez Bob. POST /lien-connexion : nouveau lien (5 par 24 h).
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.audit_log import AuditLog
from app.models.magic_login import MagicLogin
from app.models.naomy import NaomyConnection, NaomyPairingRequest
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import bob_pause, magic_login, naomy_accounts
from app.services import naomy_bridge as nb
from app.tests.test_lot89_connecteur_naomy import EVENTS_URL, DASH, _shop, _signed, conforms, naomy_env  # noqa: F401

NUMBER = "+2250701020304"


def _body(**over):
    body = {"compte_naomy": "nao_8f2c", "numero_whatsapp": NUMBER, "nom_boutique": "Maquis Chez Awa",
            "secteur": "RESTAURANT", "pays": "CI", "ville": "Abidjan", "email": "awa@maquis.ci",
            "nom_contact": "Awa Koné", "url_evenements": EVENTS_URL}
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


async def _create(client, **over):
    raw, headers = _signed(_body(**over))
    return await client.post("/api/v1/naomy/comptes", content=raw, headers=headers)


@pytest.fixture
def limiter():
    """Le limiteur en mémoire de conftest est déjà neuf à chaque test."""
    return None


@pytest.mark.asyncio
async def test_a_team_account_is_created_linked_and_on_trial(client, db_session, naomy_env, limiter):
    r = await _create(client)
    assert r.status_code == 201, r.text
    data = r.json()
    conforms(data, "CompteCree")
    assert data["portee"] == "complete" and data["cle_api"].startswith("bnk_") and not data["pre_inscrit"]
    tenant = await db_session.get(Tenant, __import__("uuid").UUID(data["boutique_id"]))
    await db_session.refresh(tenant)
    assert tenant.name == "Maquis Chez Awa" and tenant.business_type == "RESTAURANT" and tenant.business_type_chosen_at
    assert tenant.currency == "XOF" and tenant.phone == NUMBER and tenant.created_via == "NAOMY"
    assert tenant.plan == TenantPlan.INDEPENDANT  # lot 89 : Naomy à partir d'Indépendant
    today = bob_pause.local_today(tenant)
    assert tenant.paid_until == today + timedelta(days=6) and data["essai_jusqu_au"] == tenant.paid_until.isoformat()
    assert bob_pause.notice_due(tenant) is None  # pas de rapport « 7 jours avant » le jour de l'inscription
    assert bob_pause.pause_reason(tenant) is None
    owner = (await db_session.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
    assert owner.role == Role.OWNER and owner.email == "awa@maquis.ci" and owner.full_name == "Awa Koné"
    connection = (await db_session.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant.id))).scalar_one()
    assert connection.scope == "complete" and connection.assistant_enabled and connection.confirmation_number == NUMBER
    shop = data["boutique"]
    assert shop["origine"] == "NAOMY" and shop["whatsapp_connecte"] is False and shop["payee_jusqu_au"] == data["essai_jusqu_au"]
    assert data["lien_connexion"]["url"].startswith("https://agenc-ai.com/bob/dashboard/?connexion=")
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.tenant_id == tenant.id,
                                                             AuditLog.action == "TENANT_CREATED_BY_NAOMY"))).scalar_one()
    assert audit.actor == "NAOMY"
    # La clé marche aussitôt sur l'API habituelle.
    r = await client.get("/api/v1/naomy/boutique", headers={"Authorization": f"Bearer {data['cle_api']}"})
    assert r.status_code == 200 and r.json()["origine"] == "NAOMY"


@pytest.mark.asyncio
async def test_the_login_link_works_once(client, db_session, naomy_env, limiter):
    data = (await _create(client)).json()
    token = data["lien_connexion"]["url"].split("connexion=")[1]
    r = await client.post("/api/v1/auth/magic", json={"token": token})
    assert r.status_code == 200 and r.json()["access_token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200
    again = await client.post("/api/v1/auth/magic", json={"token": token})
    assert again.status_code == 400 and "déjà servi" in again.json()["detail"]
    assert (await client.post("/api/v1/auth/magic", json={"token": "faux"})).status_code == 400
    assert (await client.post("/api/v1/auth/magic", json={})).status_code == 400
    stored = (await db_session.execute(select(MagicLogin.token_hash))).scalars().all()
    assert token not in stored  # seule l'empreinte est gardée


@pytest.mark.asyncio
async def test_expired_mfa_or_disabled_links_are_refused(db_session, naomy_env):
    tenant, email = await _shop(db_session)
    owner = (await db_session.execute(select(User).where(User.tenant_id == tenant.id))).scalar_one()
    now = datetime.now(timezone.utc)
    old = await magic_login.create(db_session, owner, now - timedelta(hours=25))
    await db_session.commit()
    assert await magic_login.consume(db_session, old["url"].split("=")[1]) is None
    fresh = await magic_login.create(db_session, owner, now)
    owner.mfa_enabled = True
    await db_session.commit()
    assert await magic_login.consume(db_session, fresh["url"].split("=")[1]) is None
    owner.mfa_enabled, owner.active = False, False
    assert await magic_login.consume(db_session, fresh["url"].split("=")[1]) is None
    owner.active = True
    assert (await magic_login.consume(db_session, fresh["url"].split("=")[1])).id == owner.id
    assert await magic_login.consume(db_session, "x" * 101) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [{"secteur": "BOULANGERIE"}, {"numero_whatsapp": "0701020304"}, {"email": "pas-un-email"},
                                  {"nom_boutique": "  "}, {"intrus": 1}, {"ville": 12}, {"nom_contact": ...},
                                  {"pays": "ci"}, {"compte_naomy": "x" * 65}])
async def test_bad_bodies_are_refused(client, db_session, naomy_env, limiter, over):
    r = await _create(client, **over)
    assert r.status_code == 400 and r.json()["erreur"] == "requete_invalide"
    assert (await db_session.execute(select(func.count(Tenant.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_signature_country_and_events_address(client, db_session, naomy_env, limiter, monkeypatch):
    raw, headers = _signed(_body(), secret="un-autre-secret")
    assert (await client.post("/api/v1/naomy/comptes", content=raw, headers=headers)).status_code == 401
    raw, headers = _signed(_body(), age=600)
    assert (await client.post("/api/v1/naomy/comptes", content=raw, headers=headers)).status_code == 401
    r = await _create(client, pays="US")
    assert r.status_code == 422 and r.json()["erreur"] == "pays_inconnu"
    r = await _create(client, url_evenements="https://ailleurs.com/naomy/api/bob/x")
    assert r.status_code == 422 and r.json()["erreur"] == "url_evenements_refusee"
    assert (await db_session.execute(select(func.count(Tenant.id)))).scalar_one() == 0
    monkeypatch.setattr(naomy_env, "naomy_platform_secret", "")
    raw, headers = _signed(_body())
    assert (await client.post("/api/v1/naomy/comptes", content=raw, headers=headers)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["whatsapp", "phone", "local_phone", "email", "shop_email"])
async def test_an_existing_shop_gets_the_accept_request_never_a_duplicate(client, db_session, naomy_env, limiter,
                                                                         monkeypatch, where):
    mails = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: mails.append(kw))
    tenant, email = await _shop(db_session, email="vieux@shop.ci")
    if where == "whatsapp":
        db_session.add(WhatsAppAccount(tenant_id=tenant.id, phone_number_id="pn-118", waba_id="w",
                                       display_phone_number="+225 07 01 02 03 04", system_user_token="t"))
    elif where == "phone":
        tenant.phone = "+225 07 01 02 03 04"
    elif where == "local_phone":
        tenant.phone = "07 01 02 03 04"
    elif where == "shop_email":
        tenant.email = "AWA@maquis.ci"
    await db_session.commit()
    over = {"email": "VIEUX@shop.ci"} if where == "email" else {}
    r = await _create(client, **over)
    assert r.status_code == 409 and r.json()["erreur"] == "boutique_existante"
    assert (await db_session.execute(select(func.count(Tenant.id)))).scalar_one() == 1
    [request] = (await db_session.execute(select(NaomyPairingRequest))).scalars().all()
    assert request.tenant_id == tenant.id and request.status == "PENDING" and request.shop_name == "Maquis Chez Awa"
    assert len(mails) == 1 and "Naomy demande à relier" in mails[0]["subject"]
    assert (await _create(client, **over)).status_code == 409  # deuxième essai : pas de deuxième demande
    assert (await db_session.execute(select(func.count(NaomyPairingRequest.id)))).scalar_one() == 1


@pytest.mark.asyncio
async def test_an_already_linked_shop_gets_no_request(client, db_session, naomy_env, limiter):
    from app.tests.test_lot89_connecteur_naomy import _pair

    tenant, _ = await _shop(db_session, email="awa@maquis.ci")
    await _pair(client, db_session, tenant)
    assert (await _create(client)).status_code == 409
    assert (await db_session.execute(select(func.count(NaomyPairingRequest.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_preregistration_same_rule_and_trial_starts_at_opening(client, db_session, naomy_env, limiter, monkeypatch):
    from app.services import preregistration

    async def on(db):
        return True

    sent = []

    async def signup_mails(db, tenant, owner, send=None):
        sent.append(tenant.id)

    monkeypatch.setattr(preregistration, "mode_on", on)
    monkeypatch.setattr(preregistration, "send_signup_emails", signup_mails)
    data = (await _create(client)).json()
    conforms(data, "CompteCree")
    assert data["pre_inscrit"] is True and data["essai_jusqu_au"] is None and data["boutique"]["pre_inscrit"] is True
    tenant = (await db_session.execute(select(Tenant))).scalar_one()
    assert tenant.paid_until is None and sent == [tenant.id]
    assert bob_pause.pause_reason(tenant) == bob_pause.PREREG
    assert await preregistration.open_access(db_session, tenant, send=lambda **kw: None)
    assert tenant.paid_until == bob_pause.local_today(tenant) + timedelta(days=6)  # l'essai démarre à l'ouverture
    # Une boutique venue de Bob n'est jamais mise en essai à l'ouverture.
    other, _ = await _shop(db_session)
    other.prereg_at = datetime.now(timezone.utc)
    assert await preregistration.open_access(db_session, other, send=lambda **kw: None) and other.paid_until is None


@pytest.mark.asyncio
async def test_rate_limits(client, db_session, naomy_env, limiter):
    for i in range(3):
        r = await _create(client, email=f"a{i}@x.ci", nom_boutique=f"B{i}")
        assert r.status_code in (201, 409)
    r = await _create(client, email="a9@x.ci")
    assert r.status_code == 429 and r.json()["erreur"] == "trop_de_requetes"
    assert naomy_accounts.PER_NUMBER == 3 and naomy_accounts.PER_DAY == 200


@pytest.mark.asyncio
async def test_daily_cap(db_session, naomy_env):
    class Full:
        async def is_allowed(self, key, limit, window):
            return not key.endswith(":jour")

    with pytest.raises(nb.NaomyError) as err:
        await naomy_accounts.create_account(db_session, _body(), Full())
    assert err.value.status == 429


@pytest.mark.asyncio
async def test_a_broker_gets_its_default_products(client, db_session, naomy_env, limiter):
    from app.models.product import Product

    data = (await _create(client, secteur="INSURANCE_BROKER")).json()
    count = (await db_session.execute(select(func.count(Product.id)).where(
        Product.tenant_id == __import__("uuid").UUID(data["boutique_id"])))).scalar_one()
    assert count > 0


@pytest.mark.asyncio
async def test_a_new_login_link(client, db_session, naomy_env, limiter):
    data = (await _create(client)).json()
    headers = {"Authorization": f"Bearer {data['cle_api']}"}
    r = await client.post("/api/v1/naomy/lien-connexion", headers=headers)
    assert r.status_code == 200
    conforms(r.json(), "LienConnexion")
    for _ in range(3):
        assert (await client.post("/api/v1/naomy/lien-connexion", headers=headers)).status_code == 200
    r = await client.post("/api/v1/naomy/lien-connexion", headers=headers)  # 1 (création) + 4 = 5 : le 6e est refusé
    assert r.status_code == 429
    assert (await client.post("/api/v1/naomy/lien-connexion")).status_code == 401


@pytest.mark.asyncio
async def test_no_link_when_connected_or_double_check(client, db_session, naomy_env, limiter):
    data = (await _create(client)).json()
    headers = {"Authorization": f"Bearer {data['cle_api']}"}
    tid = __import__("uuid").UUID(data["boutique_id"])
    owner = (await db_session.execute(select(User).where(User.tenant_id == tid))).scalar_one()
    owner.mfa_enabled = True
    await db_session.commit()
    r = await client.post("/api/v1/naomy/lien-connexion", headers=headers)
    assert r.status_code == 409 and r.json()["erreur"] == "double_verification"
    db_session.add(WhatsAppAccount(tenant_id=tid, phone_number_id="pn-118b", waba_id="w",
                                   display_phone_number=NUMBER, system_user_token="t"))
    await db_session.commit()
    r = await client.post("/api/v1/naomy/lien-connexion", headers=headers)
    assert r.status_code == 409 and r.json()["erreur"] == "deja_connecte"
    shop = (await client.get("/api/v1/naomy/boutique", headers=headers)).json()
    assert shop["whatsapp_connecte"] is True


@pytest.mark.asyncio
async def test_a_bob_shop_still_says_bob(client, db_session, naomy_env):
    tenant, _ = await _shop(db_session)
    view = await nb.shop_view(db_session, tenant)
    conforms(view, "Boutique")
    assert view["origine"] == "BOB" and view["pre_inscrit"] is False and view["payee_jusqu_au"] is None
    tenant.paid_until = date(2026, 12, 31)
    assert (await nb.shop_view(db_session, tenant))["payee_jusqu_au"] == "2026-12-31"


def test_the_dashboard_exchanges_the_link_and_opens_whatsapp():
    assert 'get("connexion")' in DASH and '"/api/v1/auth/magic"' in DASH
    assert 'params.delete("connexion")' in DASH and 'pendingTab = "integrations"' in DASH
