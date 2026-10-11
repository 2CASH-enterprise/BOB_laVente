"""
Lot 65 — pré-inscription : mode de la plateforme, comptes pré-inscrits verrouillés côté serveur, accueil d'exemple,
fenêtre (contenu, offre, informations et fichiers), Super Admin (interrupteur, ouverture de l'accès), boutons publics.
"""
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.preregistration import PlatformSettings, PreregistrationFile
from app.models.tenant import Tenant
from app.services import bob_pause, monthly_report
from app.services import preregistration as pre
from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE
from app.tests.test_lot61_prospection import _admin

PASSWORD = "supersecret123"
ADMIN_API = "/api/v1/superadmin/preregistration"


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **mail: sent.append(mail) or True)
    return sent


async def _mode(db, on: bool):
    row = await db.get(PlatformSettings, 1)
    if row is None:
        db.add(PlatformSettings(id=1, prereg_mode=on))
    else:
        row.prereg_mode = on
    await db.commit()


async def _register(client, email_code, email=None, business_type="ONLINE_STORE", company="Boutique Awa"):
    email = email or f"p{uuid.uuid4().hex[:8]}@l65.ci"
    r = await client.post("/api/v1/auth/register-tenant", json={
        "company_name": company, "country": "CI", "currency": "XOF", "owner_email": email, "business_type": business_type,
        "verification_code": await email_code(email), "owner_full_name": "Awa Koné", "owner_password": PASSWORD})
    assert r.status_code == 201, r.text
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": PASSWORD})).json()["access_token"]
    return r.json()["tenant_id"], {"Authorization": f"Bearer {token}"}, email


# --- Règles ---------------------------------------------------------------------------------------------------

def test_allowed_paths():
    assert pre.allowed("GET", "/api/v1/analytics/home") and pre.allowed("GET", "/bob/api/v1/tenants/me")
    assert pre.allowed("POST", "/api/v1/auth/logout") and pre.allowed("PUT", "/api/v1/preregistration/me/prep")
    assert pre.allowed("GET", "/api/v1/notifications/counts") and pre.allowed("PUT", "/api/v1/tenants/me/business-type")
    assert pre.allowed("GET", "/api/v1/tenants/me/") and pre.allowed("GET", "/api/v1/preregistration/me")
    assert not pre.allowed("DELETE", "/api/v1/tenants/me/business-type")  # seul le choix du secteur (PUT)
    for method, path in (("GET", "/api/v1/customers"), ("GET", "/api/v1/analytics"), ("PUT", "/api/v1/tenants/me/profile"),
                         ("POST", "/api/v1/tenants/me"), ("GET", "/api/v1/whatsapp/account"), ("GET", "/api/v1/tenants/me/onboarding"),
                         ("DELETE", "/api/v1/analytics/home"), ("GET", "/api/v1/authx/x"), ("GET", "/api/v1/preregistrations")):
        assert not pre.allowed(method, path), (method, path)
    assert pre.api_path("/bob/api/v1/x") == "/api/v1/x" and pre.api_path("/x") == "/x"


def test_locked_and_pause():
    t = Tenant(name="X", country="CI", currency="XOF", active=True)
    assert not pre.is_locked(t) and bob_pause.pause_reason(t) is None
    t.prereg_at = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert pre.is_locked(t) and bob_pause.pause_reason(t) == bob_pause.PREREG
    assert bob_pause.status(t).message.startswith("Votre compte est pré-inscrit")
    t.active = False
    assert bob_pause.pause_reason(t) == bob_pause.SUSPENDED  # la suspension reste prioritaire
    t.active, t.access_opened_at = True, datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert not pre.is_locked(t) and bob_pause.pause_reason(t) is None
    assert pre.clean_phone(" +225 07 00 00 00 01 ") == "+2250700000001" and pre.clean_phone("0022507000000") == "+22507000000"
    assert pre.clean_phone("") is None
    with pytest.raises(ValueError):
        pre.clean_phone("123")


def test_content_and_example_home_by_sector():
    for sector in (ONLINE_STORE, CAR_DEALERSHIP, INSURANCE_BROKER):
        c = pre.content(sector)
        assert len(c["does"]) == 4 and c["offer"] == pre.OFFER_TEXT and c["signature"] == "L'équipe de Bob"
        assert c["prepare"][0].startswith("Un numéro") and "clients" in c["prepare"][2]
        home = pre.example_home(sector, "Awa", "XOF")
        assert home["example"] is True and home["business_type"] == sector and len(home["daily"]) == 30
        assert home["first_name"] == "Awa" and home["todo"] and home["activity"] and len(home["funnel"]) == 4
    assert "contrats" in pre.content(INSURANCE_BROKER)["prepare"][1] and "véhicules" in pre.content(CAR_DEALERSHIP)["prepare"][1]
    assert "quotes" in pre.example_home(INSURANCE_BROKER, None, None)["kpis"]
    assert pre.content(None)["products_label"] == "Fichier de vos produits"  # secteur non choisi = commerce
    assert "−50\u00a0%" in pre.OFFER_TEXT and "3 premiers mois" in pre.OFFER_TEXT and "installation offerts" in pre.OFFER_TEXT


# --- Inscription -----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signup_without_mode_is_a_normal_account(client, db_session, email_code, mails):
    tenant_id, headers, _ = await _register(client, email_code)
    tenant = await db_session.get(Tenant, uuid.UUID(tenant_id))
    assert tenant.prereg_at is None and tenant.launch_offer is None and mails == []
    assert (await client.get("/api/v1/customers", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/preregistration/me", headers=headers)).status_code == 404
    assert (await client.get("/api/v1/preregistration/public")).json() == {"mode": False, "offer": pre.OFFER_TEXT, "slots_total": 100,
                                                                          "slots_left": 100, "offer_open": True}  # lot 69


@pytest.mark.asyncio
async def test_prereg_signup_lock_and_example_home(client, db_session, email_code, mails):
    await _admin(client, db_session)  # un Super Admin à prévenir
    await _mode(db_session, True)
    assert (await client.get("/api/v1/preregistration/public")).json()["mode"] is True
    tenant_id, headers, email = await _register(client, email_code, business_type="CAR_DEALERSHIP", company="Auto Plus")
    tenant = await db_session.get(Tenant, uuid.UUID(tenant_id))
    await db_session.refresh(tenant)
    assert tenant.prereg_at is not None and tenant.launch_offer == pre.OFFER_CODE and tenant.access_opened_at is None
    welcome = next(m for m in mails if m["to"] == email)
    assert welcome["subject"] == "Pré-inscription confirmée — Auto Plus" and pre.OFFER_TEXT in welcome["body"]
    assert "La liste de vos véhicules" in welcome["body"] and welcome["body"].rstrip().endswith("L'équipe de Bob")
    alert = next(m for m in mails if m["to"] != email)
    assert alert["subject"] == "Nouvelle pré-inscription — Auto Plus" and "Concession automobile" in alert["body"]

    # Verrou côté serveur : tout répond 403, sauf l'accueil d'exemple, la pré-inscription et la connexion
    for path in ("/api/v1/customers", "/api/v1/conversations", "/api/v1/products", "/api/v1/analytics",
                 "/api/v1/whatsapp/account", "/api/v1/tenants/me/onboarding", "/api/v1/appointments"):
        r = await client.get(path, headers=headers)
        assert r.status_code == 403 and r.json()["detail"] == pre.LOCKED_MESSAGE, path
    assert (await client.put("/api/v1/tenants/me/profile", headers=headers, json={"name": "Y"})).status_code == 403
    me = await client.get("/api/v1/tenants/me", headers=headers)
    assert me.status_code == 200 and me.json()["bob_status"]["reason"] == "PREREG"
    home = (await client.get("/api/v1/analytics/home", headers=headers)).json()
    assert home["example"] is True and home["business_type"] == CAR_DEALERSHIP and home["first_name"] == "Awa"
    view = (await client.get("/api/v1/preregistration/me", headers=headers)).json()
    assert view["locked"] is True and view["company"] == "Auto Plus" and view["sector"] == CAR_DEALERSHIP
    assert view["offer"] == {"setup_free": True, "discount_pct": 50, "months": 3} and view["first_name"] == "Awa"
    assert view["content"]["size_label"] == "Nombre de véhicules" and view["sizes"] == pre.CATALOG_SIZES
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_prep_info_and_files(client, db_session, email_code, mails):
    await _mode(db_session, True)
    tenant_id, headers, _ = await _register(client, email_code)
    r = await client.put("/api/v1/preregistration/me/prep", headers=headers,
                         json={"whatsapp": "+225 07 07 00 00 01", "catalog_size": "21 à 100", "note": "  Deux   boutiques  "})
    assert r.status_code == 200 and r.json()["prep"]["whatsapp"] == "+2250707000001" and r.json()["prep"]["note"] == "Deux boutiques"
    assert (await client.put("/api/v1/preregistration/me/prep", headers=headers, json={"whatsapp": "12"})).status_code == 422
    assert (await client.put("/api/v1/preregistration/me/prep", headers=headers, json={"catalog_size": "mille"})).status_code == 422
    up = lambda kind, name, data: client.post("/api/v1/preregistration/me/files", headers=headers,  # noqa: E731
                                              data={"kind": kind}, files={"file": (name, data, "text/csv")})
    r = await up("PRODUCTS", "produits.csv", b"nom;prix\nRobe;15000\n")
    assert r.status_code == 200 and [f["filename"] for f in r.json()["prep"]["files"]] == ["produits.csv"]
    r = await up("PRODUCTS", "../../catalogue.xlsx", b"PK...")  # remplace le précédent, chemin retiré
    assert [(f["kind"], f["filename"]) for f in r.json()["prep"]["files"]] == [("PRODUCTS", "catalogue.xlsx")]
    assert (await up("CUSTOMERS", "clients.csv", b"nom;tel\n")).status_code == 200
    assert (await up("CUSTOMERS", "virus.exe", b"MZ")).status_code == 422
    assert (await up("OTHER", "a.csv", b"x")).status_code == 422
    assert (await up("CUSTOMERS", "vide.csv", b"")).status_code == 422
    assert (await up("CUSTOMERS", "gros.csv", b"x" * (pre.FILE_MAX_BYTES + 1))).status_code == 422
    files = (await db_session.execute(select(PreregistrationFile).where(
        PreregistrationFile.tenant_id == uuid.UUID(tenant_id)))).scalars().all()
    assert sorted(f.kind for f in files) == ["CUSTOMERS", "PRODUCTS"]
    product = next(f for f in files if f.kind == "PRODUCTS")
    r = await client.delete(f"/api/v1/preregistration/me/files/{product.id}", headers=headers)
    assert r.status_code == 200 and [f["kind"] for f in r.json()["prep"]["files"]] == ["CUSTOMERS"]
    # Le fichier d'un autre compte : jamais
    _, other_headers, _ = await _register(client, email_code)
    customers = next(f for f in files if f.kind == "CUSTOMERS")
    assert (await client.delete(f"/api/v1/preregistration/me/files/{customers.id}", headers=other_headers)).status_code == 404


@pytest.mark.asyncio
async def test_superadmin_mode_open_relock_and_files(client, db_session, email_code, mails):
    admin = await _admin(client, db_session)
    r = await client.put(f"{ADMIN_API}/settings", headers=admin, json={"mode": True})
    assert r.status_code == 200 and r.json()["mode"] is True and r.json()["changed_by"] == "Koffi Yao"
    first_id, first_headers, first_email = await _register(client, email_code, company="Boutique Awa")
    second_id, second_headers, _ = await _register(client, email_code, business_type="INSURANCE_BROKER", company="Cabinet K")
    await client.post("/api/v1/preregistration/me/files", headers=first_headers, data={"kind": "CUSTOMERS"},
                      files={"file": ("clients.csv", b"nom;tel\nAwa;0700000001\n", "text/csv")})
    data = (await client.get(ADMIN_API, headers=admin)).json()
    assert data["counts"] == {"locked": 2, "opened": 0} and [t["name"] for t in data["tenants"]] == ["Cabinet K", "Boutique Awa"]
    awa = next(t for t in data["tenants"] if t["name"] == "Boutique Awa")
    assert awa["owner"] == "Awa Koné" and awa["email"] == first_email and awa["sector_label"] == "Commerce"
    file_id = awa["prep"]["files"][0]["id"]
    download = await client.get(f"{ADMIN_API}/files/{file_id}", headers=admin)
    assert download.status_code == 200 and download.content.startswith(b"nom;tel") and "clients.csv" in download.headers["content-disposition"]
    assert (await client.get(f"{ADMIN_API}/files/{file_id}", headers=first_headers)).status_code == 401  # pas un Super Admin

    mails.clear()
    r = await client.post(f"{ADMIN_API}/tenants/{first_id}/open", headers=admin)
    assert r.json()["counts"] == {"locked": 1, "opened": 1}
    assert mails[0]["to"] == first_email and mails[0]["subject"] == "Votre accès à Bob est ouvert — Boutique Awa"
    assert pre.OFFER_TEXT in mails[0]["body"]
    assert (await client.get("/api/v1/customers", headers=first_headers)).status_code == 200  # bêta : tout fonctionne
    assert (await client.get("/api/v1/analytics/home", headers=first_headers)).json().get("example") is None
    await client.post(f"{ADMIN_API}/tenants/{first_id}/open", headers=admin)
    assert len(mails) == 1  # déjà ouvert : pas de second email
    await client.post(f"{ADMIN_API}/tenants/{first_id}/relock", headers=admin)
    assert (await client.get("/api/v1/customers", headers=first_headers)).status_code == 403

    r = await client.post(f"{ADMIN_API}/open-all", headers=admin)
    assert r.json()["opened"] == 2 and r.json()["counts"] == {"locked": 0, "opened": 2}
    assert (await client.get("/api/v1/contracts", headers=second_headers)).status_code == 200
    r = await client.put(f"{ADMIN_API}/settings", headers=admin, json={"mode": False})
    assert r.json()["mode"] is False
    third_id, third_headers, _ = await _register(client, email_code)
    assert (await client.get("/api/v1/customers", headers=third_headers)).status_code == 200
    assert (await client.post(f"{ADMIN_API}/tenants/{third_id}/open", headers=admin)).status_code == 404  # pas pré-inscrit
    assert (await client.get(ADMIN_API, headers=first_headers)).status_code == 401


@pytest.mark.asyncio
async def test_demo_promotion_becomes_prereg(client, db_session, email_code, mails):
    await _mode(db_session, True)
    r = await client.post("/api/v1/demo/create", data={"company_name": "Cabinet K", "business_type": "INSURANCE_BROKER", "country": "CI"})
    assert r.status_code == 200, r.text
    token = r.json()["demo_token"]
    email = f"d{uuid.uuid4().hex[:6]}@l65.ci"
    r = await client.post("/api/v1/demo/promote", headers={"Authorization": f"Bearer {token}"}, json={
        "full_name": "Awa Koné", "email": email, "password": PASSWORD, "verification_code": await email_code(email)})
    assert r.status_code == 200, r.text
    tenant = (await db_session.execute(select(Tenant).where(Tenant.email == email))).scalar_one()
    await db_session.refresh(tenant)
    assert tenant.prereg_at is not None and tenant.is_demo is False
    assert any(m["to"] == email and m["subject"].startswith("Pré-inscription confirmée") for m in mails)


def test_report_and_pages():
    t = Tenant(name="X", country="CI", currency="XOF", active=True, is_demo=False,
               prereg_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert monthly_report.next_send_on(t, datetime(2026, 10, 11, tzinfo=timezone.utc)) is None
    assert monthly_report.monthly_due(t, datetime(2026, 11, 2, 10, tzinfo=timezone.utc)) is False
    html = Path("app/static/dashboard/index.html").read_text(encoding="utf-8")
    assert 'id="prereg-overlay"' in html and 'id="prereg-banner"' in html and "function enterPreregMode()" in html
    assert 'if (preregState && preregState.locked && name !== "overview") return;' in html
    assert "var preregState = null;" in html and "var wantsPrereg" in html
    assert 'body.prereg-locked .sidebar nav button:not([data-tab="overview"]) { display: none; }' in html
    assert "Exemple : voici à quoi ressemblera votre accueil" in html and "Je me pré-inscris" in html
    landing = Path("app/static/landing/index.html").read_text(encoding="utf-8")
    assert 'fetch("api/v1/preregistration/public")' in landing and 'href="dashboard/?preinscription=1"' in landing
    demo = Path("app/static/instant-demo/index.html").read_text(encoding="utf-8")
    assert "/api/v1/preregistration/public" in demo and "Me pré-inscrire" in demo
    sa = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")
    assert 'data-section="prereg"' in sa and "Ouvrir l'accès complet (bêta)" in sa and "Ouvrir l'accès à tous les pré-inscrits" in sa
