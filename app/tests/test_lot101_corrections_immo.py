"""
Lot 101 — corrections du segment immobilier (03/11) : ville puis quartier, « Lyon 3e · Monplaisir » sur les cartes,
exemples français, « Voir d'autres biens » qui reprend la dernière recherche, QR code d'un bien qui ouvre sa fiche à
boutons, lien de l'agence qui ouvre l'accueil, aperçu des liens partagés (photo, titre, prix + honoraires).
"""
import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.contact_point import ContactPoint
from app.models.product_qr_code import ProductQrCode
from app.services import guided_flow as gf
from app.services import immo_flow as imf
from app.services import link_preview
from app.tests.test_handoff_rules import wire  # noqa: F401 — fixture
from app.tests.test_lot94_parcours_boutons import _WA, _text, guided_on  # noqa: F401 — fixture
from app.tests.test_lot97_parcours_agence import _agence, _bien, _client, _go


@pytest.fixture(autouse=True)
def _guided(monkeypatch):
    monkeypatch.setattr(get_settings(), "guided_flow_enabled", True)


def _lyon(tenant):
    return [
        _bien(tenant, "T3 lumineux avec balcon", 1150, city="Lyon 3e", district="Monplaisir", surface=68, bedrooms=2,
              fees="12 €/m² à la charge du locataire", charges="90"),
        _bien(tenant, "T2 rénové", 890, city="Lyon 3e", district="Part-Dieu", surface=45, bedrooms=1, fees="12 €/m²"),
        _bien(tenant, "T4 familial", 1390, city="Villeurbanne", district="Gratte-Ciel", surface=88, bedrooms=3, fees="12 €/m²"),
        _bien(tenant, "Maison avec jardin", 420000, tx="Vente", code="MAISON", city="Villeurbanne", district="Cusset",
              fees="4 % TTC à la charge de l'acquéreur", surface=120),
    ]


async def _agency(db, email, pnid=None):
    tenant, items = await _agence(db, email, pnid=pnid, biens=_lyon)
    tenant.currency = "EUR"
    for item in items:
        item.currency = "EUR"
    await db.commit()
    return tenant, items


# --- Ville puis quartier ---------------------------------------------------------------------------------------------

def test_cards_show_the_city_and_the_district():
    product = _bien(type("T", (), {"id": uuid.uuid4()})(), "T3", 1150, city="Lyon 3e", district="Monplaisir", surface=68)
    assert imf._place(product) == "Lyon 3e · Monplaisir" and imf._detail(product) == "68 m² · Lyon 3e · Monplaisir"
    product.sheet = {**product.sheet, "district": "Lyon 3e"}  # le quartier répète la ville : affiché une fois
    assert imf._place(product) == "Lyon 3e"


@pytest.mark.asyncio
async def test_type_then_city_then_district(db_session, unique_email):
    tenant, items = await _agency(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [cities] = (await _go(db_session, tenant, customer, conv, "g:pl")).actions  # un seul type à louer : sauté
    assert cities["body"] == "Dans quelle ville ?" and cities["button"] == "Choisir une ville"
    assert [r[1:] for r in cities["rows"]] == [("Lyon 3e", "2 appartements à louer"), ("Villeurbanne", "1 appartement à louer"),
                                               ("Peu importe", "3 appartements à louer")]
    [districts] = (await _go(db_session, tenant, customer, conv, cities["rows"][0][0])).actions
    assert districts["body"] == "Dans quel quartier ?"
    assert [r[1:] for r in districts["rows"]] == [("Monplaisir", "1 appartement à louer"), ("Part-Dieu", "1 appartement à louer"),
                                                  ("Peu importe", "2 appartements à louer")]
    [card] = (await _go(db_session, tenant, customer, conv, districts["rows"][0][0])).actions  # un seul bien : sa fiche
    assert card["body"].splitlines()[1] == "68 m² · 2 chambres · Lyon 3e · Monplaisir"
    assert card["body"].splitlines()[2] == "*1 150 EUR* / mois, charges 90 EUR"
    from app.models.prospect_profile import ProspectProfile

    profile = (await db_session.execute(select(ProspectProfile))).scalar_one()
    assert profile.real_estate["projects"]["LOCATION"]["zone"] == "Lyon 3e · Monplaisir"


@pytest.mark.asyncio
async def test_a_city_with_one_district_goes_straight_to_its_properties(db_session, unique_email):
    tenant, items = await _agency(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [cities] = (await _go(db_session, tenant, customer, conv, "g:pl")).actions
    [card] = (await _go(db_session, tenant, customer, conv, cities["rows"][1][0])).actions  # Villeurbanne : Gratte-Ciel seul
    assert card["body"].startswith("*T4 familial*")


@pytest.mark.asyncio
async def test_the_type_comes_before_the_city(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email, biens=lambda t: [
        _bien(t, "A", 900, city="Lyon 3e"), _bien(t, "B", 950, city="Villeurbanne"),
        _bien(t, "C", 1500, code="MAISON", city="Lyon 3e"), _bien(t, "D", 1600, code="MAISON", city="Villeurbanne")])
    customer, conv = await _client(db_session, tenant)
    [types] = (await _go(db_session, tenant, customer, conv, "g:pl")).actions
    assert types["body"] == "Quel type de bien ?"
    [cities] = (await _go(db_session, tenant, customer, conv, types["buttons"][0][0])).actions
    assert cities["body"] == "Dans quelle ville ?"


@pytest.mark.asyncio
async def test_one_city_skips_to_the_districts(db_session, unique_email):
    tenant, _ = await _agence(db_session, unique_email, biens=lambda t: [
        _bien(t, "A", 900, city="Lyon 3e", district="Monplaisir"), _bien(t, "B", 950, city="Lyon 3e", district="Part-Dieu")])
    customer, conv = await _client(db_session, tenant)
    [districts] = (await _go(db_session, tenant, customer, conv, "g:pl")).actions
    assert districts["body"] == "Dans quel quartier ?"


@pytest.mark.asyncio
async def test_owner_example_is_french(db_session, unique_email):
    tenant, _ = await _agency(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    await _go(db_session, tenant, customer, conv, "g:pv")
    [ask] = (await _go(db_session, tenant, customer, conv, "g:ot:V:APPARTEMENT")).actions
    assert "Villeurbanne, Gratte-Ciel, 95 m²" in ask["body"] and "Bingerville" not in ask["body"]
    source = open("app/services/immo_flow.py", encoding="utf-8").read()
    assert "Bingerville" not in source and "Cocody" not in source


# --- « Voir d'autres biens » -----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_see_other_properties_reopens_the_last_search(db_session, unique_email):
    tenant, items = await _agency(db_session, unique_email)
    customer, conv = await _client(db_session, tenant)
    [menu] = (await _go(db_session, tenant, customer, conv, "g:ib")).actions  # aucune recherche encore : l'accueil
    assert menu["rows"][0][1] == "Acheter"
    await _go(db_session, tenant, customer, conv, "g:pl")
    v = imf._place_key("Lyon 3e")
    listing = (await _go(db_session, tenant, customer, conv, f"g:iv:tL,pAPPARTEMENT,v{v}")).actions
    await _go(db_session, tenant, customer, conv, listing[0]["rows"][-1][0])  # « Peu importe » le quartier
    [detail] = (await _go(db_session, tenant, customer, conv, f"g:d:{items[0].id}")).actions
    assert "g:ib" in [b[0] for b in detail["buttons"]]
    again = (await _go(db_session, tenant, customer, conv, "g:ib")).actions
    assert again[0]["type"] == "carousel" and sorted(c["name"] for c in again[0]["cards"]) == ["T2 rénové", "T3 lumineux avec balcon"]
    assert "g:menu" in [b[0] for b in again[-1]["buttons"]]  # « Autre recherche » ramène à l'accueil
    await _go(db_session, tenant, customer, conv, "g:il:tV:0")  # une autre recherche (achat), par la liste directe
    [house] = (await _go(db_session, tenant, customer, conv, "g:ib")).actions
    assert house["body"].startswith("*Maison avec jardin*")


# --- QR code d'un bien, lien de l'agence ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_qr_code_of_a_property_opens_its_card_with_buttons(client, db_session, unique_email, guided_on):
    tenant, items = await _agency(db_session, unique_email, pnid="pn-l101-1")
    db_session.add(ProductQrCode(tenant_id=tenant.id, product_id=items[0].id, code="QRT3LYON"))
    await db_session.commit()
    guided_on({"intents": [], "objections": []}, [])
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l101-1", "33612345601",
                                                           f"Bonjour, je suis intéressé(e) par : {items[0].name} [QR:QRT3LYON]"))
    assert r.json()["guided"] is True
    sent = _WA.sent[-1]
    assert sent["kind"] == "buttons" and [t for _, t in sent["buttons"]] == ["Demander une visite", "Voir d'autres biens",
                                                                            "Un conseiller"]
    assert sent["body"].startswith("*T3 lumineux avec balcon*")


@pytest.mark.asyncio
async def test_agency_link_with_its_greeting_opens_the_welcome(client, db_session, unique_email, guided_on):
    tenant, items = await _agency(db_session, unique_email, pnid="pn-l101-2")
    db_session.add(ContactPoint(tenant_id=tenant.id, code="KITAGENCE", name="Affiche", greeting="Bonjour, j'ai un projet immobilier",
                                channel="PRINT", active=True))
    await db_session.commit()
    guided_on({"intents": [], "objections": []}, [])
    r = await client.post("/webhooks/whatsapp", json=_text("pn-l101-2", "33612345602",
                                                           "Bonjour, j'ai un projet immobilier [W:KITAGENCE]"))
    assert r.json()["guided"] is True and _WA.sent[-1]["kind"] == "list"
    assert [row[1] for row in _WA.sent[-1]["rows"]][:2] == ["Acheter", "Louer"]
    # Le client a réécrit le message : c'est une vraie question, l'IA répond.
    entry = await gf.handle(db_session, tenant, "Vous avez des T3 à Lyon ?", None, entry=None)
    assert entry is None


@pytest.mark.asyncio
async def test_product_link_opens_the_product_card(client, db_session, unique_email, guided_on):
    tenant, items = await _agency(db_session, unique_email, pnid="pn-l101-3")
    db_session.add(ContactPoint(tenant_id=tenant.id, code="SITEAG", name="Site", greeting="Bonjour", channel="SITE", active=True))
    await db_session.commit()
    guided_on({"intents": [], "objections": []}, [])
    await client.post("/webhooks/whatsapp", json=_text("pn-l101-3", "33612345603",
                                                       f"Bonjour, je suis intéressé(e) par : {items[2].name} [W:SITEAG]"))
    assert _WA.sent[-1]["body"].startswith("*T4 familial*")


# --- Aperçu des liens partagés --------------------------------------------------------------------------------------

def test_preview_of_a_property_has_photo_title_price_and_fees():
    product = _bien(type("T", (), {"id": uuid.uuid4()})(), "T3 lumineux avec balcon", 1150, city="Lyon 3e",
                    district="Monplaisir", surface=68, fees="12 €/m² à la charge du locataire")
    product.currency = "EUR"
    title, description, image = link_preview.product_preview(product, "Agence du Parc")
    assert title == "T3 lumineux avec balcon" and image == product.image_url
    assert description == "68 m² · Lyon 3e · Monplaisir · 1 150 EUR / mois — Honoraires : 12 €/m² à la charge du locataire"
    product.sheet = {k: v for k, v in product.sheet.items() if k != "fees"}
    assert "1 150" not in link_preview.product_preview(product, "Agence du Parc")[1]  # jamais un prix sans honoraires
    html = link_preview.page(title, description, image, "https://wa.me/33600000000?text=x", "Agence du Parc").body.decode()
    assert '<meta property="og:image" content="' in html and 'location.replace("https://wa.me/33600000000?text=x")' in html
    assert link_preview.is_crawler("WhatsApp/2.24.20.89 A") and link_preview.is_crawler("facebookexternalhit/1.1")
    assert not link_preview.is_crawler("Mozilla/5.0 (Linux; Android 14) Chrome/130")


@pytest.mark.asyncio
async def test_shared_link_preview_is_not_counted_as_a_click(client, db_session, unique_email):
    tenant, items = await _agency(db_session, unique_email, pnid="pn-l101-4")
    from app.models.whatsapp_account import WhatsAppAccount

    account = (await db_session.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one()
    account.display_phone_number = "+33 6 12 34 56 78"
    cp = ContactPoint(tenant_id=tenant.id, code="PREV1", name="Site", greeting="Bonjour", channel="SITE", active=True)
    db_session.add(cp)
    items[0].sku = "T3LYON"
    await db_session.commit()
    bot = await client.get("/w/PREV1?p=T3LYON", headers={"user-agent": "facebookexternalhit/1.1"})
    assert bot.status_code == 200 and 'content="T3 lumineux avec balcon"' in bot.text
    await db_session.refresh(cp)
    assert cp.click_count == 0
    person = await client.get("/w/PREV1", headers={"user-agent": "Mozilla/5.0"})
    assert "wa.me/33612345678" in person.text and 'content="Agence du Parc"' in person.text
    await db_session.refresh(cp)
    assert cp.click_count == 1
