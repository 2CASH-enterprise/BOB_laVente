"""
Lot 121 — « Mon équipe », l'entrée par Bob (décisions du 13/11) : choix « Bob seul / Bob + Naomy » à l'inscription
(rien de présélectionné), rubrique « Mon équipe », « Rencontrer Naomy » en un clic (liaison complète, assistant allumé
d'office, message WhatsApp déjà écrit quand le réglage est allumé), essai équipe de 7 jours pour la formule gratuite
(confirmé par le commerçant, une seule fois), « juste être prévenu sur WhatsApp » gardé, boutiques déjà reliées en équipe.
"""
import uuid
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.naomy import NaomyConnection, NaomyPairingRequest
from app.models.tenant import Tenant, TenantPlan
from app.services import bob_pause, team
from app.services import naomy_bridge as nb
from app.tests.test_lot89_connecteur_naomy import DASH, EVENTS_URL, _login, _shop, _signed, naomy_env  # noqa: F401


async def _pair_with(client, url, number="+2250701020399"):
    """Naomy échange le code du lien (page web ou message WhatsApp) contre la clé."""
    query = parse_qs(urlparse(url).query)
    code = query["code"][0] if "code" in query else query["text"][0].rsplit(": ", 1)[1]
    raw, headers = _signed({"code": code, "compte_naomy": "nao_121", "numero_confirmation": number, "url_evenements": EVENTS_URL})
    return await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)


@pytest.mark.asyncio
async def test_signup_keeps_the_team_choice(client, db_session, unique_email, email_code, naomy_env):
    options = (await client.get("/api/v1/auth/signup-options")).json()
    assert options["team_available"] is True
    base = {"company_name": "Boutique Awa", "country": "CI", "currency": "XOF", "owner_full_name": "Awa", "owner_password": "supersecret123"}
    for choice, expected in (("TEAM", "TEAM"), ("SOLO", "SOLO"), (None, None)):
        email = f"{uuid.uuid4().hex[:6]}{unique_email}"
        body = {**base, "owner_email": email, "verification_code": await email_code(email)}
        if choice:
            body["team"] = choice
        r = await client.post("/api/v1/auth/register-tenant", json=body)
        assert r.status_code == 201, r.text
        tenant = await db_session.get(Tenant, uuid.UUID(r.json()["tenant_id"]))
        assert tenant.team_mode == expected
    bad = {**base, "owner_email": unique_email, "verification_code": await email_code(unique_email), "team": "TRIO"}
    assert (await client.post("/api/v1/auth/register-tenant", json=bad)).status_code == 422


@pytest.mark.asyncio
async def test_no_team_step_while_naomy_is_off(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "naomy_platform_secret", "")
    assert (await client.get("/api/v1/auth/signup-options")).json()["team_available"] is False


@pytest.mark.asyncio
async def test_meeting_naomy_in_one_click_turns_the_assistant_on(client, db_session, naomy_env, monkeypatch):
    monkeypatch.setattr(naomy_env, "assistant_enabled", True)
    tenant, email = await _shop(db_session, plan=TenantPlan.INDEPENDANT)
    headers = await _login(client, email)
    view = (await client.get("/api/v1/team", headers=headers)).json()
    assert view["mode"] is None and view["naomy"]["state"] == "absent" and view["naomy"]["can_meet"] and not view["naomy"]["trial"]
    r = await client.post("/api/v1/team/naomy", headers=headers)
    assert r.status_code == 200
    link = r.json()
    assert link["by_whatsapp"] is False and link["url"].startswith("https://agenc-ai.com/naomy/connexion/bob?code=")
    assert link["team"]["mode"] == "TEAM" and link["trial_until"] is None
    paired = await _pair_with(client, link["url"])
    assert paired.status_code == 200 and paired.json()["portee"] == "complete"
    connection = (await db_session.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant.id))).scalar_one()
    assert connection.assistant_enabled and connection.assistant_enabled_at is not None  # allumé d'office
    view = (await client.get("/api/v1/team", headers=headers)).json()
    assert view["naomy"]["state"] == "linked" and view["naomy"]["can_meet"] is False


@pytest.mark.asyncio
async def test_the_whatsapp_message_is_prefilled_when_the_setting_is_on(client, db_session, naomy_env, monkeypatch):
    monkeypatch.setattr(naomy_env, "naomy_pairing_whatsapp", True)
    tenant, email = await _shop(db_session, plan=TenantPlan.PRO, name="Wax & Co")
    link = (await client.post("/api/v1/team/naomy", headers=await _login(client, email))).json()
    assert link["by_whatsapp"] is True and link["url"].startswith("https://wa.me/33780841269?text=")
    text = parse_qs(urlparse(link["url"]).query)["text"][0]
    assert text.startswith("Bonjour Naomy 👋 Je relie ma boutique « Wax & Co » à Bob.\nCode de liaison : ")
    assert len(text.rsplit(": ", 1)[1]) == nb.CODE_LENGTH
    assert (await _pair_with(client, link["url"])).status_code == 200
    monkeypatch.setattr(naomy_env, "naomy_whatsapp_number", "")  # pas de numéro : le lien habituel
    other, mail = await _shop(db_session, plan=TenantPlan.PRO)
    assert (await client.post("/api/v1/team/naomy", headers=await _login(client, mail))).json()["by_whatsapp"] is False


@pytest.mark.asyncio
async def test_free_plan_gets_a_seven_day_team_trial_only_after_agreeing(client, db_session, naomy_env):
    tenant, email = await _shop(db_session, plan=TenantPlan.FREE)
    headers = await _login(client, email)
    view = (await client.get("/api/v1/team", headers=headers)).json()
    assert view["naomy"]["can_meet"] and view["naomy"]["trial"] is True and view["naomy"]["refusal"]
    r = await client.post("/api/v1/team/naomy", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "essai_a_confirmer"
    await db_session.refresh(tenant)
    assert tenant.plan == TenantPlan.FREE and tenant.paid_until is None and tenant.team_mode is None  # rien n'a changé
    r = await client.post("/api/v1/team/naomy", headers=headers, json={"confirmer_essai": True})
    assert r.status_code == 200
    await db_session.refresh(tenant)
    until = bob_pause.local_today(tenant) + timedelta(days=6)
    assert tenant.plan == TenantPlan.INDEPENDANT and tenant.paid_until == until and tenant.team_trial_at is not None
    assert r.json()["trial_until"] == until.isoformat() and bob_pause.notice_due(tenant) is None
    assert (await _pair_with(client, r.json()["url"])).status_code == 200
    audit = (await db_session.execute(select(AuditLog.action).where(AuditLog.tenant_id == tenant.id))).scalars().all()
    assert "TEAM_TRIAL_STARTED" in audit
    assert (await client.get("/api/v1/team", headers=headers)).json()["trial_until"] == until.isoformat()


@pytest.mark.asyncio
async def test_the_trial_is_offered_only_once_and_never_to_a_paid_or_lapsed_shop(client, db_session, naomy_env):
    used, email = await _shop(db_session, plan=TenantPlan.FREE)
    used.team_trial_at = used.created_at
    lapsed, email2 = await _shop(db_session, plan=TenantPlan.FREE)
    lapsed.paid_until = date.today() - timedelta(days=40)
    await db_session.commit()
    for mail in (email, email2):
        headers = await _login(client, mail)
        view = (await client.get("/api/v1/team", headers=headers)).json()
        assert view["naomy"]["can_meet"] is False and view["naomy"]["trial"] is False
        r = await client.post("/api/v1/team/naomy", headers=headers, json={"confirmer_essai": True})
        assert r.status_code == 403 and "Indépendant" in r.json()["detail"]
    assert team.trial_possible(used) is False and team.trial_possible(lapsed) is False


@pytest.mark.asyncio
async def test_accepting_naomys_request_goes_through_the_team(client, db_session, naomy_env, monkeypatch):
    monkeypatch.setattr(naomy_env, "assistant_enabled", True)
    tenant, email = await _shop(db_session, plan=TenantPlan.STARTER)
    request = NaomyPairingRequest(tenant_id=tenant.id, naomy_account="nao_9", shop_name="Chez Naomy", status="PENDING")
    db_session.add(request)
    await db_session.commit()
    headers = await _login(client, email)
    view = (await client.get("/api/v1/team", headers=headers)).json()
    assert view["naomy"]["state"] == "request" and view["naomy"]["request"]["shop_name"] == "Chez Naomy"
    r = await client.post("/api/v1/team/naomy", headers=headers, json={"request_id": str(request.id)})
    assert r.status_code == 200
    await db_session.refresh(request)
    assert request.status == "ACCEPTED"
    assert (await _pair_with(client, r.json()["url"])).status_code == 200
    again = await client.post("/api/v1/team/naomy", headers=headers, json={"request_id": str(request.id)})
    assert again.status_code == 404  # déjà traitée
    assert (await client.post("/api/v1/team/naomy", headers=headers, json={"request_id": "n'importe"})).status_code == 404


@pytest.mark.asyncio
async def test_just_being_warned_on_whatsapp_stays_possible_and_can_be_upgraded(client, db_session, naomy_env, monkeypatch):
    monkeypatch.setattr(naomy_env, "assistant_enabled", True)
    tenant, email = await _shop(db_session, plan=TenantPlan.STARTER)
    headers = await _login(client, email)
    r = await client.post("/api/v1/naomy-connection/pairing", headers=headers, json={"scope": "assistant"})
    assert (await _pair_with(client, r.json()["url"])).json()["portee"] == "assistant"
    view = (await client.get("/api/v1/team", headers=headers)).json()
    assert view["naomy"]["state"] == "assistant" and view["naomy"]["can_meet"] is True and view["mode"] is None
    link = (await client.post("/api/v1/team/naomy", headers=headers)).json()
    assert (await _pair_with(client, link["url"])).json()["portee"] == "complete"
    view = (await client.get("/api/v1/team", headers=headers)).json()
    assert view["naomy"]["state"] == "linked" and view["mode"] == "TEAM"


@pytest.mark.asyncio
async def test_choosing_and_changing_ones_mind(client, db_session, naomy_env):
    tenant, email = await _shop(db_session)
    headers = await _login(client, email)
    assert (await client.put("/api/v1/team", headers=headers, json={"mode": "TEAM"})).json()["mode"] == "TEAM"
    assert (await client.put("/api/v1/team", headers=headers, json={"mode": "SOLO"})).json()["mode"] == "SOLO"
    assert (await client.put("/api/v1/team", headers=headers, json={"mode": "DUO"})).status_code == 422
    assert (await client.get("/api/v1/team")).status_code == 401


@pytest.mark.asyncio
async def test_a_plain_complete_pairing_puts_the_shop_in_the_team_without_forcing_the_assistant(client, db_session, naomy_env, monkeypatch):
    from app.tests.test_lot89_connecteur_naomy import _pair

    monkeypatch.setattr(naomy_env, "assistant_enabled", True)
    tenant, _ = await _shop(db_session)
    await _pair(client, db_session, tenant)
    await db_session.refresh(tenant)
    connection = (await db_session.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant.id))).scalar_one()
    assert tenant.team_mode == "TEAM" and not connection.assistant_enabled  # l'ancien chemin ne change pas (lot 92)


@pytest.mark.asyncio
async def test_unavailable_for_a_demo_or_while_the_gateway_is_off(client, db_session, naomy_env, monkeypatch):
    tenant, email = await _shop(db_session)
    headers = await _login(client, email)
    monkeypatch.setattr(naomy_env, "naomy_platform_secret", "")
    view = (await client.get("/api/v1/team", headers=headers)).json()
    assert view["naomy"]["state"] == "unavailable" and view["naomy"]["can_meet"] is False and view["naomy"]["trial"] is False
    assert (await client.post("/api/v1/team/naomy", headers=headers, json={"confirmer_essai": True})).status_code == 403


def test_the_dashboard_has_the_team_page_and_the_signup_step():
    assert 'data-tab="team" data-label="Mon équipe"' in DASH and 'id="tab-team"' in DASH
    assert "chooseSignupTeam('SOLO')" in DASH and "chooseSignupTeam('TEAM')" in DASH and "Bob + Naomy" in DASH
    assert 'class="segment-option is-chosen"' not in DASH and "checked" not in DASH[DASH.index('id="signup-team"'):DASH.index('data-step="3"')]
    assert "function signupSteps()" in DASH and "team: signupSteps().includes(2) ? signupState.team : null" in DASH
    assert "Rencontrer Naomy" in DASH and "function meetNaomy(" in DASH and '"/api/v1/team/naomy"' in DASH
    assert "essai_a_confirmer" in DASH and "Bob sera mis en pause" in DASH  # l'essai n'est jamais lancé à l'insu du commerçant
    assert "Juste être prévenu sur WhatsApp" in DASH and "D'autres agents bientôt" in DASH
    assert 'id="team-home"' in DASH and "function loadTeamHome()" in DASH and "Finalement, Bob seul" in DASH
    assert "openFullTab('team')" in DASH and 'id="naomy-pointer"' in DASH
    for word in ("portée", "Ce que vous reliez", "FCFA"):
        assert word not in DASH[DASH.index('id="tab-team"'):DASH.index('id="tab-integrations"')]


@pytest.mark.asyncio
async def test_no_trial_starts_when_the_link_cannot_be_made(client, db_session, naomy_env, monkeypatch):
    tenant, email = await _shop(db_session, plan=TenantPlan.FREE)
    headers = await _login(client, email)
    # Une demande inconnue : rien ne change, l'essai n'est pas consommé.
    r = await client.post("/api/v1/team/naomy", headers=headers, json={"confirmer_essai": True, "request_id": str(uuid.uuid4())})
    assert r.status_code == 404
    await db_session.refresh(tenant)
    assert tenant.plan == TenantPlan.FREE and tenant.paid_until is None and tenant.team_trial_at is None and tenant.team_mode is None
    # Passerelle éteinte : pas d'essai non plus.
    monkeypatch.setattr(naomy_env, "naomy_platform_secret", "")
    r = await client.post("/api/v1/team/naomy", headers=headers, json={"confirmer_essai": True})
    assert r.status_code == 403
    await db_session.refresh(tenant)
    assert tenant.plan == TenantPlan.FREE and tenant.team_trial_at is None
