"""
Lot 105 a — « Votre logiciel immobilier » à la place de Shopify pour les agences : l'export au format SeLoger
(Poliris v4.09) est lu sans IA, vérifié dans le tableau d'import, puis importé ; annule et remplace (un bien absent passe
en Vendu ou Loué) ; honoraires toujours avec le prix (loi ALUR) ; photos en JPEG. Et le message du kit suit le secteur.
"""
import io
import uuid
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.models.contact_point import ContactPoint
from app.models.product import Product
from app.services import launch_kit, poliris
from app.services.photo_formats import UPLOADS
from app.tests.test_lot53_courtier import _cabinet, _headers
from app.tests.test_lot59_excel_kit import HTML, _function


def _listing(**fields) -> str:
    """Une ligne d'Annonces.csv : 329 champs entre guillemets, séparés par « !# »."""
    cells = [""] * 329
    for key, value in fields.items():
        cells[(poliris.F[key] if key in poliris.F else int(key.lstrip("f"))) - 1] = str(value)
    cells[300] = "4.09"
    return "!#".join(f'"{c}"' for c in cells)


SALE = dict(  # noqa: C408 — champs nommés comme dans poliris.F
    agency="ideal", ref="M-307", kind="Vente", type="Maison/villa", cp="35700", city="Rennes",
            district="Bellangerais", price="370975", fees="5", fees_payer="1", price_without_fees="353309",
            surface="108.3", land="435", rooms="6", bedrooms="4", title="Maison familiale jardin plein sud",
            text="Belle maison<BR>Quartier calme", dpe="C", ges="A", tech_id="T307", energy_min="900", energy_max="1300",
            mandate="251", photo1="maison.webp")
RENT = dict(  # noqa: C408
    agency="ideal", ref="L-12", kind="Location", type="Appartement", cp="35000", city="Rennes", price="890",
            fees="650", inventory_fees="120", charges="60", charges_mode="2", deposit="830", surface="45",
            rooms="2", bedrooms="1", title="T2 lumineux Sainte-Thérèse", text="Proche métro", dpe="D", ges="NS",
            tech_id="T12", floor="0", furnished="OUI", photo1="https://cdn.agence.fr/t2.jpg")


def _zip(*lines: str, photos: dict | None = None, mode="FULL", encoding="cp1252") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Annonces.csv", "\r\n".join(lines).encode(encoding))
        archive.writestr("Photos.cfg", f"Mode={mode}")
        archive.writestr("Config.txt", "Version=4.09")
        for name, content in (photos or {}).items():
            archive.writestr(name, content)
    return out.getvalue()


def _webp() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (30, 20), (90, 140, 200)).save(out, "WEBP")
    return out.getvalue()


# --- Lecture du format ------------------------------------------------------------------------------------------------

def test_a_line_of_the_format_is_read():
    assert poliris.parse_line('"a"!#"b ""x"" "!#""!#"Belle<BR>maison"') == ["a", 'b ""x""', "", "Belle\nmaison"]


def test_listings_become_rows_with_fees_always_next_to_the_price():
    saved = {}
    read = poliris.read_zip(_zip(_listing(**SALE), _listing(**RENT), photos={"maison.webp": _webp()}),
                            lambda name, content: saved.setdefault(name, f"https://bob/uploads/products/{name}.jpg"))
    header, (sale, rent) = read["header"], read["rows"]
    row = dict(zip(header, sale))
    assert row["SKU"] == "M-307" and row["NAME"] == "Maison familiale jardin plein sud" and row["PRICE"] == "370975"
    assert row["TRANSACTION"] == "Vente" and row["TYPE"] == "Maison" and row["VILLE"] == "Rennes" and row["QUARTIER"] == "Bellangerais"
    assert row["HONORAIRES"] == "5 % TTC à la charge de l'acquéreur (prix hors honoraires : 353 309 €)"
    assert row["SURFACE"] == "108.3" and row["TERRAIN"] == "435" and row["PIECES"] == "6" and row["CHAMBRES"] == "4"
    assert row["DPE"] == "C" and row["GES"] == "A" and row["REFERENCE MANDAT"] == "251"
    assert row["DESCRIPTION"] == "Belle maison\nQuartier calme\nDépenses d'énergie estimées : entre 900 € et 1 300 € par an."
    assert row["IMAGE_URL"] == "https://bob/uploads/products/maison.webp.jpg" and row["STOCK"] == "1"
    row = dict(zip(header, rent))
    assert row["TRANSACTION"] == "Location" and row["TYPE"] == "Appartement" and row["IMAGE_URL"] == "https://cdn.agence.fr/t2.jpg"
    assert row["HONORAIRES"] == "650 € TTC à la charge du locataire, dont 120 € d'état des lieux"
    assert row["CHARGES"] == "60 € par mois (provisions avec régularisation annuelle)" and row["DEPOT DE GARANTIE"] == "830 €"
    assert row["GES"] == "" and row["ETAGE"] == "RDC" and row["MEUBLE"] == "Meublé"
    assert read["mode"] == "FULL" and read["count"] == 2


def test_what_bob_does_not_take():
    paid_by_seller = poliris.fees_text(_listing(**{**SALE, "fees_payer": "2"}).replace('"', "").split("!#"), True)
    assert paid_by_seller == "à la charge du vendeur"
    read = poliris.read_zip(_zip(_listing(**SALE), _listing(**{**SALE, "kind": "Location vacances", "ref": "V-1"}),
                                 _listing(**{**SALE, "title": "Doublon"}), _listing(**{**RENT, "photo1": "http://x.fr/a.jpg"})),
                            lambda n, c: "")
    assert [r[0] for r in read["rows"]] == ["M-307", "L-12"]
    assert any("Location vacances" in s for s in read["skipped"]) and any("en double" in s for s in read["skipped"])
    assert read["rows"][1][6] == ""  # photo en http : WhatsApp exige https
    assert read["rows"][0][6] == ""  # photo annoncée mais absente du ZIP, bien inconnu : pas de photo


def test_a_photo_already_received_is_kept_in_diff_mode():
    read = poliris.read_zip(_zip(_listing(**SALE), mode="DIFF"), lambda n, c: "",
                            {"M-307": "https://bob/uploads/products/deja.jpg"}.get)
    assert read["rows"][0][6] == "https://bob/uploads/products/deja.jpg" and read["mode"] == "DIFF"


def test_bad_files_are_explained():
    for raw, word in ((b"pas un zip", "ZIP"), (_zip().replace(b"Annonces.csv", b"Autres.csvvv"), "Annonces.csv"),
                      (_zip("a;b;c"), "colonnes")):
        with pytest.raises(poliris.PolirisError, match=word):
            poliris.read_zip(raw, lambda n, c: "")


# --- Dans le tableau de bord -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_agency_checks_then_imports_and_sold_listings_leave(client, db_session):
    tenant = await _cabinet(db_session, business_type="REAL_ESTATE")
    tenant.currency = "EUR"
    await db_session.commit()
    headers = await _headers(client, tenant)
    r = await client.post("/api/v1/catalog/import-preview", headers=headers,
                          files={"file": ("export.zip", _zip(_listing(**SALE), _listing(**RENT), photos={"maison.webp": _webp()}),
                                          "application/zip")})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["source"] == "POLIRIS" and preview["ready"] == 2 and preview["retiring"] == []
    assert "Honoraires" in preview["labels"] and "Vente ou location" in preview["labels"]
    photo = preview["rows"][0][preview["header"].index("IMAGE_URL")]
    assert photo.endswith(".jpg")
    stored = UPLOADS / photo.rsplit("/", 1)[-1]
    try:
        assert Image.open(stored).format == "JPEG"
        r = await client.post("/api/v1/catalog/import-rows", headers=headers, json={
            "header": preview["header"], "rows": preview["rows"], "source": "POLIRIS", "file_skus": preview["file_skus"]})
        assert r.json()["imported"] == 2, r.text
        house = (await db_session.execute(select(Product).where(Product.sku == "M-307"))).scalar_one()
        assert house.bob_category == "MAISON" and house.external_source == "POLIRIS" and house.stock_quantity == 1
        assert house.sheet["fees"].startswith("5 % TTC") and house.sheet["city"] == "Rennes" and house.sheet["dpe"] == "C"
        # Le lendemain, la maison est vendue : elle n'est plus dans l'export (sa photo, déjà reçue, reste en mode DIFF).
        r = await client.post("/api/v1/catalog/import-preview", headers=headers,
                              files={"file": ("export.zip", _zip(_listing(**RENT), mode="DIFF"), "application/zip")})
        preview = r.json()
        assert preview["retiring"] == ["Maison familiale jardin plein sud"]
        await client.post("/api/v1/catalog/import-rows", headers=headers, json={
            "header": preview["header"], "rows": preview["rows"], "source": "POLIRIS", "file_skus": preview["file_skus"]})
        await db_session.refresh(house)
        assert house.stock_quantity == 0 and house.sheet["status"] == "Vendu" and house.active
        # Un bien saisi à la main n'est jamais retiré par la passerelle.
        manual = Product(tenant_id=tenant.id, sku="MAIN-1", name="Saisi à la main", price=1, currency="EUR", stock_quantity=1,
                         bob_category="APPARTEMENT", sheet={"transaction": "Location"})
        db_session.add(manual)
        await db_session.commit()
        await client.post("/api/v1/catalog/import-rows", headers=headers, json={
            "header": preview["header"], "rows": preview["rows"], "source": "POLIRIS", "file_skus": preview["file_skus"]})
        await db_session.refresh(manual)
        assert manual.stock_quantity == 1 and manual.external_source is None
    finally:
        stored.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_row_removed_from_the_review_is_not_sold(client, db_session):
    tenant = await _cabinet(db_session, business_type="REAL_ESTATE")
    headers = await _headers(client, tenant)
    db_session.add(Product(tenant_id=tenant.id, sku="L-12", name="T2", price=890, currency="EUR", stock_quantity=1,
                           bob_category="APPARTEMENT", external_source="POLIRIS", external_id="L-12", sheet={"transaction": "Location"}))
    await db_session.commit()
    preview = (await client.post("/api/v1/catalog/import-preview", headers=headers,
                                 files={"file": ("e.zip", _zip(_listing(**SALE), _listing(**RENT)), "application/zip")})).json()
    rows = [row for row in preview["rows"] if row[0] != "L-12"]  # l'agence retire la ligne de l'aperçu
    await client.post("/api/v1/catalog/import-rows", headers=headers, json={
        "header": preview["header"], "rows": rows, "source": "POLIRIS", "file_skus": preview["file_skus"]})
    flat = (await db_session.execute(select(Product).where(Product.sku == "L-12"))).scalar_one()
    await db_session.refresh(flat)
    assert flat.stock_quantity == 1


@pytest.mark.asyncio
async def test_only_agencies_send_a_zip(client, db_session):
    shop = await _cabinet(db_session, business_type="ONLINE_STORE")
    r = await client.post("/api/v1/catalog/import-preview", headers=await _headers(client, shop),
                          files={"file": ("e.zip", _zip(_listing(**SALE)), "application/zip")})
    assert r.status_code == 400 and "agences" in r.json()["detail"]


# --- Le message du kit suit le secteur ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_kit_message_follows_the_sector(db_session):
    tenant = await _cabinet(db_session, business_type="INSURANCE_BROKER")
    default = ContactPoint(tenant_id=tenant.id, code="KIT_a1", name="Kit", greeting="Bonjour, j'ai une question sur mon assurance",
                           channel="PRINT", active=True)
    written = ContactPoint(tenant_id=tenant.id, code="KIT_a2", name="Kit 2", greeting="Bonjour, une question pour le cabinet",
                           channel="PRINT", active=True)
    other = ContactPoint(tenant_id=tenant.id, code="SALON1", name="Salon", greeting="Bonjour, j'ai une question sur mon assurance",
                         channel="PRINT", active=True)
    db_session.add_all([default, written, other])
    await db_session.commit()
    tenant.business_type = "REAL_ESTATE"
    assert await launch_kit.follow_sector(db_session, tenant, "INSURANCE_BROKER") == 1
    assert default.greeting == "Bonjour, j'ai un projet immobilier"
    assert written.greeting == "Bonjour, une question pour le cabinet" and other.greeting.endswith("assurance")
    assert await launch_kit.follow_sector(db_session, tenant, "REAL_ESTATE") == 0


def test_both_sector_changes_update_the_kit():
    from app.api.superadmin import routes

    assert "follow_sector(db, tenant, previous)" in Path(routes.__file__).read_text(encoding="utf-8")
    from app.api.tenants import routes as tenant_routes

    assert "follow_sector(db, tenant, previous)" in Path(tenant_routes.__file__).read_text(encoding="utf-8")


def test_dashboard():
    assert 'id="estate-software-card"' in HTML and 'class="card estate-only"' in HTML
    assert '<div class="card no-estate" style="flex: 1; min-width: 300px;">\n          <div class="label app-title" style="margin-bottom: 8px; font-weight: 600;"><span class="app-icon shopify"' in HTML
    assert "/api/v1/catalog/import-preview" in _function("importEstateZip") and 'showTab("products")' in _function("importEstateZip")
    assert "file_skus: importDraft.fileSkus" in _function("confirmImport")
    assert "Dans mon logiciel immobilier" in HTML and ".zip" in HTML
    assert uuid
