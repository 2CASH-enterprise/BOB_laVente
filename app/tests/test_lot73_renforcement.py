"""
Lot 73 — renforcement technique : CSP (d'abord « Report-Only », signalements dans le Monitoring), effacement des démos
non transformées après 30 jours, conteneurs sans « root », icônes des connexions.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from app.core.security_headers import CSP_REPORT_PATH, csp_policy
from app.services import monitoring

ROOT = Path(__file__).resolve().parents[2]
DASH = (ROOT / "app" / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
REPORT = {"csp-report": {"document-uri": "https://bob.agenc-ai.com/dashboard/", "violated-directive": "script-src-elem",
                         "effective-directive": "script-src-elem", "blocked-uri": "https://evil.example.com/x.js?token=abc",
                         "source-file": "https://bob.agenc-ai.com/dashboard/", "line-number": 12, "disposition": "report"}}


# --- CSP ---------------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_html_pages_carry_a_report_only_policy(client):
    page = await client.get("/demo/")
    assert page.status_code == 200
    policy = page.headers["content-security-policy-report-only"]
    assert "content-security-policy" not in page.headers
    for needed in ["default-src 'self'", "https://connect.facebook.net", "https://fonts.googleapis.com",
                   "https://fonts.gstatic.com", "img-src 'self' data: blob: https:", "object-src 'none'",
                   "frame-ancestors 'none'", f"report-uri {CSP_REPORT_PATH}"]:
        assert needed in policy, needed
    api = await client.get("/api/v1/integrations/meta-catalog/login-config")
    assert "content-security-policy-report-only" not in api.headers  # seulement les pages HTML
    assert page.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_enforce_switch(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "csp_enforce", True)
    page = await client.get("/demo/")
    assert page.headers["content-security-policy"] == csp_policy()
    assert "content-security-policy-report-only" not in page.headers


@pytest.mark.asyncio
async def test_report_reaches_monitoring_masked_and_grouped(client):
    monitoring._local_queue.clear()
    for _ in range(2):
        r = await client.post(CSP_REPORT_PATH, content=json.dumps(REPORT), headers={"Content-Type": "application/csp-report"})
        assert r.status_code == 204
    events = monitoring.pop_events()
    assert len(events) == 2 and events[0]["signature"] == events[1]["signature"]
    e = events[0]
    assert e["source"] == "BROWSER" and e["exc_type"] == "CSP" and e["context"] == "Tableau de bord"
    assert e["title"] == "CSP : bloquerait https://evil.example.com (script-src-elem) — Tableau de bord"
    assert "token=abc" not in e["title"]

    # Format « Reporting API », extensions ignorées, corps illisible ignoré
    body = [{"type": "csp-violation", "body": {"documentURL": "https://bob.agenc-ai.com/superadmin/",
                                               "effectiveDirective": "img-src", "blockedURL": "http://x.example/a.png"}},
            {"type": "csp-violation", "body": {"documentURL": "https://bob.agenc-ai.com/", "effectiveDirective": "script-src",
                                               "blockedURL": "chrome-extension://abc/inject.js"}}]
    assert (await client.post(CSP_REPORT_PATH, content=json.dumps(body),
                              headers={"Content-Type": "application/reports+json"})).status_code == 204
    assert (await client.post(CSP_REPORT_PATH, content=b"pas du json")).status_code == 204
    events = monitoring.pop_events()
    assert [e["context"] for e in events] == ["Super Admin"]


@pytest.mark.asyncio
async def test_report_endpoint_is_rate_limited(client):
    monitoring._local_queue.clear()
    for _ in range(40):
        await client.post(CSP_REPORT_PATH, content=json.dumps(REPORT))
    assert len(monitoring.pop_events()) == 30


def test_csp_issue_explained_and_never_a_spike():
    issue = type("I", (), {"source": "BROWSER", "exc_type": "CSP", "context": "Tableau de bord", "title": "CSP",
                           "browsers": [], "origin": None, "location": None, "last_message": "", "trace": None,
                           "meta_code": None})()
    info = monitoring.explain(issue)
    assert "Report-Only" in info["consequence"] and "security_headers.py" in info["hint"]
    src = (ROOT / "app" / "services" / "monitoring.py").read_text(encoding="utf-8")
    assert 'if issue.exc_type == "CSP":' in src[src.index("async def send_alerts"):]


# --- Démos effacées après 30 jours ---------------------------------------------------------------------------------

async def _demo(db, created_days_ago, is_demo=True, image=None):
    from app.models.product import Product
    from app.models.tenant import Tenant

    tenant = Tenant(name="Démo", country="CI", currency="XOF", email=f"demo-{uuid.uuid4().hex[:8]}@bob-demo.internal",
                    is_demo=is_demo, created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago))
    db.add(tenant)
    await db.flush()
    db.add(Product(tenant_id=tenant.id, sku="P1", name="Pagne", price=1000, currency="XOF", stock_quantity=1, image_url=image))
    await db.commit()
    return tenant.id


@pytest.mark.asyncio
async def test_old_demos_are_erased_with_their_data_but_traces_stay(db_session, tmp_path):
    from app.models.ad_link import AdLink, AdLinkEvent
    from app.models.messaging_settings import MessageSendAudit, SendResult
    from app.models.product import Product
    from app.models.prospect import Prospect
    from app.models.tenant import Tenant
    from app.services.demo_purge import purge_demos

    await db_session.execute(text("PRAGMA foreign_keys=ON"))
    (tmp_path / "old.jpg").write_bytes(b"x")
    (tmp_path / "shared.jpg").write_bytes(b"x")
    old = await _demo(db_session, 31, image="https://bob.agenc-ai.com/uploads/products/old.jpg")
    shared = await _demo(db_session, 40, image="https://bob.agenc-ai.com/uploads/products/shared.jpg")
    recent = await _demo(db_session, 29)
    promoted = await _demo(db_session, 90, is_demo=False, image="https://bob.agenc-ai.com/uploads/products/shared.jpg")

    link = AdLink(name="Pub", code="pub-ab12", channel="FACEBOOK_AD")
    db_session.add(link)
    await db_session.flush()
    db_session.add_all([AdLinkEvent(link_id=link.id, kind="DEMO", tenant_id=old),
                        Prospect(company="Agence", sector="REAL_ESTATE", country="FR", code="pa1",
                                 demo_at=datetime.now(timezone.utc) - timedelta(days=31), demo_tenant_id=old),
                        MessageSendAudit(tenant_id=old, requested_by="IA", permission_checked="DEMO",
                                         result=list(SendResult)[0])])
    await db_session.commit()
    # Une vraie démo : catégories imbriquées, commande, client (les liens sans cascade sont défaits avant)
    from app.models.category import Category
    from app.models.customer import Customer
    from app.services.order_service import create_order

    parent = Category(tenant_id=old, name="Mode")
    db_session.add(parent)
    await db_session.flush()
    child = Category(tenant_id=old, name="Pagnes", parent_id=parent.id)
    customer = Customer(tenant_id=old, whatsapp_number="2250700000003")
    db_session.add_all([child, customer])
    await db_session.flush()
    product_id = (await db_session.execute(select(Product.id).where(Product.tenant_id == old))).scalar_one()
    await db_session.execute(update(Product).where(Product.id == product_id).values(category_id=child.id, stock_quantity=3))
    await db_session.commit()
    await create_order(db=db_session, tenant_id=old, customer_id=customer.id, items=[{"product_id": str(product_id), "quantity": 1}],
                       delivery_address="Abidjan", payment_method="cash", created_by="IA")
    await db_session.commit()

    report = await purge_demos(db_session, uploads=tmp_path)
    assert report == {"demos": 2, "photos": 1, "errors": 0}
    remaining = set((await db_session.execute(select(Tenant.id))).scalars())
    assert old not in remaining and shared not in remaining and {recent, promoted} <= remaining
    assert (await db_session.execute(select(Product).where(Product.tenant_id == old))).first() is None
    assert not (tmp_path / "old.jpg").exists() and (tmp_path / "shared.jpg").exists()  # photo encore utilisée : gardée

    db_session.expire_all()
    prospect = (await db_session.execute(select(Prospect).where(Prospect.code == "pa1"))).scalar_one()
    assert prospect.demo_at is not None and prospect.demo_tenant_id is None  # « a essayé la démo » reste
    events = (await db_session.execute(select(AdLinkEvent).where(AdLinkEvent.kind == "DEMO"))).scalars().all()
    assert len(events) == 1 and events[0].tenant_id is None  # le comptage par lien reste
    assert (await db_session.execute(select(MessageSendAudit).where(MessageSendAudit.tenant_id == old))).first() is None
    assert await purge_demos(db_session, uploads=tmp_path) == {"demos": 0, "photos": 0, "errors": 0}


def test_purge_is_scheduled_and_labelled():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.demo_purge" in TASK_MODULES
    entry = celery_app.conf.beat_schedule["demo-purge"]
    assert entry["task"] == "app.workers.demo_purge.purge_demos_task"
    assert entry["task"] in monitoring.TASK_LABELS
    from app.services.demo_purge import _upload_name

    assert _upload_name("https://x/uploads/products/a.jpg?v=2") == "a.jpg"
    assert _upload_name("https://x/uploads/products/../secret") is None and _upload_name("https://x/visuels/a.jpg") is None


# --- Conteneurs sans root ---------------------------------------------------------------------------------------------

def test_containers_do_not_run_as_root():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER bob" in docker and "--uid 1000" in docker and "PYTHONDONTWRITEBYTECODE=1" in docker
    assert docker.index("USER bob") > docker.index("pip install")  # les paquets sont installés avant
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "beat --loglevel=info --schedule /tmp/celerybeat-schedule" in compose


# --- Icônes des connexions --------------------------------------------------------------------------------------------

def test_each_connection_has_its_icon():
    for name in ["chat", "bag", "catalog", "mail"]:
        assert f'<symbol id="ico-{name}"' in DASH
    for icon, label in [("chat", "Compte WhatsApp Business"), ("bag", "Shopify</div>"), ("catalog", "Catalogue Facebook (Meta)"),
                        ("mail", "Régler le transfert avec Gmail"), ("mail", "Régler le transfert avec Outlook")]:
        i = DASH.index(label)
        assert f'<use href="#ico-{icon}"/>' in DASH[i - 200:i], label


def test_purge_knows_every_link_without_cascade():
    """PostgreSQL refuse d'effacer une démo si un lien interne sans cascade est oublié : la liste suit le schéma."""
    import importlib
    import pkgutil

    import app.models
    from app.core.database import Base
    from app.services.demo_purge import NO_ACTION_LINKS

    for m in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{m.name}")
    found = {(t.name, fk.parent.name) for t in Base.metadata.tables.values() for fk in t.foreign_keys
             if (fk.ondelete or "").upper() not in ("CASCADE", "SET NULL")}
    assert found == NO_ACTION_LINKS
