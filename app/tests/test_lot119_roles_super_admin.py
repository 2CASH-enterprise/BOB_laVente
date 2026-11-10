"""
Lot 119 — rôles de l'équipe AgenC'AI dans le Super Admin : Propriétaire, Commercial, Marketing, Technique, Facturation,
Support, Lecture seule (plusieurs par personne, droits additionnés), vérifiés par le serveur sur chaque route ; chiffres
des boutiques pour Propriétaire et Facturation seulement ; invitation par e-mail (48 h, usage unique) ; jamais moins
d'un Propriétaire actif ; journal de qui a fait quoi (consultations et exports de prospects compris).
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.security import create_superadmin_access_token, hash_password, verify_password
from app.main import app
from app.models.audit_log import AuditLog
from app.models.monitoring import MonitorIssue
from app.models.prospect import Prospect
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan
from app.services import sa_roles

HTML = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")
PUBLIC = {("POST", "/api/v1/superadmin/bootstrap"), ("POST", "/api/v1/superadmin/login"),
          ("POST", "/api/v1/superadmin/login/verify"), ("POST", "/api/v1/superadmin/login/resend"),
          ("POST", "/api/v1/superadmin/team/accept")}


async def _member(db, *roles, name="Awa Koné", active=True):
    m = SuperAdminUser(email=f"{uuid.uuid4().hex[:8]}@agencai.ci", full_name=name, hashed_password=hash_password("motdepasse-123"),
                       roles=list(roles), active=active)
    db.add(m)
    await db.commit()
    return m


def _h(member):
    return {"Authorization": f"Bearer {create_superadmin_access_token(member.id)}"}


async def _shop(db, name="Maquis Awa", **fields):
    t = Tenant(name=name, country="CI", currency="XOF", email=f"{uuid.uuid4().hex[:6]}@shop.ci", phone="+2250700000001",
               plan=TenantPlan.PRO, commission_rate=5, paid_until=date.today() + timedelta(days=3), **fields)
    db.add(t)
    await db.commit()
    return t


def _issue(title):
    now = datetime.now(timezone.utc)
    return MonitorIssue(signature=uuid.uuid4().hex, source="SEND_EMAIL", title=title, count=3, first_seen=now, last_seen=now)


def _routes(routes):
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":
            yield from _routes(r.original_router.routes)
        elif isinstance(r, APIRoute):
            yield r


def test_every_super_admin_route_checks_a_right():
    seen = set()
    for r in _routes(app.routes):
        if "/superadmin" not in r.path:
            continue
        perms = [d.call.sa_perm for d in r.dependant.dependencies if hasattr(d.call, "sa_perm")]
        for method in r.methods:
            seen.add((method, r.path))
            assert perms or (method, r.path) in PUBLIC, (method, r.path)
            assert all(p in sa_roles.PERMS for p in perms)
    assert PUBLIC <= seen and len(seen) > 70


def test_the_rights_table():
    owner = set(sa_roles.perms_for(["OWNER"]))
    assert owner == set(sa_roles.PERMS)
    ro = set(sa_roles.perms_for(["READONLY"]))
    assert not {p for p in ro if p.endswith(".edit")} and not ro & {"shops.figures", "shops.billing", "shops.suspend", "team.manage"}
    sales = set(sa_roles.perms_for(["SALES"]))
    assert {"prospects.edit", "prereg.edit", "campaigns.edit", "shops.basic"} <= sales
    assert not sales & {"shops.view", "shops.figures", "aicosts.view", "tech.view"}
    assert "shops.basic" not in sa_roles.perms_for(["MARKETING"])
    assert set(sa_roles.perms_for(["SALES", "TECH"])) == sales | set(sa_roles.perms_for(["TECH"]))  # droits additionnés
    assert [p for p in sa_roles.PERMS if sa_roles.can(["BILLING"], p) and p.startswith("shops.")] == [
        "shops.basic", "shops.view", "shops.figures", "shops.billing", "shops.suspend"]
    assert sa_roles.clean(["TECH", "OWNER", "INCONNU", "TECH"]) == ["OWNER", "TECH"]


@pytest.mark.asyncio
async def test_a_new_member_defaults_to_read_only(db_session):
    m = SuperAdminUser(email="nouveau@agencai.ci", full_name="N", hashed_password="x")
    db_session.add(m)
    await db_session.commit()
    assert m.roles == ["READONLY"]


@pytest.mark.asyncio
async def test_sales_sees_names_and_prospects_never_shop_figures(client, db_session):
    sales = await _member(db_session, "SALES")
    shop = await _shop(db_session)
    rows = (await client.get("/api/v1/superadmin/tenants", headers=_h(sales))).json()
    [row] = [r for r in rows if r["id"] == str(shop.id)]
    assert row["name"] == "Maquis Awa" and row["business_type"] and row["country"] == "CI" and row["bob_status"]
    assert row["email"] is None and row["phone"] is None and row["plan"] is None and row["paid_until"] is None
    assert row["commission_rate"] is None and row["conversation_count_this_month"] is None
    for method, path in [("get", f"/tenants/{shop.id}"), ("get", "/ai-costs"), ("get", "/stats"), ("get", "/monitoring"),
                         ("get", "/team"), ("get", "/plans"), ("get", f"/tenants/{shop.id}/history")]:
        assert (await getattr(client, method)("/api/v1/superadmin" + path, headers=_h(sales))).status_code == 403, path
    r = await client.put(f"/api/v1/superadmin/tenants/{shop.id}/plan", headers=_h(sales), json={"plan": "FREE"})
    assert r.status_code == 403 and "rôle" in r.json()["detail"]
    assert (await client.get("/api/v1/superadmin/prospects", headers=_h(sales))).status_code == 200
    r = await client.post("/api/v1/superadmin/prospects", headers=_h(sales), json={"company": "Cabinet K", "sector": "INSURANCE_BROKER"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert (await client.get(f"/api/v1/superadmin/prospects/{pid}", headers=_h(sales))).status_code == 200
    assert (await client.get("/api/v1/superadmin/prospects/export.xlsx", headers=_h(sales))).status_code == 200
    actions = (await db_session.execute(select(AuditLog.action).where(AuditLog.actor == f"superadmin:{sales.id}"))).scalars().all()
    assert "PROSPECT_VIEWED" in actions and "PROSPECTS_EXPORTED" in actions  # consultations et exports notés
    assert (await client.get("/api/v1/superadmin/preregistration", headers=_h(sales))).status_code == 200


@pytest.mark.asyncio
async def test_billing_support_and_tech_see_what_they_need(client, db_session):
    shop = await _shop(db_session)
    billing, support, tech = await _member(db_session, "BILLING"), await _member(db_session, "SUPPORT"), await _member(db_session, "TECH")
    url = f"/api/v1/superadmin/tenants/{shop.id}"
    full = (await client.get(url, headers=_h(billing))).json()
    assert full["commission_rate"] == 5 and full["order_count"] == 0 and full["total_commission_due"] == 0 and full["plan"] == "PRO"
    for member in (support, tech):
        d = (await client.get(url, headers=_h(member))).json()
        assert d["plan"] == "PRO" and d["email"] and d["paid_until"]  # fiche complète…
        assert d["commission_rate"] is None and d["customer_count"] is None and d["order_count"] is None \
            and d["total_commission_due"] is None  # … sans les chiffres
    assert (await client.put(url + "/plan", headers=_h(billing), json={"plan": "STARTER"})).status_code == 200
    assert (await client.put(url + "/commission-rate", headers=_h(billing), json={"commission_rate": 3})).status_code == 200
    assert (await client.put(url + "/plan", headers=_h(support), json={"plan": "PRO"})).status_code == 403
    assert (await client.put(url + "/active", headers=_h(support), json={"active": False})).status_code == 200
    assert (await client.put(url + "/business-type", headers=_h(support), json={"business_type": "RESTAURANT"})).status_code == 200
    assert (await client.put(url + "/business-type", headers=_h(billing), json={"business_type": "ONLINE_STORE"})).status_code == 403
    assert (await client.put(url + "/active", headers=_h(tech), json={"active": True})).status_code == 403
    assert (await client.get("/api/v1/superadmin/monitoring", headers=_h(tech))).status_code == 200
    assert (await client.get("/api/v1/superadmin/monitoring", headers=_h(billing))).status_code == 403
    assert (await client.get("/api/v1/superadmin/ai-costs", headers=_h(tech))).status_code == 200
    assert (await client.get("/api/v1/superadmin/prospects", headers=_h(tech))).status_code == 403
    assert (await client.get("/api/v1/superadmin/plans", headers=_h(billing))).status_code == 200
    history = (await client.get(url + "/history", headers=_h(billing))).json()["items"]
    assert any(h["by"] == "Awa Koné" for h in history)  # tracé au nom de la personne


@pytest.mark.asyncio
async def test_read_only_and_marketing(client, db_session):
    ro, mk = await _member(db_session, "READONLY"), await _member(db_session, "MARKETING")
    issue = _issue("x")
    db_session.add(issue)
    await db_session.commit()
    for path in ("/tenants", "/prospects", "/prospection/email", "/preregistration", "/monitoring", "/plans", "/ai-costs"):
        assert (await client.get("/api/v1/superadmin" + path, headers=_h(ro))).status_code == 200, path
    blocked = [("put", "/plans", {"quotas": {}, "mode": "CONTINUE"}), ("post", "/prospects", {"company": "X"}),
               ("put", "/preregistration/settings", {"enabled": True}), ("post", f"/monitoring/issues/{issue.id}/status", {"status": "RESOLVED"}),
               ("post", "/prospection/campaigns", {}), ("post", "/team/invite", {"email": "a@b.ci", "full_name": "A B", "roles": ["OWNER"]})]
    for method, path, body in blocked:
        r = await getattr(client, method)("/api/v1/superadmin" + path, headers=_h(ro), json=body)
        assert r.status_code == 403, path
    assert (await client.get("/api/v1/superadmin/prospection/email", headers=_h(mk))).status_code == 200
    assert (await client.get("/api/v1/superadmin/tenants", headers=_h(mk))).status_code == 403
    assert (await client.post("/api/v1/superadmin/prospects", headers=_h(mk), json={"company": "X"})).status_code == 403
    assert (await client.get("/api/v1/superadmin/prospects", headers=_h(mk))).status_code == 200


@pytest.mark.asyncio
async def test_overview_only_shows_what_the_role_covers(client, db_session):
    await _shop(db_session)  # échéance dans 3 jours → « À faire » Facturation
    now = datetime.now(timezone.utc)
    db_session.add_all([Prospect(company="Auto Plus", code="fol119", status="ACTIVE", sector="CAR_DEALERSHIP",
                                 next_action_on=date.today() - timedelta(days=1), contacted_at=now - timedelta(days=2)),
                        _issue("SMTP")])
    await db_session.commit()
    sales, tech, owner = await _member(db_session, "SALES"), await _member(db_session, "TECH"), await _member(db_session, "OWNER")
    s = (await client.get("/api/v1/superadmin/overview", headers=_h(sales))).json()
    assert {t["kind"] for t in s["todo"]} == {"FOLLOW_UP"} and "ai_cost" not in s["kpis"] and "monitoring_new" not in s["kpis"]
    assert s["funnel"] is not None and s["kpis"]["follow_up"] == 1
    t = (await client.get("/api/v1/superadmin/overview", headers=_h(tech))).json()
    assert {x["kind"] for x in t["todo"]} == {"MONITORING"} and t["funnel"] is None and "follow_up" not in t["kpis"]
    o = (await client.get("/api/v1/superadmin/overview", headers=_h(owner))).json()
    assert {"FOLLOW_UP", "MONITORING", "BILLING"} <= {x["kind"] for x in o["todo"]} and "ai_cost" in o["kpis"]


@pytest.mark.asyncio
async def test_me_gives_rights_and_labels(client, db_session):
    m = await _member(db_session, "TECH", "SALES")
    me = (await client.get("/api/v1/superadmin/me", headers=_h(m))).json()
    assert me["roles"] == ["SALES", "TECH"] and me["role_labels"] == ["Commercial", "Technique"]
    assert "tech.edit" in me["perms"] and "prospects.edit" in me["perms"] and "team.manage" not in me["perms"]


@pytest.mark.asyncio
async def test_invitation_by_email(client, db_session, monkeypatch):
    mails = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: mails.append(kw) or True)
    owner = await _member(db_session, "OWNER", name="Thérèse")
    body = {"email": " Koffi@AgencAI.ci ", "full_name": "Koffi  Yao", "roles": ["SALES", "MARKETING"]}
    r = await client.post("/api/v1/superadmin/team/invite", headers=_h(owner), json=body)
    assert r.status_code == 201 and r.json()["status"] == "INVITED" and r.json()["email_sent"]
    assert r.json()["roles"] == ["SALES", "MARKETING"] and r.json()["full_name"] == "Koffi Yao"
    assert (await client.post("/api/v1/superadmin/team/invite", headers=_h(owner), json=body)).status_code == 409
    [mail] = mails
    assert mail["to"] == "koffi@agencai.ci" and "Thérèse" in mail["body"] and "Commercial, Marketing" in mail["body"]
    token = re.search(r"invitation=([\w-]+)", mail["body"]).group(1)
    member = (await db_session.execute(select(SuperAdminUser).where(SuperAdminUser.email == "koffi@agencai.ci"))).scalar_one()
    assert member.invite_token_hash != token  # empreinte seule
    # Avant d'accepter : impossible de se connecter (mot de passe inconnu).
    r = await client.post("/api/v1/superadmin/login", data={"username": "koffi@agencai.ci", "password": "motdepasse-123"})
    assert r.status_code == 401
    assert (await client.post("/api/v1/superadmin/team/accept", json={"token": token, "password": "court"})).status_code == 422
    assert (await client.post("/api/v1/superadmin/team/accept", json={"token": "faux", "password": "un-bon-mot-de-passe"})).status_code == 400
    assert (await client.post("/api/v1/superadmin/team/accept", json={"token": token, "password": "un-bon-mot-de-passe"})).status_code == 204
    await db_session.refresh(member)
    assert verify_password("un-bon-mot-de-passe", member.hashed_password) and member.invite_token_hash is None
    assert (await client.post("/api/v1/superadmin/team/accept", json={"token": token, "password": "un-autre-mot-de-passe"})).status_code == 400
    assert (await client.post(f"/api/v1/superadmin/team/{member.id}/reinvite", headers=_h(owner))).status_code == 409
    team = (await client.get("/api/v1/superadmin/team", headers=_h(owner))).json()
    assert {m["email"]: m["status"] for m in team["members"]}["koffi@agencai.ci"] == "ACTIVE" and len(team["roles"]) == 7
    journal = (await client.get("/api/v1/superadmin/team/journal", headers=_h(owner))).json()["items"]
    whats = [(i["who"], i["what"]) for i in journal]
    assert ("Thérèse", "a invité Koffi Yao") in whats and ("Koffi Yao", "a rejoint l'équipe") in whats


@pytest.mark.asyncio
async def test_expired_or_disabled_invitations(client, db_session, monkeypatch):
    mails = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: mails.append(kw) or True)
    owner = await _member(db_session, "OWNER")
    for bad in ([], ["CHEF"], ["OWNER", "CHEF"]):
        r = await client.post("/api/v1/superadmin/team/invite", headers=_h(owner), json={"email": "x@y.ci", "full_name": "X Y", "roles": bad})
        assert r.status_code == 422
    r = await client.post("/api/v1/superadmin/team/invite", headers=_h(owner), json={"email": "x@y.ci", "full_name": "X Y", "roles": ["TECH"]})
    member = await db_session.get(SuperAdminUser, uuid.UUID(r.json()["id"]))
    first = re.search(r"invitation=([\w-]+)", mails[-1]["body"]).group(1)
    member.invite_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()
    assert (await client.post("/api/v1/superadmin/team/accept", json={"token": first, "password": "un-bon-mot-de-passe"})).status_code == 400
    assert (await client.post(f"/api/v1/superadmin/team/{member.id}/reinvite", headers=_h(owner))).status_code == 200
    second = re.search(r"invitation=([\w-]+)", mails[-1]["body"]).group(1)
    assert second != first
    assert (await client.put(f"/api/v1/superadmin/team/{member.id}", headers=_h(owner), json={"active": False})).status_code == 200
    assert (await client.post("/api/v1/superadmin/team/accept", json={"token": second, "password": "un-bon-mot-de-passe"})).status_code == 400
    # Réactivé sans nouvelle invitation : l'ancien lien ne sert plus.
    assert (await client.put(f"/api/v1/superadmin/team/{member.id}", headers=_h(owner), json={"active": True})).status_code == 200
    assert (await client.post("/api/v1/superadmin/team/accept", json={"token": second, "password": "un-bon-mot-de-passe"})).status_code == 400


@pytest.mark.asyncio
async def test_always_one_owner_and_changes_apply_at_once(client, db_session):
    owner = await _member(db_session, "OWNER", name="Thérèse")
    other = await _member(db_session, "SALES", "TECH", name="Koffi")
    url = f"/api/v1/superadmin/team/{owner.id}"
    r = await client.put(url, headers=_h(owner), json={"roles": ["SALES"]})
    assert r.status_code == 409 and "Propriétaire" in r.json()["detail"]
    assert (await client.put(url, headers=_h(owner), json={"active": False})).status_code == 409  # pas soi-même
    # Un rôle retiré l'est tout de suite (relu à chaque requête).
    assert (await client.get("/api/v1/superadmin/monitoring", headers=_h(other))).status_code == 200
    assert (await client.put(f"/api/v1/superadmin/team/{other.id}", headers=_h(owner), json={"roles": ["SALES"]})).status_code == 200
    assert (await client.get("/api/v1/superadmin/monitoring", headers=_h(other))).status_code == 403
    # Désactivé : plus aucun accès, tout de suite.
    assert (await client.put(f"/api/v1/superadmin/team/{other.id}", headers=_h(owner), json={"active": False})).status_code == 200
    assert (await client.get("/api/v1/superadmin/me", headers=_h(other))).status_code == 401
    # Un second Propriétaire permet au premier de changer de rôle ; un Propriétaire désactivé ne compte pas.
    second = await _member(db_session, "OWNER", name="Associé")
    r = await client.put(url, headers=_h(owner), json={"active": False})  # même avec un autre Propriétaire
    assert r.status_code == 409 and "propre accès" in r.json()["detail"]
    assert (await client.put(url, headers=_h(owner), json={"roles": ["OWNER", "BILLING"]})).status_code == 200
    second.active = False
    await db_session.commit()
    assert (await client.put(url, headers=_h(owner), json={"roles": ["BILLING"]})).status_code == 409
    second.active = True
    await db_session.commit()
    assert (await client.put(url, headers=_h(owner), json={"roles": ["BILLING"]})).status_code == 200
    assert (await client.get("/api/v1/superadmin/team", headers=_h(owner))).status_code == 403
    assert (await client.put(f"/api/v1/superadmin/team/{second.id}", headers=_h(second), json={"roles": ["TECH"]})).status_code == 409
    journal = (await client.get("/api/v1/superadmin/team/journal", headers=_h(second))).json()["items"]
    assert any(i["what"] == "a changé les rôles de Koffi" for i in journal) and any(i["what"] == "a désactivé l'accès de Koffi" for i in journal)


@pytest.mark.asyncio
async def test_bootstrap_makes_an_owner_and_old_colleague_route_is_read_only(client, db_session):
    r = await client.post("/api/v1/superadmin/bootstrap", json={"email": "premier@agencai.ci", "password": "motdepasse-123", "full_name": "P"})
    assert r.status_code == 200
    first = (await db_session.execute(select(SuperAdminUser))).scalar_one()
    assert first.roles == ["OWNER"]
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/api/v1/superadmin/create-colleague", headers=headers,
                          json={"email": "c@agencai.ci", "password": "motdepasse-123", "full_name": "C"})
    assert r.status_code == 200
    colleague = (await db_session.execute(select(SuperAdminUser).where(SuperAdminUser.email == "c@agencai.ci"))).scalar_one()
    assert colleague.roles == ["READONLY"]
    sales = await _member(db_session, "SALES")
    assert (await client.post("/api/v1/superadmin/create-colleague", headers=_h(sales),
                              json={"email": "d@agencai.ci", "password": "motdepasse-123", "full_name": "D"})).status_code == 403


def test_the_console_hides_what_the_role_cannot_do():
    perms = re.search(r"const ALL_PERMS = \[(.*?)\];", HTML, re.S).group(1)
    assert sorted(re.findall(r'"([a-z.]+)"', perms)) == sorted(sa_roles.PERMS)
    for section, perm in (("shops", "shops.basic"), ("prospects", "prospects.view"), ("prereg", "prereg.view"),
                          ("aicosts", "aicosts.view"), ("monitoring", "tech.view"), ("team", "team.manage")):
        assert f'data-section="{section}" data-perm="{perm}"' in HTML
    for handler, perm in (("savePlans", "plans.edit"), ("toggleActive", "shops.suspend"), ("extendShop", "shops.billing"),
                          ("setIssueStatus", "tech.edit"), ("exportProspects", "prospects.edit"), ("saveCampaign", "campaigns.edit"),
                          ("setPreregMode", "prereg.edit")):
        assert re.search(rf'data-perm="{re.escape(perm)}" onclick="{handler}\(', HTML), handler
    assert 'api("/api/v1/superadmin/me")' in HTML and "function applyPerms()" in HTML
    assert 'id="invite-form"' in HTML and 'get("invitation")' in HTML and "/team/accept" in HTML
    assert 'id="team-section"' in HTML and "function inviteMember()" in HTML and "/team/journal" in HTML
