"""
Lot 88 — (1) photo dès l'ajout d'un produit ; (2) import de catalogue PDF : tableau lu sans IA (gratuit), brochure ou
scan lu par Mistral (quotas : Gratuit 0, Indépendant 5, Starter 20, Pro et Entreprise sans limite ; 50 pages au plus) ;
(3) écran « Vérifiez avant d'importer » pour Excel, CSV et PDF : erreurs et avertissements cellule par cellule, rien
n'est enregistré avant « Importer ».
"""
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.catalog_import import CatalogImport
from app.models.product import Product
from app.models.tenant import TenantPlan
from app.services import catalog_pdf, catalog_preview
from app.services.catalog_import import _clean_price
from app.tests.test_lot87_retouche_photos import DASH, _shop, studio_env  # noqa: F401


# --- Petit fabricant de PDF (texte, cadres de tableau, image JPEG), sans dépendance ----------------------------------

def _jpeg(color=(200, 30, 40), size=(60, 60)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


def _pdf(pages: list[dict]) -> bytes:
    """pages : [{"ops": [str], "image": jpeg ou None}] — format A4 (595 × 842 points)."""
    objects: list[bytes] = []

    def add(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    catalog = add(b"")
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for page in pages:
        resources = f"/Font << /F1 {font} 0 R >>"
        if page.get("image"):
            data = page["image"]
            w, h = Image.open(io.BytesIO(data)).size
            image = add(f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB "
                        f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\nstream\n".encode() + data
                        + b"\nendstream")
            resources += f" /XObject << /Im1 {image} 0 R >>"
        content = "\n".join(page["ops"]).encode("latin-1")
        stream = add(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 595 842] /Resources << {resources} >> "
                        f"/Contents {stream} 0 R >>".encode()))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    objects[tree - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, data in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + data + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _text(x, y, value, size=10) -> str:
    value = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET"


def _table_pdf(rows: list[list[str]], image_row: int | None = 0, repeat_on_second_page: list[list[str]] | None = None) -> bytes:
    """Tableau encadré : Référence | Nom | Prix | Stock | Photo ; une photo dans la colonne Photo de image_row."""
    widths, height = [80, 200, 90, 60, 70], 50

    def page(table_rows, with_image):
        ops, top = [], 780
        for r, cells in enumerate(table_rows):
            x = 40
            y = top - (r + 1) * height
            for c, width in enumerate(widths):
                ops.append(f"{x} {y} {width} {height} re S")
                if c < len(cells) and cells[c]:
                    ops.append(_text(x + 4, y + 20, cells[c]))
                x += width
            if with_image is not None and r == with_image + 1:
                ops.append(f"q 36 0 0 36 {40 + sum(widths[:4]) + 17} {y + 7} cm /Im1 Do Q")
        return {"ops": ops, "image": _jpeg() if with_image is not None else None}

    header = ["Référence", "Nom", "Prix", "Stock", "Photo"]
    pages = [page([header] + rows, image_row)]
    if repeat_on_second_page:
        pages.append(page([header] + repeat_on_second_page, None))
    return _pdf(pages)


def _scan_pdf(pages=1) -> bytes:
    images = [Image.new("RGB", (400, 560), (240, 240, 240)) for _ in range(pages)]
    out = io.BytesIO()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:])
    return out.getvalue()


ROWS = [["R1", "Robe wax Awa", "15.000 FCFA", "4", ""], ["R2", "Chemise lin", "12 500", "", ""], ["R3", "Sac cabas", "", "2", ""]]


# --- Règles ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, clean", [("15.000 FCFA", "15000"), ("15,000", "15000"), ("1.250.000 F CFA", "1250000"),
                                        ("12,50", "12.50"), ("1 250,50 €", "1250.50"), ("1,250.50", "1250.50"),
                                        ("12.5", "12.5"), ("9000", "9000")])
def test_prices_with_thousand_separators(raw, clean):
    assert _clean_price(raw) == clean


def test_quotas_and_limits_chosen_by_therese():
    assert catalog_pdf.PDF_AI_QUOTA == {"FREE": 0, "INDEPENDANT": 5, "STARTER": 20, "PRO": None, "ENTERPRISE": None}
    assert catalog_pdf.MAX_PAGES == 50 and catalog_pdf.OCR_COST_PER_PAGE == 0.002


def test_proposed_reference_comes_from_the_name_and_never_collides():
    taken = {"ROBE-WAX"}
    assert catalog_pdf.proposed_sku("Robe wax", taken) == "ROBE-WAX-2"
    assert catalog_pdf.proposed_sku("Robe wax", taken) == "ROBE-WAX-3"
    assert catalog_pdf.proposed_sku("Écharpe en soie (80 cm)", set()) == "ECHARPE-EN-SOIE-80-CM"
    assert catalog_pdf.proposed_sku("!!!", set()) == "PRODUIT"


def test_one_image_goes_to_one_product_only(studio_env):  # noqa: F811
    data = "data:image/jpeg;base64," + __import__("base64").b64encode(_jpeg()).decode()
    table = catalog_pdf.products_to_table([{"name": "Robe rouge", "image": "img-1.jpeg"}, {"name": "Robe bleue", "image": "img-1.jpeg"}],
                                          {"img-1.jpeg": data}, store=False)
    photos = [row[catalog_pdf.AI_HEADER.index("IMAGE_URL")] for row in table["rows"]]
    assert photos[0].endswith("-pdf.jpg") and photos[1] == ""  # la même image n'est pas collée à deux produits
    assert table["header"] == catalog_pdf.AI_HEADER and table["generated"] == [0, 1]


def test_table_pdf_is_read_without_ai_with_its_photo(studio_env):  # noqa: F811
    read = catalog_pdf.read_tables(_table_pdf(ROWS, image_row=0, repeat_on_second_page=[["R4", "Pagne", "9000", "1", ""]]))
    assert read["pages"] == 2 and read["header"] == ["Référence", "Nom", "Prix", "Stock", "Photo"]
    assert [r[:4] for r in read["rows"]] == [r[:4] for r in ROWS] + [["R4", "Pagne", "9000", "1"]]  # en-tête répété ignoré
    assert list(read["images"]) == [0]
    assert Image.open(io.BytesIO(read["images"][0])).size[0] >= 60
    header, rows = catalog_pdf.with_images(read["header"], read["rows"], read["images"])
    assert rows[0][4].startswith("https://agenc-ai.com/bob/uploads/products/") and rows[0][4].endswith("-pdf.jpg")
    assert rows[1][4] == "" and (studio_env / rows[0][4].rsplit("/", 1)[1]).is_file()


def test_scanned_or_brochure_pdf_has_no_table():
    read = catalog_pdf.read_tables(_scan_pdf(2))
    assert read["pages"] == 2 and read["header"] == [] and read["rows"] == []


@pytest.mark.parametrize("raw, message", [
    (b"bonjour", "pas un PDF"), (b"%PDF-1.4 abime", "PDF illisible"), (None, "51 pages : 50 au plus"),
])
def test_refused_pdfs(raw, message):
    with pytest.raises(catalog_pdf.PdfError, match=message):
        catalog_pdf.read_tables(raw if raw is not None else _scan_pdf(51))


# --- Écran de vérification (Excel, CSV) ------------------------------------------------------------------------------

async def _login(client, db_session, plan=TenantPlan.PRO):
    tenant, email = await _shop(db_session, plan=plan)
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return tenant, {"Authorization": f"Bearer {token}"}


def _csv(lines: list[str]) -> bytes:
    return "\n".join(lines).encode()


@pytest.mark.asyncio
async def test_sheet_preview_flags_cells_and_saves_nothing(client, db_session, studio_env):  # noqa: F811
    tenant, headers = await _login(client, db_session)
    content = _csv(["Référence;Nom;Prix;Devise;Stock;Photo", "R1;Robe;15000;XOF;3;https://x.ci/a.jpg", "R2;Chemise;abc;XOF;2;",
                    ";Sans réf;5000;XOF;1;", "R1;Robe bis;9000;XOF;;", "R5;Pagne;4000;XO;1;"])
    r = await client.post("/api/v1/catalog/import-preview", files={"file": ("cat.csv", content, "text/csv")}, headers=headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] == "SHEET" and d["header"][:3] == ["SKU", "NAME", "PRICE"] and d["labels"][:3] == ["Référence", "Nom", "Prix"]
    issues = {(i["row"], i["field"], i["level"]): i["message"] for i in d["issues"]}
    assert issues[(1, "PRICE", "error")] == "Prix invalide"
    assert issues[(2, "SKU", "error")] == "Référence obligatoire"
    assert issues[(3, "SKU", "warning")] == "Même référence que la ligne 1 : elle la remplacera"
    assert issues[(3, "STOCK", "warning")] == "Stock vide : le produit sera présenté comme indisponible"
    assert issues[(4, "CURRENCY", "error")].startswith("Devise invalide")
    assert (1, "IMAGE_URL", "warning") not in issues  # ligne déjà refusée : pas d'avertissement en plus
    assert (3, "IMAGE_URL", "warning") in issues and (0, "IMAGE_URL", "warning") not in issues
    assert d["ready"] == 2 and d["total"] == 5
    assert (await db_session.execute(select(func.count(Product.id)))).scalar_one() == 0  # rien n'est enregistré

    # Le commerçant corrige, revérifie, puis importe.
    rows = d["rows"]
    rows[1][d["header"].index("PRICE")] = "12000"
    rows[2][0] = "R3"
    rows[4][d["header"].index("CURRENCY")] = "XOF"
    check = (await client.post("/api/v1/catalog/import-check", json={"header": d["header"], "rows": rows}, headers=headers)).json()
    assert [i for i in check["issues"] if i["level"] == "error"] == [] and check["ready"] == 5
    result = (await client.post("/api/v1/catalog/import-rows", json={"header": d["header"], "rows": rows}, headers=headers)).json()
    assert (result["imported"], result["updated"], result["failed"]) == (4, 1, 0)
    names = (await db_session.execute(select(Product.sku, Product.name).where(Product.tenant_id == tenant.id))).all()
    assert sorted(names) == [("R1", "Robe bis"), ("R2", "Chemise"), ("R3", "Sans réf"), ("R5", "Pagne")]


@pytest.mark.asyncio
async def test_excel_preview_and_missing_columns(client, db_session, studio_env):  # noqa: F811
    from openpyxl import Workbook

    _, headers = await _login(client, db_session)
    book = Workbook()
    sheet = book.active
    sheet.append(["Nom", "Prix"])
    sheet.append(["Robe", 15000])
    out = io.BytesIO()
    book.save(out)
    d = (await client.post("/api/v1/catalog/import-preview", headers=headers,
                           files={"file": ("cat.xlsx", out.getvalue(), "application/octet-stream")})).json()
    assert d["ready"] == 0 and d["issues"][0]["row"] is None and "SKU" in d["issues"][0]["message"]


@pytest.mark.asyncio
async def test_rows_sent_back_are_bounded(client, db_session):
    _, headers = await _login(client, db_session)
    too_many = {"header": ["SKU", "NAME"], "rows": [["a", "b"]] * (catalog_preview.MAX_ROWS + 1)}
    r = await client.post("/api/v1/catalog/import-check", json=too_many, headers=headers)
    assert r.status_code == 422 and "2000" in r.json()["detail"]
    r = await client.post("/api/v1/catalog/import-rows", json={"header": [], "rows": []}, headers=headers)
    assert r.status_code == 422


# --- PDF -------------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_table_pdf_preview_is_free_for_every_plan(client, db_session, studio_env):  # noqa: F811
    tenant, headers = await _login(client, db_session, plan=TenantPlan.FREE)
    r = await client.post("/api/v1/catalog/import-preview", headers=headers,
                          files={"file": ("catalogue.pdf", _table_pdf(ROWS), "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    at = {h: i for i, h in enumerate(d["header"])}
    assert d["source"] == "PDF_TABLE" and d["pages"] == 1
    assert [row[at["PRICE"]] for row in d["rows"]] == ["15000", "12500", ""]
    assert [row[at["CURRENCY"]] for row in d["rows"]] == ["XOF"] * 3  # devise de la boutique
    assert d["rows"][0][at["IMAGE_URL"]].endswith("-pdf.jpg")
    issues = {(i["row"], i["field"], i["level"]) for i in d["issues"]}
    assert (2, "PRICE", "error") in issues and (1, "STOCK", "warning") in issues and d["ready"] == 2
    record = (await db_session.execute(select(CatalogImport))).scalar_one()
    assert (record.source, record.status, float(record.cost_usd)) == ("PDF_TABLE", "READY", 0.0)


async def _ocr(raw, key):
    assert raw.startswith(b"%PDF") and key == "cle-mistral"
    b64 = "data:image/jpeg;base64," + __import__("base64").b64encode(_jpeg((20, 60, 200), (120, 160))).decode()
    return {"pages_processed": 2, "pages": [
        {"markdown": "# Collection été\n\n![img-0.jpeg](img-0.jpeg)\n\n**Robe Awa** — 15.000 FCFA — tailles S à XL",
         "images": [{"id": "img-0.jpeg", "data": b64}]},
        {"markdown": "Conditions de vente : paiement à la livraison.", "images": []}]}


async def _structure(markdown, key, model):
    assert "Robe Awa" in markdown and model == get_settings().mistral_classifier_model
    return ([{"name": "Robe Awa", "sku": None, "price": 15000, "currency": "XOF", "sizes": "S à XL", "image": "img-0.jpeg"},
             {"name": "Boubou brodé", "sku": "BB-01", "price": None, "currency": None, "image": "img-9.jpeg"},
             {"name": "", "price": 1}],
            {"prompt_tokens": 900, "cached_tokens": 0, "completion_tokens": 200})


@pytest.mark.asyncio
async def test_brochure_pdf_is_read_by_ai_then_checked_then_imported(client, db_session, studio_env, monkeypatch, tmp_path):  # noqa: F811
    from app.api.catalog import import_ as routes
    from app.models.llm_usage import LlmUsage

    monkeypatch.setattr(get_settings(), "mistral_api_key", "cle-mistral")
    monkeypatch.setattr(catalog_pdf, "STORE", tmp_path / "imports")
    started = []
    monkeypatch.setattr(routes, "_start_ai_reading", lambda import_id: started.append(import_id))
    tenant, headers = await _login(client, db_session, plan=TenantPlan.STARTER)
    r = await client.post("/api/v1/catalog/import-preview", headers=headers,
                          files={"file": ("brochure.pdf", _scan_pdf(2), "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PROCESSING" and r.json()["pages"] == 2
    record = (await db_session.execute(select(CatalogImport))).scalar_one()
    assert started == [record.id] and (tmp_path / "imports" / f"{record.id}.pdf").is_file()
    assert (await client.get(f"/api/v1/catalog/import-preview/{record.id}", headers=headers)).json()["status"] == "PROCESSING"

    assert await catalog_pdf.process_ai_import(db_session, record.id, ocr=_ocr, structurer=_structure) == "READY"
    assert not (tmp_path / "imports" / f"{record.id}.pdf").exists()  # le PDF est effacé après lecture
    assert float(record.cost_usd) == 0.004 and record.pages == 2
    usage = (await db_session.execute(select(LlmUsage))).scalar_one()
    assert (usage.kind, usage.prompt_tokens, usage.tenant_id) == ("CATALOG", 900, tenant.id)

    d = (await client.get(f"/api/v1/catalog/import-preview/{record.id}", headers=headers)).json()
    at = {h: i for i, h in enumerate(d["header"])}
    assert d["status"] == "READY" and d["source"] == "PDF_AI" and len(d["rows"]) == 2  # produit sans nom ignoré
    awa, boubou = d["rows"]
    assert awa[at["SKU"]] == "ROBE-AWA" and awa[at["PRICE"]] == "15000" and awa[at["Tailles ou pointures"]] == "S à XL"
    assert awa[at["IMAGE_URL"]].endswith("-pdf.jpg") and boubou[at["IMAGE_URL"]] == ""  # image inconnue : pas de photo
    assert boubou[at["CURRENCY"]] == "XOF"  # devise de la boutique
    issues = {(i["row"], i["field"], i["level"]): i["message"] for i in d["issues"]}
    assert issues[(0, "SKU", "info")] == "Référence proposée par Bob (le catalogue n'en donne pas)"
    assert (1, "PRICE", "error") in issues and (1, "SKU", "info") not in issues

    boubou[at["PRICE"]] = "35000"
    result = (await client.post("/api/v1/catalog/import-rows", headers=headers,
                                json={"header": d["header"], "rows": d["rows"], "import_id": str(record.id)})).json()
    assert result["imported"] == 2 and result["failed"] == 0
    product = (await db_session.execute(select(Product).where(Product.sku == "ROBE-AWA"))).scalar_one()
    assert product.image_url.endswith("-pdf.jpg") and product.image_status is None  # retouchée ensuite (lot 87)
    assert product.sheet.get("sizes") == "S à XL"
    await db_session.refresh(record)
    assert record.status == "IMPORTED" and record.result is None
    quota = (await client.get("/api/v1/catalog/pdf-quota", headers=headers)).json()
    assert quota == {"quota": 20, "used": 1, "left": 19, "configured": True}


@pytest.mark.asyncio
async def test_ai_reading_is_refused_by_plan_quota_or_missing_key(client, db_session, monkeypatch, tmp_path):
    from app.api.catalog import import_ as routes

    monkeypatch.setattr(catalog_pdf, "STORE", tmp_path)
    monkeypatch.setattr(routes, "_start_ai_reading", lambda import_id: None)
    monkeypatch.setattr(get_settings(), "mistral_api_key", "")
    tenant, headers = await _login(client, db_session, plan=TenantPlan.INDEPENDANT)
    upload = {"file": ("brochure.pdf", _scan_pdf(), "application/pdf")}
    r = await client.post("/api/v1/catalog/import-preview", files=upload, headers=headers)
    assert r.status_code == 422 and "pas encore activée" in r.json()["detail"]

    monkeypatch.setattr(get_settings(), "mistral_api_key", "cle")
    _, free_headers = await _login(client, db_session, plan=TenantPlan.FREE)
    r = await client.post("/api/v1/catalog/import-preview", files=upload, headers=free_headers)
    assert r.status_code == 422 and "à partir de la formule Indépendant" in r.json()["detail"]

    for n in range(5):
        status = "FAILED" if n == 0 else "IMPORTED"
        db_session.add(CatalogImport(tenant_id=tenant.id, source="PDF_AI", status=status))
    db_session.add(CatalogImport(tenant_id=tenant.id, source="PDF_TABLE", status="READY"))
    await db_session.commit()
    assert (await client.post("/api/v1/catalog/import-preview", files=upload, headers=headers)).json()["status"] == "PROCESSING"
    r = await client.post("/api/v1/catalog/import-preview", files=upload, headers=headers)
    assert r.status_code == 422 and "vos 5 imports de PDF mis en page" in r.json()["detail"]  # échec et tableau non comptés


@pytest.mark.asyncio
async def test_failed_or_stuck_ai_reading_is_not_counted(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "mistral_api_key", "cle")
    monkeypatch.setattr(catalog_pdf, "STORE", tmp_path)
    tenant, headers = await _login(client, db_session, plan=TenantPlan.STARTER)
    record = CatalogImport(tenant_id=tenant.id, source="PDF_AI", status="PROCESSING", pages=1)
    db_session.add(record)
    await db_session.commit()
    (tmp_path / f"{record.id}.pdf").write_bytes(_scan_pdf())

    async def down(raw, key):
        raise RuntimeError("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123 refusé")

    assert await catalog_pdf.process_ai_import(db_session, record.id, ocr=down) == "FAILED"
    assert "abcdefghijklmnop" not in record.error and not (tmp_path / f"{record.id}.pdf").exists()
    d = (await client.get(f"/api/v1/catalog/import-preview/{record.id}", headers=headers)).json()
    assert d["status"] == "FAILED" and d["error"].endswith("rien n'a été décompté, vous pouvez réessayer.")
    assert (await catalog_pdf.ai_quota(db_session, tenant))["used"] == 0

    stuck = CatalogImport(tenant_id=tenant.id, source="PDF_AI", status="PROCESSING", pages=1,
                          created_at=datetime.now(timezone.utc) - timedelta(minutes=20))
    db_session.add(stuck)
    await db_session.commit()
    d = (await client.get(f"/api/v1/catalog/import-preview/{stuck.id}", headers=headers)).json()
    assert d["status"] == "FAILED" and "trop de temps" in d["error"]

    empty = CatalogImport(tenant_id=tenant.id, source="PDF_AI", status="PROCESSING", pages=1)
    db_session.add(empty)
    await db_session.commit()
    (tmp_path / f"{empty.id}.pdf").write_bytes(_scan_pdf())

    async def nothing(markdown, key, model):
        return [], None

    assert await catalog_pdf.process_ai_import(db_session, empty.id, ocr=_ocr_any, structurer=nothing) == "FAILED"
    assert empty.error == "Aucun produit trouvé dans ce PDF"


async def _ocr_any(raw, key):
    return {"pages_processed": 1, "pages": [{"markdown": "Couverture", "images": []}]}


@pytest.mark.asyncio
async def test_an_import_of_another_shop_is_never_shown(client, db_session):
    other, _ = await _shop(db_session)
    record = CatalogImport(tenant_id=other.id, source="PDF_AI", status="READY", result={"header": ["SKU"], "rows": [["X"]]})
    db_session.add(record)
    await db_session.commit()
    _, headers = await _login(client, db_session)
    assert (await client.get(f"/api/v1/catalog/import-preview/{record.id}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/catalog/import-preview/{uuid.uuid4()}", headers=headers)).status_code == 404


# --- Tableau de bord -------------------------------------------------------------------------------------------------

def test_dashboard_add_form_has_a_photo_sent_after_creation():
    form = DASH[DASH.index('id="new-product-title"'):DASH.index("createProduct()\">Ajouter</button>")]
    assert 'id="new-product-photo"' in form and 'onchange="showChosenPhoto()"' in form and "jpg, png ou webp" in form
    js = DASH[DASH.index("async function createProduct() {"):DASH.index("function showChosenCsv() {")]
    assert "const created = await api(\"/api/v1/products\"" in js and "`/api/v1/products/${created.id}/image`" in js
    assert "ajoutez-la avec « Gérer »" in js and "5 * 1024 * 1024" in js
    assert "initPhotoDrop();" in DASH[DASH.index("async function loadProducts() {"):][:300]


def test_dashboard_review_screen():
    js = DASH[DASH.index("async function importCsv() {"):DASH.index("async function loadCompanyProfile()")]
    assert '"/api/v1/catalog/import-preview"' in js and 'r.status === "PROCESSING"' in js and "waitForImport(r.id)" in js
    assert '"/api/v1/catalog/import-check"' in js and '"/api/v1/catalog/import-rows"' in js and "import_id: importDraft.importId" in js
    assert "Vérifiez avant d'importer" in js and "À corriger : la ligne ne sera pas importée" in js and "Proposé par Bob" in js
    assert 'id="import-review"' in DASH and 'id="pdf-quota-note"' in DASH and '"/api/v1/catalog/pdf-quota"' in DASH
    assert 'accept=".xlsx,.csv,.pdf' in DASH
