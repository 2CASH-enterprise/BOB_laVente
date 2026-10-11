"""
Lot 69 — plan de prospection : emails aussi pour les prospects déjà contactés sur WhatsApp, compteur des places de
l'offre de lancement, entonnoir par pays, vue « Aujourd'hui » ; plafonds des formules (alertes à 80 % et 100 %,
Bob ne s'arrête pas sauf mode « pause »).
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.preregistration import PlatformSettings
from app.models.prospect import Prospect, ProspectEmail, ProspectEvent
from app.models.tenant import Tenant, TenantPlan
from app.services import plan_quota, preregistration as pre, prospect_mailer as mailer, prospection as pros
from app.tests.test_lot61_prospection import _admin
from app.tests.test_lot62_emails_prospection import _campaign, _prospect, gmail  # noqa: F401
from app.tests.test_lot65_pre_inscription import _register

SA = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")
DASH = Path("app/static/dashboard/index.html").read_text(encoding="utf-8")
LANDING = Path("app/static/landing/index.html").read_text(encoding="utf-8")
DEMO = Path("app/static/instant-demo/index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)


async def _settings(db, **fields):
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1, prereg_mode=False)
        db.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    await db.commit()
    return row


# ---------------------------------------------------------------- 1. emails après un WhatsApp

@pytest.mark.asyncio
async def test_campaign_takes_prospects_contacted_on_whatsapp(db_session, gmail):  # noqa: F811
    await _prospect(db_session, company="Contacté WhatsApp", email="w@x.ci", contacted_at=NOW, last_contact_at=NOW)
    await _prospect(db_session, company="A cliqué", email="k@x.ci", contacted_at=NOW, first_click_at=NOW)
    await _prospect(db_session, company="A essayé la démo", email="d@x.ci", demo_at=NOW)
    await _prospect(db_session, company="A répondu", email="r@x.ci", replied_at=NOW)
    already = await _prospect(db_session, company="Déjà un email", email="e@x.ci")
    db_session.add(ProspectEmail(prospect_id=already.id, step=1, status="SENT", sent_at=NOW))
    await db_session.commit()
    c = await _campaign(db_session)
    assert [p.company for p in await mailer.eligible(db_session, c)] == ["Contacté WhatsApp"]


# ---------------------------------------------------------------- 2. places de l'offre de lancement

@pytest.mark.asyncio
async def test_launch_slots_counter_and_offer(client, db_session, email_code):
    await _settings(db_session, prereg_mode=True, launch_slots=2)
    assert (await client.get("/api/v1/preregistration/public")).json() == {
        "mode": True, "offer": pre.OFFER_TEXT, "slots_total": 2, "slots_left": 2, "offer_open": True}
    first, _, _ = await _register(client, email_code, company="Boutique 1")
    second, _, _ = await _register(client, email_code, company="Boutique 2")
    public = (await client.get("/api/v1/preregistration/public")).json()
    assert public["slots_left"] == 0 and public["offer_open"] is False
    third, headers, _ = await _register(client, email_code, company="Boutique 3")
    tenants = {str(t.id): t for t in (await db_session.execute(select(Tenant))).scalars()}
    assert tenants[first].launch_offer and tenants[second].launch_offer
    assert tenants[third].prereg_at is not None and tenants[third].launch_offer is None  # pré-inscrit, sans l'offre
    me = (await client.get("/api/v1/preregistration/me", headers=headers)).json()
    assert me["content"]["offer"] is None and me["offer"] is None
    await _settings(db_session, launch_slots=1)  # moins de places que de comptes déjà servis : jamais négatif
    assert (await client.get("/api/v1/preregistration/public")).json()["slots_left"] == 0
    subject, body = pre.welcome_email(tenants[third], "Awa")
    assert pre.OFFER_TEXT not in body and pre.OFFER_TEXT in pre.welcome_email(tenants[first], "Awa")[1]


@pytest.mark.asyncio
async def test_superadmin_changes_slots(client, db_session):
    headers = await _admin(client, db_session)
    d = (await client.get("/api/v1/superadmin/preregistration", headers=headers)).json()
    assert d["slots"] == {"total": 100, "used": 0, "left": 100}
    d = (await client.put("/api/v1/superadmin/preregistration/slots", headers=headers, json={"total": 40})).json()
    assert d["slots"]["total"] == 40
    assert (await client.put("/api/v1/superadmin/preregistration/slots", headers=headers, json={"total": -1})).status_code == 422


def test_offer_line():
    assert pros.offer_line({"total": 100, "left": 37}) == ("Les 100 premiers inscrits ont l'installation offerte et 3 mois "
                                                           "à −50 % (il reste 37 places).")
    assert pros.offer_line({"total": 100, "left": 1}).endswith("(il reste 1 place).")
    assert pros.offer_line({"total": 100, "left": 0}) is None and pros.offer_line(None) is None


# ---------------------------------------------------------------- 3. entonnoir par pays

@pytest.mark.asyncio
async def test_funnel_by_country(client, db_session):
    headers = await _admin(client, db_session)
    await _prospect(db_session, company="A", email="a@x.ci", country="CI", contacted_at=NOW)
    await _prospect(db_session, company="B", email="b@x.sn", country="SN")
    await _prospect(db_session, company="C", email="c@x.cm", country="CM", contacted_at=NOW, first_click_at=NOW)
    d = (await client.get("/api/v1/superadmin/prospects/funnel", headers=headers)).json()
    rows = {r["name"]: r for r in d["by_country"]}
    assert set(rows) == {"Côte d'Ivoire", "Sénégal", "Cameroun"}
    assert rows["Cameroun"]["clicked"] == 1 and rows["Côte d'Ivoire"]["contacted"] == 1 and rows["Sénégal"]["contacted"] == 0


# ---------------------------------------------------------------- 4. vue « Aujourd'hui »

async def _wa(db, prospect, days_ago, channel="WHATSAPP"):
    at = NOW - timedelta(days=days_ago)
    db.add(ProspectEvent(prospect_id=prospect.id, kind="CONTACT", channel=channel, at=at))
    prospect.contacted_at = prospect.contacted_at or at
    prospect.last_contact_at = at
    await db.commit()


@pytest.mark.asyncio
async def test_today_lists(db_session):
    fresh = await _prospect(db_session, company="Nouveau", email=None, phone="2250700000001")
    emailed = await _prospect(db_session, company="Email parti", email="e@x.ci", phone="2250700000002", contacted_at=NOW - timedelta(days=1))
    await _prospect(db_session, company="Sans téléphone", email="n@x.ci")
    relance = await _prospect(db_session, company="À relancer", email=None, phone="2250700000003")
    await _wa(db_session, relance, 8)
    recent = await _prospect(db_session, company="Contacté hier", email=None, phone="2250700000004")
    await _wa(db_session, recent, 1)
    twice = await _prospect(db_session, company="Déjà relancé", email=None, phone="2250700000005")
    await _wa(db_session, twice, 10)
    await _wa(db_session, twice, 3)
    call = await _prospect(db_session, company="À appeler", email=None, phone="2250700000006")
    await _wa(db_session, call, 15)
    called = await _prospect(db_session, company="Déjà appelé", email=None, phone="2250700000007")
    await _wa(db_session, called, 20)
    await _wa(db_session, called, 2, channel="PHONE")
    clicked = await _prospect(db_session, company="A cliqué", email=None, phone="2250700000008", first_click_at=NOW)
    await _wa(db_session, clicked, 15)
    later = await _prospect(db_session, company="À revoir", email=None, phone="2250700000009",
                            next_action_on=(NOW + timedelta(days=30)).date())
    await _wa(db_session, later, 20)
    lost = await _prospect(db_session, company="Perdu", email=None, phone="2250700000010")
    lost.status = "NOT_INTERESTED"
    await db_session.commit()
    old_mail = await _prospect(db_session, company="Emails sans réponse", email="o@x.ci", phone="2250700000011",
                               contacted_at=NOW - timedelta(days=15))
    today = await pros.today(db_session, NOW, daily_limit=5)
    assert [p.company for p in today["to_send"]] == ["Nouveau", "Email parti"]
    assert [p.company for p, _ in today["relances"]] == ["À relancer"]
    assert [p.company for p, _ in today["calls"]] == ["À appeler", "Emails sans réponse"]
    assert today["sent_today"] == 0 and today["waiting"] == 2
    for p in (fresh, emailed, lost, old_mail):
        assert p.company  # utilisés
    # Limite du jour : déjà 4 WhatsApp aujourd'hui sur 5 → un seul à envoyer
    for i in range(4):
        extra = await _prospect(db_session, company=f"Fait {i}", email=None, phone=f"22507000001{i}")
        await _wa(db_session, extra, 0)
    today = await pros.today(db_session, NOW + timedelta(minutes=1), daily_limit=5)
    assert today["sent_today"] == 4 and today["left_today"] == 1 and [p.company for p in today["to_send"]] == ["Nouveau"]


@pytest.mark.asyncio
async def test_today_endpoint_and_limit(client, db_session):
    headers = await _admin(client, db_session, name="Koffi Yao")
    await _settings(db_session, prereg_mode=True)
    p = await _prospect(db_session, company="Boutique Awa", contact_name="Awa Koné", email=None, phone="2250700000001",
                        sector="ONLINE_STORE")
    relance = await _prospect(db_session, company="Mode Fatou", contact_name="Fatou", email=None, phone="2250700000002",
                              sector="ONLINE_STORE")
    db_session.add(ProspectEvent(prospect_id=relance.id, kind="CONTACT", channel="WHATSAPP",
                                 at=datetime.now(timezone.utc) - timedelta(days=8)))
    relance.contacted_at = datetime.now(timezone.utc) - timedelta(days=8)
    await db_session.commit()
    d = (await client.get("/api/v1/superadmin/prospects/today", headers=headers)).json()
    assert d["limit"] == 25 and [x["company"] for x in d["to_send"]] == ["Boutique Awa"]
    msg = d["to_send"][0]["message"]
    assert msg.startswith("Bonjour Awa, je suis Koffi d'AgenC'AI.") and "il reste 100 places" in msg
    assert msg.endswith(pros.link(p.code, p.sector)) and d["to_send"][0]["whatsapp_url"].startswith("https://wa.me/2250700000001?text=")
    r = d["relances"][0]
    assert r["company"] == "Mode Fatou" and r["days"] == 8 and "je ne vous relancerai plus" in r["message"]
    d = (await client.put("/api/v1/superadmin/prospects/today/limit", headers=headers, json={"limit": 10})).json()
    assert d["limit"] == 10
    assert (await client.put("/api/v1/superadmin/prospects/today/limit", headers=headers, json={"limit": 0})).status_code == 422
    assert (await client.get("/api/v1/superadmin/prospects/today")).status_code == 401
    # La fiche donne le même message, avec l'offre
    detail = (await client.get(f"/api/v1/superadmin/prospects/{p.id}", headers=headers)).json()
    assert "il reste 100 places" in detail["message"]
    await _settings(db_session, prereg_mode=False)
    detail = (await client.get(f"/api/v1/superadmin/prospects/{p.id}", headers=headers)).json()
    assert "places" not in detail["message"]


# ---------------------------------------------------------------- 5. plafonds des formules

async def _shop(db, plan=TenantPlan.INDEPENDANT, conversations=0, **fields):
    tenant = Tenant(name="Boutique Awa", country="CI", currency="XOF", email=f"{uuid.uuid4().hex[:6]}@l69.ci", plan=plan, **fields)
    db.add(tenant)
    await db.flush()
    for i in range(conversations):
        customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2250{uuid.uuid4().int % 10**9:09d}")
        db.add(customer)
        await db.flush()
        db.add(Conversation(tenant_id=tenant.id, customer_id=customer.id))
    await db.commit()
    return tenant


def test_suggested_and_levels():
    q = dict(plan_quota.DEFAULT_QUOTAS)
    assert plan_quota.suggested("INDEPENDANT", 400, q) == "STARTER"
    assert plan_quota.suggested("INDEPENDANT", 2000, q) == "PRO"
    assert plan_quota.suggested("PRO", 9000, q) == "ENTERPRISE" and plan_quota.suggested("ENTERPRISE", 9000, q) is None
    assert [plan_quota.level_of(n, 10) for n in (7, 8, 9, 10, 12)] == [0, 80, 80, 100, 100]
    assert plan_quota.level_of(10, None) == 0
    assert plan_quota.price_label("STARTER") == "49\u00a0500\u00a0FCFA (75\u00a0€) HT par mois" and plan_quota.price_label("ENTERPRISE") == "sur devis"


@pytest.mark.asyncio
async def test_usage_alerts_once_per_level(db_session):
    await _settings(db_session, plan_quotas={"INDEPENDANT": 10})
    tenant = await _shop(db_session, conversations=8)
    await _shop(db_session, plan=TenantPlan.FREE, conversations=9)
    await _shop(db_session, plan=TenantPlan.ENTERPRISE, conversations=9)
    sent = []
    send = lambda **kw: sent.append(kw) or True  # noqa: E731
    u = await plan_quota.usage(db_session, tenant)
    assert (u["used"], u["limit"], u["pct"], u["level"], u["suggested"], u["paused"]) == (8, 10, 80, 80, "STARTER", False)
    report = await plan_quota.check_all(db_session, NOW, send)
    assert report == {"checked": 1, "alerts": 1}
    assert sent[0]["to"] == tenant.email and sent[0]["subject"] == "Bob : 80 % des conversations de votre formule utilisées"
    body = sent[0]["body"]
    assert "8 conversations ce mois-ci, sur les 10 de votre formule Indépendant" in body
    assert "Bob continue de répondre à tous vos clients" in body and "formule Starter (jusqu'à 1\u00a0500 conversations, 49\u00a0500\u00a0FCFA (75\u00a0€) HT par mois)" in body
    assert (await plan_quota.check_all(db_session, NOW, send))["alerts"] == 0  # jamais deux fois le même seuil
    for _ in range(3):
        customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2250{uuid.uuid4().int % 10**9:09d}")
        db_session.add(customer)
        await db_session.flush()
        db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id))
    await db_session.commit()
    await plan_quota.check_all(db_session, NOW, send)
    assert len(sent) == 2 and sent[1]["subject"] == "Bob : plafond de votre formule Indépendant atteint"
    assert "le plafond de votre formule Indépendant (10 conversations) est atteint" in sent[1]["body"]
    # Le mois suivant, on repart de zéro
    tenant.quota_alert_month = "2026-09"
    await db_session.commit()
    await plan_quota.check_all(db_session, NOW, send)
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_failed_email_is_retried(db_session):
    await _settings(db_session, plan_quotas={"INDEPENDANT": 10})
    tenant = await _shop(db_session, conversations=9)
    await plan_quota.check_all(db_session, NOW, lambda **kw: False)
    await db_session.refresh(tenant)
    assert tenant.quota_alert_level == 0


@pytest.mark.asyncio
async def test_pause_mode_only_when_chosen(db_session):
    await _settings(db_session, plan_quotas={"INDEPENDANT": 3})
    tenant = await _shop(db_session, conversations=5)
    assert await plan_quota.paused(db_session, tenant) is False  # par défaut, Bob continue
    await _settings(db_session, quota_mode="PAUSE")
    assert await plan_quota.paused(db_session, tenant) is True
    exactly = await _shop(db_session, conversations=3)
    assert await plan_quota.paused(db_session, exactly) is False  # au plafond, pas au-delà
    demo = await _shop(db_session, conversations=5, is_demo=True)
    assert await plan_quota.usage(db_session, demo) is None
    source = Path("app/api/webhooks/whatsapp.py").read_text(encoding="utf-8")
    assert "quota_exceeded = await plan_quota.paused(db, tenant)" in source


@pytest.mark.asyncio
async def test_superadmin_plans_usage_and_todo(client, db_session):
    headers = await _admin(client, db_session)
    d = (await client.get("/api/v1/superadmin/plans", headers=headers)).json()
    assert d["mode"] == "WARN" and [(p["plan"], p["quota"]) for p in d["plans"]] == [
        ("INDEPENDANT", 350), ("STARTER", 1500), ("PRO", 5000), ("ENTERPRISE", None)]
    d = (await client.put("/api/v1/superadmin/plans", headers=headers, json={"quotas": {"INDEPENDANT": 4}, "mode": "WARN"})).json()
    assert d["plans"][0]["quota"] == 4 and d["plans"][1]["quota"] == 1500
    d = (await client.put("/api/v1/superadmin/plans", headers=headers, json={"quotas": {"STARTER": 2000}, "mode": "WARN"})).json()
    assert [p["quota"] for p in d["plans"]] == [4, 2000, 5000, None]  # un changement garde les autres plafonds
    assert (await client.put("/api/v1/superadmin/plans", headers=headers, json={"quotas": {"GOLD": 4}})).status_code == 422
    assert (await client.put("/api/v1/superadmin/plans", headers=headers, json={"mode": "STOP"})).status_code == 422
    assert (await client.put("/api/v1/superadmin/plans", headers=headers, json={"quotas": {"PRO": 0}})).status_code == 422
    tenant = await _shop(db_session, conversations=4)
    await _shop(db_session, conversations=1)  # 25 % : pas dans « À faire »
    u = (await client.get(f"/api/v1/superadmin/tenants/{tenant.id}/plan-usage", headers=headers)).json()
    assert u["usage"]["pct"] == 100 and u["price"] == "15\u00a0000\u00a0FCFA (23\u00a0€) HT par mois" and u["ai_cost_usd"] == 0
    assert u["ai_share_pct"] == 0 and u["costly"] is False
    overview = (await client.get("/api/v1/superadmin/overview", headers=headers)).json()
    quota = [t for t in overview["todo"] if t["kind"] == "QUOTA"]
    assert quota == [{"kind": "QUOTA", "tone": "danger", "title": "Boutique Awa a utilisé 100 % de sa formule Indépendant",
                      "detail": "4 conversations sur 4 ce mois-ci · formule conseillée : Starter · Bob continue de répondre",
                      "action": "Ouvrir la fiche", "section": "shops", "tenant_id": str(tenant.id)}]


@pytest.mark.asyncio
async def test_tenant_sees_its_usage(client, db_session):
    from app.core.security import create_access_token
    from app.core.security import hash_password
    from app.models.user import Role, User

    tenant = await _shop(db_session, conversations=2)
    user = User(tenant_id=tenant.id, email=tenant.email, hashed_password=hash_password("x"), full_name="Awa", role=Role.OWNER)
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(user.id, tenant.id, 'OWNER')}"}
    d = (await client.get("/api/v1/tenants/me/plan-usage", headers=headers)).json()
    assert d["usage"]["used"] == 2 and d["usage"]["limit"] == 350 and d["usage"]["level"] == 0


def test_scheduled_and_pages():
    from app.services.monitoring import TASK_LABELS
    from app.workers.celery_app import celery_app

    tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
    assert "app.workers.plan_quota.check_quotas_task" in tasks and "app.workers.plan_quota.check_quotas_task" in TASK_LABELS
    for needle in ('id="today-card"', "Relances J7", "Appels J14", "data-group=\"by_country\"", 'id="plans-card"',
                   'id="shop-usage"', "saveSlots()", "QUOTA:"):
        assert needle in SA
    assert 'id="plan-usage-card"' in DASH and "loadPlanUsage();" in DASH and "${c.offer ? `" in DASH
    assert "p.offer_open" in LANDING and "p.offer_open" in DEMO and "p.offer_open" in DASH
    for forbidden in ("alert(", "confirm(", "prompt("):
        assert not re.search(r"(?<![\w.])" + re.escape(forbidden), SA.replace("saConfirm(", ""))
