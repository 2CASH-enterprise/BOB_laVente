"""
Lot 103 — affiches vitrine premium : 3 modèles avec le QR code du kit (agence, sombre, chevalet) pour tous les secteurs,
2 modèles avec le QR code d'un produit (affiche du bien pour les agences, autocollant vitre pour tous). Photo au choix
parmi les produits ou biens, polices intégrées (rien chez Google), prix d'un bien toujours avec ses honoraires,
signature « Proposé par Bob ».
"""
import uuid

import pytest

from app.models.product import Product
from app.models.product_qr_code import ProductQrCode
from app.services import vitrine_posters as vp
from app.services.business_type import (
    CAR_DEALERSHIP,
    INSURANCE_BROKER,
    ONLINE_STORE,
    REAL_ESTATE,
)
from app.tests.test_lot53_courtier import _headers
from app.tests.test_lot59_excel_kit import HTML, _connected, _function

PHOTO = "https://cdn.example.fr/maison.jpg"


async def _product(db, tenant, name="Maison contemporaine avec piscine", price=640000, sheet=None, **extra):
    product = Product(tenant_id=tenant.id, sku=f"S{uuid.uuid4().hex[:6]}", name=name, price=price, currency="EUR",
                      sheet=sheet, **{"active": True, **extra})
    db.add(product)
    await db.commit()
    return product


async def _qr(db, tenant, product):
    qr = ProductQrCode(tenant_id=tenant.id, product_id=product.id, code=uuid.uuid4().hex[:8])
    db.add(qr)
    await db.commit()
    return qr


SALE = {"transaction": "Vente", "city": "Lyon 3e", "district": "Monplaisir", "fees": "5 % TTC à la charge de l'acquéreur",
        "surface": "160", "rooms": "5", "bedrooms": "4", "land_surface": "900", "dpe": "B", "ges": "A", "reference": "M-2207"}


# --- Les affiches du kit ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("sector,word", [(REAL_ESTATE, "Votre projet immobilier"), (CAR_DEALERSHIP, "Votre prochain véhicule"),
                                         (INSURANCE_BROKER, "Votre assurance"), (ONLINE_STORE, "Vos achats")])
async def test_every_sector_gets_the_three_premium_posters(client, db_session, sector, word):
    tenant = await _connected(db_session, sector)
    headers = await _headers(client, tenant)
    for model, size in (("agence", "210mm 297mm"), ("sombre", "210mm 297mm"), ("chevalet", "148mm 210mm")):
        r = await client.get(f"/api/v1/kit/poster?model={model}&accent=sauge", headers=headers)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
        page = r.text
        assert f"size: {size}; margin: 0" in page and "window.print()" in page
        assert "-webkit-print-color-adjust: exact; print-color-adjust: exact;" in page  # les fonds sombres s'impriment
        assert "font-display:block" in page and 'viewBox="0 0 ' in page  # le QR suit la taille de son cadre
        assert "Proposé par Bob" in page and "+225 07 07 00 00 99" in page and "<svg" in page
        assert "data:font/woff2;base64," in page and "googleapis" not in page and "gstatic" not in page
        assert "#A3B1A0" in page  # la teinte choisie
        if model == "agence":
            assert word in page and "Une réponse," in page
    assert (await client.get("/api/v1/kit/poster?model=inconnu", headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_the_chosen_photo_is_the_merchants_original(client, db_session):
    tenant = await _connected(db_session, REAL_ESTATE)
    headers = await _headers(client, tenant)
    product = await _product(db_session, tenant, image_url="https://cdn.example.fr/studio.png", image_original_url=PHOTO)
    page = (await client.get(f"/api/v1/kit/poster?model=agence&photo={product.id}", headers=headers)).text
    assert f'src="{PHOTO}"' in page and "studio.png" not in page
    assert "<img" not in (await client.get("/api/v1/kit/poster?model=agence", headers=headers)).text  # sans photo
    assert f'src="{PHOTO}"' in (await client.get(f"/api/v1/kit/poster?model=chevalet&photo={product.id}", headers=headers)).text
    other = await _connected(db_session, REAL_ESTATE)
    foreign = await _product(db_session, other, image_url=PHOTO)
    assert (await client.get(f"/api/v1/kit/poster?photo={foreign.id}", headers=headers)).status_code == 404


def test_the_qr_code_follows_the_size_of_its_frame():
    from app.services import launch_kit

    svg = vp.scalable(launch_kit.qr_svg("https://agenc-ai.com/bob/w/KIT_1"))
    assert svg.startswith("<svg") and 'viewBox="0 0 ' in svg.split(">")[0]
    assert vp.scalable(svg) == svg and vp.scalable("<svg></svg>") == "<svg></svg>"


def test_only_a_clean_web_address_becomes_a_photo():
    assert vp.safe_image(PHOTO) == PHOTO and vp.safe_image("  " + PHOTO + " ") == PHOTO
    for bad in ('https://x.fr/a.jpg" onerror="alert(1)', "javascript:alert(1)", "/uploads/a.jpg", "https://x.fr/<b>", None, ""):
        assert vp.safe_image(bad) is None


@pytest.mark.asyncio
async def test_poster_options_list_only_products_with_a_photo(client, db_session):
    tenant = await _connected(db_session, ONLINE_STORE)
    with_photo = await _product(db_session, tenant, name="Robe wax", image_url=PHOTO)
    await _product(db_session, tenant, name="Sans photo")
    await _product(db_session, tenant, name="Archivé", image_url=PHOTO, active=False)
    data = (await client.get("/api/v1/kit/poster-options", headers=await _headers(client, tenant))).json()
    assert [p["id"] for p in data["photos"]] == [str(with_photo.id)]
    assert [m["code"] for m in data["models"]] == ["agence", "sombre", "chevalet"]
    assert [a["code"] for a in data["accents"]] == ["or", "champagne", "sauge", "rose"]


@pytest.mark.asyncio
async def test_a_shop_that_says_tu_keeps_tu_and_an_agency_shows_its_activities(db_session):
    shop = await _connected(db_session, ONLINE_STORE)
    shop.address_form = "TU"
    data = {"phone": "+225 07", "qr_svg": "<svg></svg>"}
    assert "Scanne le code : notre assistant te répond" in vp.kit_poster(shop, data, "agence")
    assert "Scanne, <span" in vp.kit_poster(shop, data, "chevalet")
    product = Product(name="Robe", price=1, currency="XOF")
    assert "Ce produit te plaît ?" in vp.product_poster(shop, product, "+225", "<svg></svg>", "vitre")
    agency = await _connected(db_session, REAL_ESTATE)
    agency.real_estate_profile = {"activities": ["LOCATION", "GESTION"]}
    assert vp.services(agency) == ["Louer", "Mettre en location"]
    agency.real_estate_profile = None
    assert vp.services(agency) == ["Acheter", "Louer", "Vendre", "Faire estimer"]
    agency.name = "<b>Agence</b>"
    page = vp.kit_poster(agency, data, "sombre")
    assert "<b>Agence</b>" not in page and "&lt;b&gt;Agence&lt;/b&gt;" in page


# --- Les affiches d'un produit ou d'un bien --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_property_poster_always_shows_the_price_with_its_fees(client, db_session):
    tenant = await _connected(db_session, REAL_ESTATE)
    tenant.real_estate_profile = {"card": "CPI 6901 2024 000 012 345", "address": "12 rue de la République, 69002 Lyon"}
    await db_session.commit()
    headers = await _headers(client, tenant)
    product = await _product(db_session, tenant, sheet=SALE, image_url=PHOTO)
    qr = await _qr(db_session, tenant, product)
    r = await client.get(f"/api/v1/qr-codes/{qr.id}/poster?model=bien", headers=headers)
    assert r.status_code == 200, r.text
    page = r.text
    assert "size: 210mm 297mm" in page and "À vendre" in page and "Lyon 3e · Monplaisir" in page and f'src="{PHOTO}"' in page
    assert "640 000 €" in page and "EUR" not in page and "Prix honoraires inclus. Honoraires : 5 % TTC à la charge de l&#x27;acquéreur." in page
    assert "160 m²" in page and "900 m²" in page and "Chambres" in page and "DPE" in page and "Réf. M-2207" in page
    assert "12 rue de la République, 69002 Lyon · Carte professionnelle CPI 6901 2024 000 012 345" in page
    assert "Proposé par Bob" in page and "[" not in page.split('<div class="sheet">')[1]  # jamais de case vide

    product.sheet = {**SALE, "fees": ""}  # loi ALUR : pas d'honoraires, pas de prix
    await db_session.commit()
    page = (await client.get(f"/api/v1/qr-codes/{qr.id}/poster?model=bien", headers=headers)).text
    assert "640 000" not in page and "Prix et honoraires sur demande auprès de l&#x27;agence." in page


@pytest.mark.asyncio
async def test_rental_poster_and_missing_lines(db_session):
    tenant = await _connected(db_session, REAL_ESTATE)
    product = await _product(db_session, tenant, name="T2 lumineux", price=890, image_url=PHOTO,
                             sheet={"transaction": "Location", "city": "Villeurbanne", "fees": "12 €/m² à la charge du locataire",
                                    "charges": "60 €", "deposit": "830 €", "surface": "45 m²"})
    page = vp.product_poster(tenant, product, "+33 6", "<svg></svg>", "bien")
    assert "À louer" in page and "par mois" in page and "Loyer charges comprises, dont 60 € de charges." in page
    assert "Dépôt de garantie : 830 €." in page and "45 m²" in page and "45 m² m²" not in page
    assert "Réf." not in page and "DPE" not in page and "Carte professionnelle" not in page
    facts = vp.bien_facts(product)
    assert facts["specs"] == [("45 m²", "Habitables")]


@pytest.mark.asyncio
async def test_window_sticker_for_every_sector_and_guards(client, db_session):
    shop = await _connected(db_session, ONLINE_STORE)
    headers = await _headers(client, shop)
    product = await _product(db_session, shop, name="Robe wax", image_url=PHOTO)
    qr = await _qr(db_session, shop, product)
    r = await client.get(f"/api/v1/qr-codes/{qr.id}/poster?model=vitre&accent=rose", headers=headers)
    assert r.status_code == 200 and "size: 200mm 200mm" in r.text and "Ce produit vous plaît ?" in r.text and "#8C4F45" in r.text
    assert "Proposé par Bob" in r.text and "<svg" in r.text
    # l'affiche « bien » est réservée aux agences, un bien sans photo n'a pas d'affiche A4
    assert (await client.get(f"/api/v1/qr-codes/{qr.id}/poster?model=bien", headers=headers)).status_code == 422
    agency = await _connected(db_session, REAL_ESTATE)
    bare = await _qr(db_session, agency, await _product(db_session, agency, sheet=SALE))
    r = await client.get(f"/api/v1/qr-codes/{bare.id}/poster?model=bien", headers=await _headers(client, agency))
    assert r.status_code == 422 and "photo" in r.json()["detail"]
    assert "Visitez-le" in (await client.get(f"/api/v1/qr-codes/{bare.id}/poster", headers=await _headers(client, agency))).text
    # le QR d'une autre boutique est introuvable
    assert (await client.get(f"/api/v1/qr-codes/{bare.id}/poster", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_agency_address_is_kept_in_the_profile(client, db_session):
    tenant = await _connected(db_session, REAL_ESTATE)
    r = await client.put("/api/v1/tenants/me/real-estate-profile", headers=await _headers(client, tenant),
                         json={"status": "AGENCE", "activities": ["VENTE"], "address": "  12 rue de la République,   Lyon "})
    assert r.status_code == 200, r.text
    assert r.json()["address"] == "12 rue de la République, Lyon"


def test_fonts_are_bundled_with_their_licence():
    for _, _, _, filename in vp._FONTS:
        assert (vp.FONTS_DIR / filename).stat().st_size > 5000
    assert (vp.FONTS_DIR / "OFL-Jost.txt").exists() and (vp.FONTS_DIR / "OFL-Cormorant-Garamond.txt").exists()


def test_dashboard():
    for el in ('id="kit-poster-model"', 'id="kit-poster-accent"', 'id="kit-poster-photo"', 'id="estate-address"'):
        assert el in HTML, el
    assert "/api/v1/kit/poster-options" in _function("loadKitPosterOptions")
    assert "photo" in _function("openKitPoster") and "/poster?" in _function("openQrPoster")
    assert "openQrPoster(${jsArg(c.id)}, 'bien')" in HTML and "openQrPoster(${jsArg(c.id)}, 'vitre')" in HTML
    assert 'address: "estate-address"' in HTML
