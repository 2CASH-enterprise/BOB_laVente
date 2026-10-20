"""
Lot 87 — « Bob retouche vos photos » (décisions de Thérèse, 20/10).

- FOND BLANC STUDIO pour tous les produits (hors agences immobilières et courtiers) : détourage puis finition Bob
  (fond blanc légèrement dégradé, ombre douce, article centré) et signature discrète « Édité par Bob AI ».
  Détourage par Photoroom (Basic, 0,02 $) dans le quota mensuel de la formule, au-delà (et en formule gratuite) par
  notre serveur (gratuit). Le commerçant garde toujours sa photo d'origine et peut y revenir.
- PHOTO PORTÉE (habits seulement, jamais en formule gratuite) : FASHN « product to model », mode équilibré (2 crédits,
  ≈ 0,15 $), validée par le commerçant avant d'être montrée. Carte : montage « Photo réelle | Portée » (à l'essai) ou
  photo portée seule.
"""
import base64
import io
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

UPLOADS = Path(__file__).resolve().parents[1] / "static" / "uploads" / "products"
STUDIO_SIZE = 1200
MAX_SOURCE_BYTES = 12 * 1024 * 1024
SIGNATURE = "Édité par Bob AI"

# Quotas par mois (choix de Thérèse du 20/10). Au-delà du fond blanc Photoroom : notre serveur prend le relais.
WHITE_QUOTA = {"FREE": 0, "INDEPENDANT": 50, "STARTER": 150, "PRO": 250, "ENTERPRISE": 500}
WORN_QUOTA = {"FREE": 0, "INDEPENDANT": 5, "STARTER": 20, "PRO": 100, "ENTERPRISE": 500}
PHOTOROOM_COST = 0.02
FASHN_COST = 0.15  # mode « balanced » : 2 crédits à 0,075 $

PHOTOROOM_URL = "https://sdk.photoroom.com/v1/segment"
FASHN_RUN_URL = "https://api.fashn.ai/v1/run"
FASHN_STATUS_URL = "https://api.fashn.ai/v1/status/{id}"

AFRICA = {"CI", "SN", "BJ", "TG", "ML", "BF", "NE", "GN", "CM", "GA", "CG", "CD", "TD", "CF", "MR", "GW", "SL", "LR",
          "GH", "NG", "KE", "RW", "MG"}
_ACCESSORY = re.compile(r"(?i)chaussure|basket|sneaker|escarpin|sandale|mocassin|botte|bottine|talon|tong|claquette|"
                        r"mule\b|derby|sac\b|sacoche|pochette|portefeuille|porte-monnaie|ceinture|montre|bijou|collier|"
                        r"bracelet|bague|boucles? d'oreille|lunette|chapeau|casquette|bonnet|écharpe|echarpe|cravate|"
                        r"parfum|valise")
_GARMENT = re.compile(r"(?i)robe|chemise|pantalon|costume|veste|jupe|t-?shirt|tee-?shirt|boubou|pagne|ensemble|"
                      r"manteau|blouson|short|jean|pull|sweat|polo|tailleur|abaya|kaftan|caftan|tunique|combinaison|"
                      r"chemisier|blouse|gilet|kimono|survêtement|survetement|maillot|body|top\b|débardeur|debardeur")


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def plan_of(tenant) -> str:
    plan = getattr(tenant, "plan", None)
    return str(getattr(plan, "value", plan) or "FREE")


def retouches(tenant) -> bool:
    """Pas pour les démos, les agences immobilières (pièces, façades) ni les courtiers."""
    from app.services.business_type import is_insurance, is_real_estate

    return bool(tenant) and not tenant.is_demo and not is_real_estate(tenant) and not is_insurance(tenant)


def is_garment(product) -> bool:
    """Habits seulement (choix du 20/10) : ni chaussures, ni sacs, ni accessoires."""
    text = f"{getattr(product, 'name', '')} {(getattr(product, 'sheet', None) or {}).get('occasion', '')}"
    if _ACCESSORY.search(getattr(product, "name", "") or ""):
        return False
    return getattr(product, "bob_category", None) == "FASHION" or bool(_GARMENT.search(text))


async def used(db, tenant_id, kind: str, engine: str | None = None, now: datetime | None = None) -> int:
    from app.models.image_job import ImageJob

    stmt = select(func.count()).select_from(ImageJob).where(
        ImageJob.tenant_id == tenant_id, ImageJob.kind == kind, ImageJob.created_at >= month_start(now),
        ImageJob.status.in_(("PENDING", "DONE")))
    if engine:
        stmt = stmt.where(ImageJob.engine == engine)
    return int((await db.execute(stmt)).scalar() or 0)


async def quotas(db, tenant, now: datetime | None = None) -> dict:
    plan = plan_of(tenant)
    white_used = await used(db, tenant.id, "WHITE", "PHOTOROOM", now)
    worn_used = await used(db, tenant.id, "WORN", None, now)
    return {"white_quota": WHITE_QUOTA.get(plan, 0), "white_used": white_used,
            "white_left": max(0, WHITE_QUOTA.get(plan, 0) - white_used),
            "worn_quota": WORN_QUOTA.get(plan, 0), "worn_used": worn_used,
            "worn_left": max(0, WORN_QUOTA.get(plan, 0) - worn_used)}


# --- Source de la photo ---------------------------------------------------------------------------------------------

def local_file(url: str | None) -> Path | None:
    """Une photo déjà chez Bob (uploads/products/…) : lue sur le disque, jamais téléchargée."""
    marker = "/uploads/products/"
    if not url or marker not in url:
        return None
    name = url.rsplit(marker, 1)[1].split("?", 1)[0]
    if not name or "/" in name or ".." in name:
        return None
    path = UPLOADS / name
    return path if path.is_file() else None


async def load_source(url: str) -> bytes:
    path = local_file(url)
    if path is not None:
        return path.read_bytes()
    if not (url or "").lower().startswith("https://"):
        raise ValueError("adresse de photo non sécurisée")
    import httpx

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, max_redirects=3) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").lower().startswith("image/"):
                raise ValueError("ce n'est pas une image")
            data = b""
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > MAX_SOURCE_BYTES:
                    raise ValueError("photo trop lourde")
    return data


# --- Détourage ------------------------------------------------------------------------------------------------------

async def photoroom_cutout(data: bytes, api_key: str) -> bytes:
    """Photoroom « Remove Background » (Basic) : PNG transparent. La clé voyage dans l'en-tête, jamais ailleurs."""
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(PHOTOROOM_URL, headers={"x-api-key": api_key},
                                     files={"image_file": ("photo", data)}, data={"format": "png"})
    if response.status_code != 200 or not response.content:
        raise RuntimeError(f"Photoroom a répondu {response.status_code}")
    return response.content


async def server_cutout(data: bytes, model_path: str) -> bytes:
    """Notre serveur : détourage dans un processus séparé (mémoire rendue aussitôt)."""
    import asyncio
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        source, target = Path(folder) / "source", Path(folder) / "cutout.png"
        source.write_bytes(data)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.services.photo_cutout", str(source), str(target), model_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(process.communicate(), timeout=180)
        except asyncio.TimeoutError:
            process.kill()
            raise RuntimeError("détourage trop long") from None
        if process.returncode != 0 or not target.is_file():
            raise RuntimeError("détourage impossible : " + (err or b"").decode("utf-8", "replace")[-200:])
        return target.read_bytes()


# --- Finition studio, signature, montage -----------------------------------------------------------------------------

FONT_FILES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # paquet fonts-dejavu-core (Dockerfile)
              "/usr/share/fonts/dejavu/DejaVuSans.ttf")


def _font(size: int, bold: bool = False):
    """Police avec les accents (« Édité », « réelle ») ; la police intégrée de Pillow ne les a pas."""
    from PIL import ImageFont

    for path in FONT_FILES:
        candidate = path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf") if bold else path
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def _studio_background(width: int, height: int):
    """Blanc pur en haut, très légèrement gris en bas : l'effet « fond de studio »."""
    from PIL import Image

    column = Image.new("L", (1, height))
    column.putdata([255 - int(11 * max(0.0, (y / height) - 0.35) / 0.65) for y in range(height)])
    return Image.merge("RGB", [column.resize((width, height))] * 3)


def _signature(canvas, size: int = 18) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(canvas)
    draw.text((20, canvas.height - size - 16), SIGNATURE, fill=(150, 156, 162), font=_font(size))


def studio(cutout_png: bytes, signature: bool = True, size: int = STUDIO_SIZE) -> bytes:
    """Article détouré → fond blanc studio, ombre douce, article centré (82 % du cadre), JPEG."""
    from PIL import Image, ImageFilter

    item = Image.open(io.BytesIO(cutout_png)).convert("RGBA")
    box = item.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox()
    if not box:
        raise RuntimeError("aucun article détecté sur la photo")
    item = item.crop(box)
    scale = min(size * 0.82 / item.width, size * 0.78 / item.height)
    item = item.resize((max(1, int(item.width * scale)), max(1, int(item.height * scale))), Image.LANCZOS)
    canvas = _studio_background(size, size)
    left, top = (size - item.width) // 2, int(size * 0.88) - item.height
    shadow = Image.new("L", (size, size), 0)
    from PIL import ImageDraw

    ImageDraw.Draw(shadow).ellipse((left + item.width * 0.12, top + item.height - item.height * 0.035,
                                    left + item.width * 0.88, top + item.height + item.height * 0.035), fill=70)
    shadow = shadow.filter(ImageFilter.GaussianBlur(size // 60))
    canvas.paste((60, 60, 60), (0, 0), shadow)
    canvas.paste(item, (left, top), item)
    if signature:
        _signature(canvas)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def _pill(canvas, text: str, x: int, y: int, right: bool = False) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(canvas)
    font = _font(24, bold=True)
    width = int(draw.textlength(text, font=font)) + 28
    if right:
        x = x - width
    draw.rounded_rectangle((x, y, x + width, y + 40), radius=20, fill=(27, 31, 35))
    draw.text((x + 14, y + 7), text, fill=(255, 255, 255), font=font)


def _tight(image):
    """Recadre une photo studio autour de l'article (le fond blanc et la signature d'origine sont laissés de côté)."""
    from PIL import ImageOps

    gray = ImageOps.invert(image.convert("L")).point(lambda v: 255 if v > 18 else 0)
    width, height = image.size
    gray.paste(0, (0, int(height * 0.93), int(width * 0.4), height))  # pas la signature « Édité par Bob AI »
    box = gray.getbbox()
    if not box:
        return image
    margin = int(max(width, height) * 0.02)
    return image.crop((max(0, box[0] - margin), max(0, box[1] - margin), min(width, box[2] + margin),
                       min(height, box[3] + margin)))


def montage(real_jpg: bytes, worn_jpg: bytes, size: int = STUDIO_SIZE) -> bytes:
    """« Photo réelle | Portée » sur une seule image carrée (une carte WhatsApp = une image)."""
    from PIL import Image, ImageOps

    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    half = size // 2
    for index, data in enumerate((real_jpg, worn_jpg)):
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        if index == 0:  # l'article entier, recadré au plus près, sur le même fond studio
            item = _tight(image)
            scale = min((half - 1) * 0.9 / item.width, size * 0.7 / item.height)
            item = item.resize((max(1, int(item.width * scale)), max(1, int(item.height * scale))), Image.LANCZOS)
            image = _studio_background(half - 1, size)
            image.paste(item, ((half - 1 - item.width) // 2, int(size * 0.5 - item.height * 0.45)))
        else:  # le mannequin, cadré sur le corps
            image = ImageOps.fit(image, (half - 1, size), Image.LANCZOS, centering=(0.5, 0.4))
        canvas.paste(image, (index * (half + 1), 0))
    _pill(canvas, "Photo réelle", 18, 18)
    _pill(canvas, "Portée", size - 18, 18, right=True)
    _signature(canvas)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def save(data: bytes, suffix: str) -> str:
    from app.core.config import get_settings

    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}-{suffix}.jpg"
    (UPLOADS / name).write_bytes(data)
    return f"{get_settings().public_base_url.rstrip('/')}/uploads/products/{name}"


# --- Ce que la carte envoie ------------------------------------------------------------------------------------------

def card_image(product) -> str | None:
    """Photo portée acceptée → montage ou portée seule ; sinon studio ; sinon la photo du commerçant."""
    return product.image_url


def reset_for_new_source(product, url: str | None) -> None:
    """Nouvelle photo du commerçant (envoi, import, Shopify, Meta) : tout est refait à partir d'elle."""
    if url and url == product.image_original_url:
        return  # même photo d'origine : on garde la retouche
    product.image_url = url
    product.image_original_url = None
    product.image_studio_url = None
    product.image_status = None
    product.worn_status = product.worn_preview_url = product.worn_image_url = product.card_style = None


async def _job(db, tenant_id, product_id, kind, engine, status="PENDING"):
    from app.models.image_job import ImageJob

    job = ImageJob(tenant_id=tenant_id, product_id=product_id, kind=kind, engine=engine, status=status)
    db.add(job)
    await db.flush()
    return job


async def process_white(db, product, tenant, now: datetime | None = None, photoroom=None, cutter=None) -> str:
    """Fond blanc studio d'un produit. Renvoie DONE ou FAILED ; la photo d'origine reste toujours disponible."""
    from app.core.config import get_settings

    settings = get_settings()
    source = product.image_original_url or product.image_url
    if not source:
        return "FAILED"
    use_photoroom = bool(settings.photoroom_api_key) and (await quotas(db, tenant, now))["white_left"] > 0
    engine = "PHOTOROOM" if use_photoroom else "SERVER"
    job = await _job(db, tenant.id, product.id, "WHITE", engine)
    try:
        data = await load_source(source)
        cut_png = None
        if use_photoroom:
            try:
                cut_png = await (photoroom or photoroom_cutout)(data, settings.photoroom_api_key)
                job.cost_usd = PHOTOROOM_COST
            except Exception as exc:  # noqa: BLE001 — Photoroom indisponible : notre serveur prend le relais
                logger.warning("Photoroom indisponible (%s) : détourage par le serveur", type(exc).__name__)
                job.engine, cut_png = "SERVER", None
        if cut_png is None:
            cut_png = await (cutter or server_cutout)(data, settings.photo_model_path)
        url = save(studio(cut_png), "studio")
    except Exception as exc:  # noqa: BLE001 — jamais d'erreur pour le client : sa photo d'origine reste
        job.status, job.error, job.finished_at = "FAILED", str(exc)[:300], datetime.now(timezone.utc)
        product.image_status = "FAILED"
        product.image_original_url = product.image_original_url or source
        await db.commit()
        logger.warning("Retouche impossible (produit %s) : %s", product.id, str(exc)[:200])
        return "FAILED"
    job.status, job.finished_at = "DONE", datetime.now(timezone.utc)
    product.image_original_url = source
    product.image_studio_url = url
    product.image_status = "DONE"
    if not product.worn_image_url:
        product.image_url = url
    else:
        await _refresh_card(product)
    await db.commit()
    return "DONE"


async def run_pending(db, limit: int = 5, now: datetime | None = None) -> dict:
    """Tâche de fond : les photos jamais retouchées (ou à refaire), quelques-unes à la fois."""
    from app.models.product import Product
    from app.models.tenant import Tenant

    rows = (await db.execute(
        select(Product, Tenant).join(Tenant, Tenant.id == Product.tenant_id).where(
            Product.image_url.is_not(None), Product.active.is_(True),
            (Product.image_status.is_(None)) | (Product.image_status == "PENDING"),
            Tenant.is_demo.is_(False))
        .order_by(Product.updated_at.desc()).limit(limit * 4))).all()
    report = {"done": 0, "failed": 0, "skipped": 0}
    for product, tenant in rows:
        if report["done"] + report["failed"] >= limit:
            break
        if not retouches(tenant):
            product.image_status = "ORIGINAL"  # agence, courtier : jamais retouché
            report["skipped"] += 1
            continue
        report["done" if await process_white(db, product, tenant, now) == "DONE" else "failed"] += 1
    await db.commit()
    return report


def use_original(product) -> None:
    """« Revenir à ma photo » : la carte envoie de nouveau la photo du commerçant (et plus de retouche auto)."""
    if product.image_original_url:
        product.image_url = product.image_original_url
    product.image_status = "ORIGINAL"


# --- Photo portée (FASHN) --------------------------------------------------------------------------------------------

def worn_prompt(country: str | None) -> str:
    who = "A Black African fashion model" if (country or "").upper() in AFRICA else "A fashion model"
    return (f"{who}, full body, natural confident pose, wearing the garment exactly as shown (same color, pattern, "
            "cut and details), clean light grey photo studio background, soft studio lighting")


async def fashn_run(image_bytes: bytes, prompt: str, api_key: str) -> str:
    import httpx

    data_uri = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
    body = {"model_name": "product-to-model", "inputs": {
        "product_image": data_uri, "prompt": prompt, "aspect_ratio": "3:4", "generation_mode": "balanced",
        "output_format": "jpeg", "return_base64": True}}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(FASHN_RUN_URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
    payload = response.json() if response.content else {}
    if response.status_code != 200 or payload.get("error") or not payload.get("id"):
        raise RuntimeError(f"FASHN a répondu {response.status_code}")
    return str(payload["id"])


async def fashn_status(prediction_id: str, api_key: str) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(FASHN_STATUS_URL.format(id=prediction_id),
                                    headers={"Authorization": f"Bearer {api_key}"})
    return response.json() if response.content else {}


def _decode_output(output) -> bytes | None:
    first = (output or [None])[0] if isinstance(output, list) else None
    if isinstance(first, str) and first.startswith("data:image/"):
        return base64.b64decode(first.split(",", 1)[1])
    return None


async def request_worn(db, product, tenant, now: datetime | None = None, runner=None) -> dict:
    """Le commerçant demande la photo portée d'un habit (quota du mois) : envoyée à FASHN, résultat à valider."""
    from app.core.config import get_settings

    settings = get_settings()
    if not retouches(tenant) or not is_garment(product):
        return {"error": "La photo portée est réservée aux habits."}
    if plan_of(tenant) == "FREE":
        return {"error": "La photo portée est disponible à partir de la formule Indépendant."}
    if not settings.fashn_api_key:
        return {"error": "La photo portée n'est pas encore activée par l'équipe Bob."}
    if (await quotas(db, tenant, now))["worn_left"] <= 0:
        return {"error": "Plus de photo portée disponible ce mois-ci pour votre formule."}
    source = product.image_studio_url or product.image_original_url or product.image_url
    if not source:
        return {"error": "Ajoutez d'abord une photo du produit."}
    job = await _job(db, tenant.id, product.id, "WORN", "FASHN")
    try:
        job.external_id = await (runner or fashn_run)(await load_source(source), worn_prompt(tenant.country),
                                                      settings.fashn_api_key)
    except Exception as exc:  # noqa: BLE001
        job.status, job.error, job.finished_at = "FAILED", str(exc)[:300], datetime.now(timezone.utc)
        await db.commit()
        return {"error": "La photo portée n'a pas pu être lancée, réessayez dans quelques minutes."}
    product.worn_status = "PENDING"
    await db.commit()
    return {"status": "PENDING"}


async def collect_worn(db, poller=None) -> dict:
    """Tâche de fond : récupère les photos portées terminées chez FASHN (aperçu à valider)."""
    from app.core.config import get_settings
    from app.models.image_job import ImageJob
    from app.models.product import Product

    settings = get_settings()
    jobs = (await db.execute(select(ImageJob).where(ImageJob.kind == "WORN", ImageJob.status == "PENDING",
                                                    ImageJob.external_id.is_not(None)).limit(20))).scalars().all()
    report = {"ready": 0, "failed": 0, "waiting": 0}
    for job in jobs:
        try:
            status = await (poller or fashn_status)(job.external_id, settings.fashn_api_key)
        except Exception:  # noqa: BLE001 — on réessaie au prochain passage
            report["waiting"] += 1
            continue
        product = await db.get(Product, job.product_id) if job.product_id else None
        state = status.get("status")
        if state == "completed" and (image := _decode_output(status.get("output"))):
            job.status, job.cost_usd, job.finished_at = "DONE", FASHN_COST, datetime.now(timezone.utc)
            if product is not None:
                product.worn_preview_url = save(image, "porte-apercu")
                product.worn_status = "REVIEW"
            report["ready"] += 1
        elif state in ("failed", "canceled") or state == "completed":
            job.status, job.finished_at = "FAILED", datetime.now(timezone.utc)
            job.error = str((status.get("error") or {}).get("name") or "sans image")[:300]
            if product is not None:
                product.worn_status = "FAILED"
            report["failed"] += 1
        else:
            report["waiting"] += 1
    await db.commit()
    return report


async def _refresh_card(product) -> None:
    """Après acceptation ou changement de style : l'image de la carte (montage ou portée seule)."""
    if not product.worn_image_url:
        product.image_url = product.image_studio_url or product.image_original_url
        return
    if (product.card_style or "MONTAGE") == "WORN":
        product.image_url = product.worn_image_url
        return
    real = product.image_studio_url or product.image_original_url
    product.image_url = save(montage(await load_source(real), await load_source(product.worn_image_url)), "montage")


async def decide_worn(db, product, accept: bool) -> None:
    if product.worn_status != "REVIEW" or not product.worn_preview_url:
        raise ValueError("Aucune photo portée à valider.")
    if accept:
        product.worn_image_url = product.worn_preview_url
        product.worn_status = "ACCEPTED"
        product.card_style = product.card_style or "MONTAGE"
        await _refresh_card(product)
    else:
        product.worn_status = "REFUSED"
    product.worn_preview_url = None if not accept else product.worn_preview_url
    await db.commit()


async def set_card_style(db, product, style: str) -> None:
    if style not in ("MONTAGE", "WORN"):
        raise ValueError("Choix inconnu.")
    product.card_style = style
    await _refresh_card(product)
    await db.commit()


def view(product, tenant, quota: dict) -> dict:
    return {
        "original_url": product.image_original_url or product.image_url, "studio_url": product.image_studio_url,
        "card_url": product.image_url, "status": product.image_status, "retouched": retouches(tenant),
        "garment": is_garment(product), "worn_status": product.worn_status, "worn_preview_url": product.worn_preview_url,
        "worn_url": product.worn_image_url, "card_style": product.card_style or "MONTAGE",
        "worn_enabled": plan_of(tenant) != "FREE", **quota,
    }
