"""
Lot 104 — des photos que WhatsApp accepte toujours (03/11).

Les messages WhatsApp avec photo (fiche à boutons, cartes qui défilent) n'acceptent que le JPEG et le PNG. Une photo
.webp (ou .gif, .heic…) est acceptée par l'API puis jamais livrée : le client ne reçoit rien. Désormais :
- toute photo enregistrée par Bob (tableau de bord, assistant du commerçant) est convertie en JPEG si besoin ;
- les photos .webp déjà enregistrées sont converties une fois pour toutes (`python -m app.services.photo_formats`) ;
- une adresse d'image qui n'est visiblement pas un JPEG ou un PNG part sans photo (voir agents.tools.has_sendable_photo).
"""
import io
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

SENDABLE_SUFFIXES = (".jpg", ".jpeg", ".png")
REFUSED_SUFFIXES = (".webp", ".gif", ".svg", ".avif", ".heic", ".heif", ".bmp", ".tif", ".tiff")
UPLOADS = Path(__file__).resolve().parent.parent / "static" / "uploads" / "products"
_LOCAL = re.compile(r"/uploads/products/([A-Za-z0-9_-]+)\.(webp|gif|avif|bmp|tiff?)$", re.IGNORECASE)
PHOTO_COLUMNS = ("image_url", "image_original_url", "image_studio_url", "worn_preview_url", "worn_image_url")


def suffix_of(url: str | None) -> str:
    path = (url or "").split("?", 1)[0].split("#", 1)[0].lower()
    name = path.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def refused_by_whatsapp(url: str | None) -> bool:
    """Une adresse dont l'extension dit clairement « pas JPEG ni PNG » (sans extension : on laisse passer)."""
    return suffix_of(url) in REFUSED_SUFFIXES


def to_jpeg(content: bytes) -> bytes:
    """N'importe quelle image lisible → JPEG (transparence posée sur du blanc)."""
    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        image.load()
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            ground = Image.new("RGB", image.size, (255, 255, 255))
            ground.paste(image, mask=image.split()[-1])
            image = ground
        elif image.mode != "RGB":
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90, optimize=True)
        return out.getvalue()


def sendable(content: bytes, ext: str) -> tuple[bytes, str]:
    """(contenu, extension) prêts pour WhatsApp : JPEG et PNG gardés tels quels, le reste converti en JPEG."""
    ext = ext.lower().lstrip(".")
    if ext in ("jpg", "jpeg", "png"):
        return content, "jpg" if ext == "jpeg" else ext
    return to_jpeg(content), "jpg"


async def convert_existing(db, uploads: Path = UPLOADS) -> int:
    """Convertit en JPEG les photos .webp (etc.) déjà enregistrées par Bob et met les adresses à jour. Rejouable."""
    from sqlalchemy import or_, select

    from app.models.product import Product

    columns = [getattr(Product, c) for c in PHOTO_COLUMNS]
    rows = (await db.execute(select(Product).where(or_(*[
        or_(*[col.ilike(f"%.{ext}") for ext in ("webp", "gif", "avif", "bmp", "tif", "tiff")]) for col in columns
    ])))).scalars().all()
    done: dict[str, str] = {}
    changed = 0
    for product in rows:
        for column in PHOTO_COLUMNS:
            url = getattr(product, column)
            match = _LOCAL.search(url or "")
            if not match:
                continue
            if url not in done:
                source = uploads / f"{match.group(1)}.{match.group(2)}"
                target = uploads / f"{match.group(1)}.jpg"
                try:
                    if not target.exists():
                        target.write_bytes(to_jpeg(source.read_bytes()))
                except Exception:  # noqa: BLE001 — fichier absent ou illisible : l'adresse reste, la photo partira sans image
                    logger.warning("Photo %s non convertie", source.name)
                    continue
                done[url] = url[:match.start()] + f"/uploads/products/{match.group(1)}.jpg"
            setattr(product, column, done[url])
            changed += 1
    await db.commit()
    return changed


if __name__ == "__main__":  # docker compose exec api python -m app.services.photo_formats
    import asyncio

    from app.core.database import AsyncSessionLocal

    async def _main() -> None:
        async with AsyncSessionLocal() as db:
            print(f"Adresses de photos converties en JPEG : {await convert_existing(db)}")

    asyncio.run(_main())
