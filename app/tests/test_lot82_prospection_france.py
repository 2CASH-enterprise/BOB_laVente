"""
Lot 82 — prospection en France : messages WhatsApp prêts (agents et mandataires immobiliers, petites boutiques),
page Facebook de Bob (fin de démo, emails, site) et liens de publication suivis jusqu'à la démo et au compte.
"""
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.ad_link import AdLink, AdLinkEvent
from app.models.prospect import Prospect
from app.models.superadmin_user import SuperAdminUser
from app.services import ad_links
from app.services import prospection as pros

ROOT = Path(__file__).resolve().parents[1]
SA_HTML = (ROOT / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")
BROWSER = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"
LINKS = "/api/v1/superadmin/prospection/links"


async def _admin(client, db, name="Thérèse Martin"):
    email = f"sa{uuid.uuid4().hex[:6]}@agencai.ci"
    db.add(SuperAdminUser(email=email, hashed_password=hash_password("supersecret123"), full_name=name))
    await db.commit()
    token = (await client.post("/api/v1/superadmin/login", data={"username": email, "password": "supersecret123"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Messages France ------------------------------------------------------------------------------------------------

def test_france_real_estate_message():
    p = Prospect(company="Agence du Parc", contact_name="Julien Petit", sector="REAL_ESTATE", country="FR", city="Villeurbanne",
                 code="abc234")
    text = pros.message(p, "Thérèse")
    assert text.startswith("Bonjour Julien, j'ai vu vos annonces à Villeurbanne. Je suis Thérèse, d'AgenC'AI.")
    assert "moins d'une minute, même le soir" in text and "propose les visites" in text
    assert "répondez STOP et je ne vous écrirai plus" in text
    assert text.endswith("/demo/?secteur=immobilier&p=abc234") or text.endswith("p=abc234")
    no_city = pros.message(Prospect(company="X", sector="REAL_ESTATE", country="FR", code="c"), None)
    assert no_city.startswith("Bonjour, j'ai vu vos annonces. Je vous écris de la part d'AgenC'AI.")


def test_france_store_message_and_other_cases_unchanged():
    p = Prospect(company="Wax & Co", contact_name="Awa Koné", sector="ONLINE_STORE", country="FR", code="w1")
    text = pros.message(p, "Thérèse")
    assert text.startswith("Bonjour Awa, j'ai vu que vos clients vous commandent par WhatsApp. Je suis Thérèse, d'AgenC'AI.")
    assert "prend les commandes" in text and "STOP" in text
    # Côte d'Ivoire, et courtier en France : le message habituel
    ci = pros.message(Prospect(company="Wax & Co", contact_name="Awa", sector="ONLINE_STORE", country="CI", code="w2"), "Thérèse")
    assert ci.startswith("Bonjour Awa, je suis Thérèse d'AgenC'AI. Bob, notre vendeur virtuel") and "STOP" not in ci
    fr_broker = pros.message(Prospect(company="Cabinet", sector="INSURANCE_BROKER", country="FR", code="b"), "Thérèse")
    assert "notre vendeur virtuel" in fr_broker


# --- Page Facebook ----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_demo_page_offers_the_facebook_page(client, monkeypatch):
    html = (await client.get("/demo/")).text
    assert 'href="https://www.facebook.com/share/1KPHXStciM/"' in html and "__FACEBOOK_PAGE_URL__" not in html
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "facebook_page_url", "")
    hidden = (await client.get("/demo/")).text
    assert '<a href="" target="_blank"' in hidden and '.fb-follow a[href=""]' in hidden  # masqué par la feuille de style


def test_prospection_email_and_site_link_the_facebook_page(monkeypatch):
    from app.core.config import get_settings
    from app.services.prospect_mailer import render

    p = Prospect(company="Agence", contact_name="Julien", sector="REAL_ESTATE", country="FR", code="abc234")
    _, text, html = render(p, "Bob", "Bonjour {prenom}")
    assert "Suivez Bob sur Facebook : https://www.facebook.com/share/1KPHXStciM/" in text
    assert ">Suivez Bob sur Facebook</a>" in html
    monkeypatch.setattr(get_settings(), "facebook_page_url", "javascript:alert(1)")
    _, text, html = render(p, "Bob", "Bonjour")
    assert "Facebook" not in text and "javascript" not in html  # seule une adresse https:// est proposée
    landing = (ROOT / "static" / "landing" / "index.html").read_text(encoding="utf-8")
    assert 'href="https://www.facebook.com/share/1KPHXStciM/" target="_blank" rel="noopener">Facebook</a>' in landing


# --- Liens de publication ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_and_list_links(client, db_session):
    headers = await _admin(client, db_session)
    bad = await client.post(LINKS, headers=headers, json={"name": "X", "channel": "TIKTOK"})
    assert bad.status_code == 400
    created = await client.post(LINKS, headers=headers, json={"name": "Publicité mandataires Lyon — octobre",
                                                               "channel": "FACEBOOK_AD", "sector": "REAL_ESTATE"})
    assert created.status_code == 201, created.text
    code = created.json()["code"]
    assert code.startswith("publicite-mandataires-lyon-octobre-") and len(code) == len("publicite-mandataires-lyon-octobre-") + 4
    assert created.json()["url"].endswith(f"/demo/?secteur=immobilier&src={code}")
    listing = (await client.get(LINKS, headers=headers)).json()
    assert listing["links"][0]["channel_label"] == "Publicité Facebook" and listing["links"][0]["sector_label"] == "Agence immobilière"
    assert listing["facebook_page_url"] == "https://www.facebook.com/share/1KPHXStciM/"
    assert (await client.get(LINKS)).status_code in (401, 403)  # Super Admin seulement


@pytest.mark.asyncio
async def test_visitors_counted_once_never_previews_then_demo_and_account(client, db_session, unique_email, email_code):
    headers = await _admin(client, db_session)
    code = (await client.post(LINKS, headers=headers, json={"name": "Groupe e-commerçants", "channel": "GROUP",
                                                             "sector": "ONLINE_STORE"})).json()["code"]
    preview = await client.get(f"/demo/?src={code}", headers={"User-Agent": "facebookexternalhit/1.1"})
    assert preview.status_code == 200 and ad_links.COOKIE not in preview.cookies
    first = await client.get(f"/demo/?src={code}", headers={"User-Agent": BROWSER})
    assert first.cookies.get(ad_links.COOKIE) == code and "commerce" in first.text.lower()
    client.cookies.set(ad_links.COOKIE, code)
    await client.get(f"/demo/?src={code}", headers={"User-Agent": BROWSER})  # même navigateur : pas recompté
    await client.get(f"/demo/?src={code.upper()}x", headers={"User-Agent": BROWSER})  # code inconnu : rien
    link = (await db_session.execute(select(AdLink).where(AdLink.code == code))).scalar_one()
    await db_session.refresh(link)
    assert link.clicks == 1 and link.last_click_at is not None

    demo = await client.post("/api/v1/demo/create", data={"company_name": "Cabinet K", "business_type": "INSURANCE_BROKER", "country": "CI"})
    assert demo.status_code == 200, demo.text
    client.cookies.clear()  # compte créé ailleurs : rattaché par la démo
    promoted = await client.post("/api/v1/demo/promote", headers={"Authorization": f"Bearer {demo.json()['demo_token']}"}, json={
        "email": unique_email, "password": "supersecret123", "full_name": "Awa", "verification_code": await email_code(unique_email)})
    assert promoted.status_code == 200, promoted.text
    kinds = sorted(e.kind for e in (await db_session.execute(select(AdLinkEvent).where(AdLinkEvent.link_id == link.id))).scalars())
    assert kinds == ["DEMO", "SIGNUP"]
    row = (await client.get(LINKS, headers=headers)).json()["links"][0]
    assert (row["clicks"], row["demos"], row["signups"]) == (1, 1, 1)
    # Deux fois la même démo : comptée une fois
    await ad_links.attach(db_session, type("R", (), {"cookies": {ad_links.COOKIE: code}})(), uuid.UUID(demo.json()["tenant_id"]), "DEMO")
    assert (await client.get(LINKS, headers=headers)).json()["links"][0]["demos"] == 1


@pytest.mark.asyncio
async def test_archived_link_counts_nothing(client, db_session):
    headers = await _admin(client, db_session)
    created = (await client.post(LINKS, headers=headers, json={"name": "Ancienne pub", "channel": "FACEBOOK_POST"})).json()
    assert created["url"].endswith(f"/demo/?src={created['code']}")
    assert (await client.post(f"{LINKS}/{created['id']}/archive", headers=headers)).json() == {"archived": True}
    page = await client.get(f"/demo/?src={created['code']}", headers={"User-Agent": BROWSER})
    assert page.status_code == 200 and ad_links.COOKIE not in page.cookies
    assert (await client.get(LINKS, headers=headers)).json()["links"][0]["clicks"] == 0
    assert (await client.post(f"{LINKS}/{created['id']}/archive", headers=headers)).json() == {"archived": False}


def test_tracking_never_breaks_a_signup():
    import asyncio

    class Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("base indisponible")

        async def rollback(self):
            pass

    asyncio.run(ad_links.attach(Broken(), type("R", (), {"cookies": {ad_links.COOKIE: "abc-1234"}})(), uuid.uuid4(), "SIGNUP"))


def test_super_admin_card():
    assert 'id="links-card"' in SA_HTML and "loadAdLinks();" in SA_HTML
    js = SA_HTML[SA_HTML.index("async function loadAdLinks()"):SA_HTML.index("// ==== Lot 62")]
    assert "esc(l.name)" in js and "esc(l.url)" in js and "jsArg(l.id)" in js and "Aucun cookie publicitaire" in js
    assert "/api/v1/superadmin/prospection/links" in js
