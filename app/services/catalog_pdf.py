"""
Lot 88 — import d'un catalogue PDF (décisions de Thérèse du 20/10).

- PDF avec de vrais tableaux (export d'un logiciel, d'Excel…) : lu sur notre serveur, sans IA, gratuit, pour toutes les
  formules. Les photos placées dans une ligne du tableau sont rattachées au produit de cette ligne.
- Brochure mise en page ou PDF scanné : lu par Mistral (OCR, ≈ 0,002 $ la page, puis rangement en produits par un petit
  modèle). Plafonds : 50 pages par import ; imports par mois : Gratuit 0, Indépendant 5, Starter 20, Pro et Entreprise
  sans limite. Les images repérées près d'un produit lui sont rattachées. Bob n'invente rien : une information absente
  reste vide, et une référence absente est proposée (signalée dans l'écran de vérification).
Tout passe ensuite par l'écran « Vérifiez avant d'importer » (catalog_preview) ; les photos sont retouchées (lot 87).
"""
import base64
import io
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 50
PDF_AI_QUOTA = {"FREE": 0, "INDEPENDANT": 5, "STARTER": 20, "PRO": None, "ENTERPRISE": None}  # None : sans limite
OCR_MODEL = "mistral-ocr-latest"
OCR_COST_PER_PAGE = 0.002  # Mistral OCR 3 : 2 $ les 1 000 pages
PAGES_PER_CALL = 3
PROCESSING_TIMEOUT = timedelta(minutes=15)
STORE = Path(__file__).resolve().parents[1] / "var" / "catalog-imports"  # jamais servi au public (hors de static/)
AI_HEADER = ["SKU", "NAME", "DESCRIPTION", "CATEGORY", "PRICE", "CURRENCY", "STOCK", "IMAGE_URL"]
SHEET_COLUMNS = [("sizes", "Tailles ou pointures"), ("colors", "Couleurs")]


class PdfError(ValueError):
    """PDF refusé ou illisible : message pour le commerçant."""


# --- Lecture sans IA : les tableaux du PDF ---------------------------------------------------------------------------

def _plain(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_header(row: list) -> bool:
    from app.services.csv_column_mapper import _match_column

    known = {_match_column(_plain(c)) for c in row if c} - {None}
    return len(known) >= 2 and bool(known & {"NAME", "PRICE"})


def _crop_jpeg(page, bbox) -> bytes | None:
    try:
        x0, top, x1, bottom = (max(0, bbox[0]), max(0, bbox[1]), min(page.width, bbox[2]), min(page.height, bbox[3]))
        if x1 - x0 < 20 or bottom - top < 20:
            return None
        image = page.crop((x0, top, x1, bottom)).to_image(resolution=200).original.convert("RGB")
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:  # noqa: BLE001 — une image illisible n'empêche pas l'import
        logger.warning("Image du PDF illisible", exc_info=True)
        return None


def open_pdf(raw: bytes):
    import pdfplumber

    if len(raw) > MAX_PDF_BYTES:
        raise PdfError("PDF trop volumineux (20 Mo au plus)")
    if not raw.startswith(b"%PDF"):
        raise PdfError("Ce fichier n'est pas un PDF")
    try:
        pdf = pdfplumber.open(io.BytesIO(raw))
        count = len(pdf.pages)
    except Exception as exc:  # noqa: BLE001
        raise PdfError("PDF illisible (protégé par un mot de passe ou abîmé)") from exc
    if count == 0:
        pdf.close()
        raise PdfError("Ce PDF n'a aucune page")
    if count > MAX_PAGES:
        pdf.close()
        raise PdfError(f"Ce PDF a {count} pages : 50 au plus par import. Découpez-le en plusieurs fichiers.")
    return pdf


def read_tables(raw: bytes) -> dict:
    """{"pages", "header", "rows", "images": {index de ligne: jpeg}} ; header vide si aucun tableau de produits."""
    with open_pdf(raw) as pdf:
        header: list[str] = []
        rows: list[list[str]] = []
        images: dict[int, bytes] = {}
        for page in pdf.pages:
            pictures = list(page.images or [])
            for table in page.find_tables():
                extracted = table.extract()
                for cells, layout in zip(extracted, table.rows):
                    values = [_plain(c) for c in cells]
                    if not any(values):
                        continue
                    if not header:
                        if _is_header(values):
                            header = values
                        continue
                    if [v.lower() for v in values] == [h.lower() for h in header]:
                        continue  # en-tête répété en haut de chaque page
                    top, bottom = layout.bbox[1], layout.bbox[3]
                    inside = [p for p in pictures if top <= (p["top"] + p["bottom"]) / 2 <= bottom]
                    if inside:
                        biggest = max(inside, key=lambda p: (p["x1"] - p["x0"]) * (p["bottom"] - p["top"]))
                        jpeg = _crop_jpeg(page, (biggest["x0"], biggest["top"], biggest["x1"], biggest["bottom"]))
                        if jpeg:
                            images[len(rows)] = jpeg
                    rows.append((values + [""] * len(header))[:len(header)])
        return {"pages": len(pdf.pages), "header": header, "rows": rows, "images": images}


def with_images(header: list[str], rows: list[list[str]], images: dict[int, bytes]) -> tuple[list[str], list[list[str]]]:
    """Photos trouvées dans le PDF : enregistrées chez Bob, leur adresse va dans la colonne Photo de la ligne."""
    from app.services.csv_column_mapper import _match_column
    from app.services.photo_studio import save

    if not images:
        return header, rows
    at = next((i for i, h in enumerate(header) if h == "IMAGE_URL" or _match_column(h) == "IMAGE_URL"), None)
    if at is None:
        header, rows, at = header + ["IMAGE_URL"], [row + [""] for row in rows], len(header)
    for index, jpeg in images.items():
        if index < len(rows) and not rows[index][at].strip():
            rows[index][at] = save(jpeg, "pdf")
    return header, rows


# --- Lecture par l'IA (brochure, scan) -------------------------------------------------------------------------------

def plan_of(tenant) -> str:
    plan = getattr(tenant, "plan", None)
    return str(getattr(plan, "value", plan) or "FREE")


async def ai_quota(db, tenant, now: datetime | None = None) -> dict:
    from app.models.catalog_import import CatalogImport
    from app.services.photo_studio import month_start

    quota = PDF_AI_QUOTA.get(plan_of(tenant), 0)
    used = (await db.execute(select(func.count(CatalogImport.id)).where(
        CatalogImport.tenant_id == tenant.id, CatalogImport.source == "PDF_AI",
        CatalogImport.status != "FAILED", CatalogImport.created_at >= month_start(now)))).scalar_one()
    return {"quota": quota, "used": int(used), "left": None if quota is None else max(0, quota - int(used))}


def ai_refusal(tenant, quota: dict, configured: bool) -> str | None:
    if not configured:
        return "La lecture des PDF mis en page n'est pas encore activée. Exportez votre catalogue en Excel ou en CSV."
    if quota["quota"] == 0:
        return ("Ce PDF est une brochure ou un document scanné : sa lecture par l'IA est disponible à partir de la "
                "formule Indépendant. Un PDF avec un tableau, un fichier Excel ou CSV s'importent dans toutes les formules.")
    if quota["left"] == 0:
        return (f"Vous avez utilisé vos {quota['quota']} imports de PDF mis en page de ce mois-ci. Un PDF avec un "
                "tableau, un fichier Excel ou CSV restent possibles.")
    return None


async def mistral_ocr(raw: bytes, api_key: str) -> dict:
    """Mistral OCR : texte de chaque page (Markdown) et images, la clé ne quitte jamais l'en-tête du SDK."""
    from mistralai.client.sdk import Mistral

    client = Mistral(api_key=api_key)
    response = await client.ocr.process_async(
        model=OCR_MODEL, include_image_base64=True, image_min_size=60,
        document={"type": "document_url", "document_url": "data:application/pdf;base64," + base64.b64encode(raw).decode()})
    data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
    return {"pages": [{"markdown": p.get("markdown") or "",
                       "images": [{"id": i.get("id"), "data": i.get("image_base64")} for i in (p.get("images") or [])]}
                      for p in data.get("pages") or []],
            "pages_processed": int(((data.get("usage_info") or {}).get("pages_processed")) or len(data.get("pages") or []))}


STRUCTURE_PROMPT = """Tu lis un extrait de catalogue de produits (texte d'un PDF, en Markdown). Rends UNIQUEMENT ce JSON :
{"products": [{"name": "...", "sku": null, "description": null, "category": null, "price": null, "currency": null,
"stock": null, "sizes": null, "colors": null, "image": null}]}
Règles :
- Un objet par produit vendu (ignore couverture, sommaire, conditions de vente, coordonnées).
- name : le nom du produit tel qu'écrit. sku : la référence ou le code imprimé, sinon null.
- description : courte (300 caractères au plus), avec les mots du catalogue. category : la rubrique ou le rayon.
- price : un nombre seul (15000 pour « 15.000 FCFA ») ; currency : code à 3 lettres (FCFA ou F CFA → XOF, € → EUR,
  $ → USD) ; si plusieurs prix (ancien, promo), le prix actuel.
- stock : un nombre seulement s'il est écrit. sizes et colors : tels qu'écrits (ex. « S à XL », « noir, camel »).
- image : l'identifiant de l'image du produit, tel qu'il apparaît dans ![…](identifiant), la plus proche du produit.
- N'invente jamais : une information absente reste null."""


async def mistral_structure(markdown: str, api_key: str, model: str) -> tuple[list[dict], dict | None]:
    from mistralai.client.sdk import Mistral

    from app.services.llm_costs import usage_from_mistral

    response = await Mistral(api_key=api_key).chat.complete_async(
        model=model, temperature=0, max_tokens=6000, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": STRUCTURE_PROMPT}, {"role": "user", "content": markdown[:60000]}])
    content = response.choices[0].message.content
    if isinstance(content, list):
        content = "".join(getattr(chunk, "text", "") for chunk in content)
    products = json.loads(content or "{}").get("products") or []
    return [p for p in products if isinstance(p, dict)], usage_from_mistral(getattr(response, "usage", None))


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _plain(value)[:2000]


def proposed_sku(name: str, taken: set[str]) -> str:
    """Référence proposée quand le catalogue n'en a pas : tirée du nom (la même à chaque import du même catalogue)."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Z0-9]+", "-", plain.upper()).strip("-")[:40] or "PRODUIT"
    sku, n = base, 2
    while sku in taken:
        sku, n = f"{base}-{n}", n + 1
    taken.add(sku)
    return sku


def _image_bytes(data: str | None) -> bytes | None:
    if not data:
        return None
    try:
        return base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
    except Exception:  # noqa: BLE001
        return None


def products_to_table(products: list[dict], images: dict[str, str | None], store: bool) -> dict:
    from app.services.photo_studio import save

    header = AI_HEADER + ([label for _, label in SHEET_COLUMNS] if store else [])
    rows, generated, taken = [], [], {_text(p.get("sku")) for p in products if _text(p.get("sku"))}
    used_images: set[str] = set()
    for product in products:
        name = _text(product.get("name"))
        if not name:
            continue
        sku = _text(product.get("sku"))
        if not sku:
            sku = proposed_sku(name, taken)
            generated.append(len(rows))
        photo = ""
        image_id = _text(product.get("image"))
        if image_id and image_id not in used_images and (data := _image_bytes(images.get(image_id))):
            used_images.add(image_id)
            photo = save(data, "pdf")
        row = [sku, name, _text(product.get("description"))[:300], _text(product.get("category")),
               _text(product.get("price")), _text(product.get("currency")).upper()[:3], _text(product.get("stock")), photo]
        if store:
            row += [_text(product.get(key)) for key, _ in SHEET_COLUMNS]
        rows.append(row)
    return {"header": header, "rows": rows, "generated": generated}


async def process_ai_import(db, import_id, ocr=None, structurer=None) -> str:
    """Tâche de fond : lit le PDF par l'IA, range les produits, garde l'aperçu à vérifier. Le PDF est ensuite effacé."""
    from app.core.config import get_settings
    from app.models.catalog_import import CatalogImport
    from app.models.tenant import Tenant
    from app.services import monitoring
    from app.services.business_type import is_store

    record = await db.get(CatalogImport, import_id)
    if record is None or record.status != "PROCESSING":
        return "SKIPPED"
    settings = get_settings()
    path = STORE / f"{record.id}.pdf"
    try:
        tenant = await db.get(Tenant, record.tenant_id)
        raw = path.read_bytes()
        read = await (ocr or mistral_ocr)(raw, settings.mistral_api_key)
        record.pages = read["pages_processed"]
        record.cost_usd = round(read["pages_processed"] * OCR_COST_PER_PAGE, 4)
        products: list[dict] = []
        pages = read["pages"]
        for start in range(0, len(pages), PAGES_PER_CALL):
            chunk = "\n\n".join(p["markdown"] for p in pages[start:start + PAGES_PER_CALL]).strip()
            if not chunk:
                continue
            found, usage = await (structurer or mistral_structure)(chunk, settings.mistral_api_key,
                                                                    settings.mistral_classifier_model)
            products += found
            if usage:
                from app.agents.orchestrator import record_usage

                record_usage(db, record.tenant_id, None, "CATALOG", settings.mistral_classifier_model, {"usage": usage})
        images = {i["id"]: i["data"] for p in pages for i in p["images"] if i.get("id")}
        record.result = products_to_table(products, images, is_store(tenant))
        if not record.result["rows"]:
            record.status, record.error = "FAILED", "Aucun produit trouvé dans ce PDF"
        else:
            record.status = "READY"
    except Exception as exc:  # noqa: BLE001 — le commerçant voit un message clair, le détail va au monitoring
        logger.warning("Lecture du PDF impossible (import %s) : %s", record.id, type(exc).__name__)
        record.status = "FAILED"
        record.error = monitoring.mask(f"Lecture impossible : {exc}")[:300]
    finally:
        path.unlink(missing_ok=True)
    record.finished_at = datetime.now(timezone.utc)
    await db.commit()
    return record.status


def timed_out(record, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    created = record.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return record.status == "PROCESSING" and created is not None and now - created > PROCESSING_TIMEOUT
