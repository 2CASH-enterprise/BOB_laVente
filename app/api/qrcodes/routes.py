"""
Codes QR trackés vers un produit vedette (section QR). Le QR imprimé encode toujours
/qr/{code}, jamais un lien wa.me direct — le commerçant peut changer le produit ciblé
sans jamais réimprimer le QR, et chaque scan est compté.
"""
import secrets
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.product import Product
from app.models.product_qr_code import ProductQrCode
from app.models.whatsapp_account import WhatsAppAccount
from app.schemas.qrcode import QrCodeCreate, QrCodeResponse
from app.services.plan_limits import can_create_qr_code

router = APIRouter(tags=["qrcodes"])

CODE_LENGTH = 8


def _generate_code() -> str:
    return secrets.token_urlsafe(6)[:CODE_LENGTH]


@router.get("/api/v1/qr-codes", response_model=list[QrCodeResponse])
async def list_qr_codes(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ProductQrCode, Product).where(ProductQrCode.tenant_id == current_user.tenant_id).join(
        Product, Product.id == ProductQrCode.product_id
    )
    rows = (await db.execute(stmt)).all()
    return [
        QrCodeResponse(
            id=qr.id, code=qr.code, product_id=qr.product_id, product_name=product.name,
            product_image_url=product.image_url, product_price=float(product.price), product_currency=product.currency,
            scan_count=qr.scan_count, short_path=f"/qr/{qr.code}",
        )
        for qr, product in rows
    ]


@router.post(
    "/api/v1/qr-codes", response_model=QrCodeResponse, status_code=201, dependencies=[Depends(require_role("MANAGER"))]
)
async def create_qr_code(
    payload: QrCodeCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await can_create_qr_code(db, current_user.tenant_id):
        raise HTTPException(status_code=403, detail="Limite de 5 codes QR atteinte pour cette entreprise")

    product_stmt = select(Product).where(Product.tenant_id == current_user.tenant_id, Product.id == payload.product_id)
    product = (await db.execute(product_stmt)).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")

    code = _generate_code()
    qr = ProductQrCode(tenant_id=current_user.tenant_id, product_id=product.id, code=code)
    db.add(qr)
    await db.commit()
    await db.refresh(qr)

    return QrCodeResponse(
        id=qr.id, code=qr.code, product_id=qr.product_id, product_name=product.name,
        product_image_url=product.image_url, product_price=float(product.price), product_currency=product.currency,
        scan_count=qr.scan_count, short_path=f"/qr/{qr.code}",
    )


@router.delete("/api/v1/qr-codes/{qr_id}", status_code=204, dependencies=[Depends(require_role("MANAGER"))])
async def delete_qr_code(
    qr_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ProductQrCode).where(ProductQrCode.tenant_id == current_user.tenant_id, ProductQrCode.id == qr_id)
    qr = (await db.execute(stmt)).scalar_one_or_none()
    if qr is None:
        raise HTTPException(status_code=404, detail="Code QR introuvable")
    await db.delete(qr)
    await db.commit()


@router.get("/api/v1/qr-codes/{qr_id}/poster", response_class=HTMLResponse)
async def qr_code_poster(
    qr_id: UUID,
    model: str = "vitre",
    accent: str = "or",
    banner: str = "jour",
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lot 103 — affiche du bien (A4) ou autocollant vitre (20 × 20 cm) avec le QR code de ce produit."""
    from app.core.config import get_settings
    from app.models.tenant import Tenant
    from app.services import launch_kit, vitrine_posters

    stmt = select(ProductQrCode).where(ProductQrCode.tenant_id == current_user.tenant_id, ProductQrCode.id == qr_id)
    qr = (await db.execute(stmt)).scalar_one_or_none()
    product = await db.get(Product, qr.product_id) if qr is not None else None
    if qr is None or product is None:
        raise HTTPException(status_code=404, detail="Code QR introuvable")
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == qr.tenant_id))).scalar_one_or_none()
    if account is None or not account.display_phone_number:
        raise HTTPException(status_code=409, detail="Connectez d'abord votre numéro WhatsApp (Intégrations)")
    tenant = await db.get(Tenant, qr.tenant_id)
    svg = launch_kit.qr_svg(f"{get_settings().public_base_url.rstrip('/')}/qr/{qr.code}")
    try:
        if model == "plat":  # lot 108 b : affiche d'un plat (plat du jour, spécialité, nouveauté)
            from app.services import resto_posters
            from app.services.business_type import is_restaurant

            if not is_restaurant(tenant):
                raise vitrine_posters.PosterError("L'affiche d'un plat est réservée aux restaurants.")
            page = resto_posters.dish_poster(tenant, product, account.display_phone_number, svg, accent, banner)
        else:
            page = vitrine_posters.product_poster(tenant, product, account.display_phone_number, svg, model, accent)
    except vitrine_posters.PosterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/qr/{code}")
async def redirect_qr_code(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Endpoint public (aucune authentification) — c'est la cible physique du QR imprimé.
    Incrémente le compteur puis redirige vers WhatsApp avec le produit pré-rempli.
    """
    stmt = select(ProductQrCode).where(ProductQrCode.code == code)
    qr = (await db.execute(stmt)).scalar_one_or_none()
    if qr is None:
        raise HTTPException(status_code=404, detail="Code QR introuvable")

    product = await db.get(Product, qr.product_id)
    account_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == qr.tenant_id)
    account = (await db.execute(account_stmt)).scalar_one_or_none()

    if account is None or not account.display_phone_number or product is None:
        raise HTTPException(
            status_code=404, detail="Ce commerce n'a pas encore configuré son numéro WhatsApp pour ce QR"
        )

    from app.services import link_preview

    crawler = link_preview.is_crawler(request.headers.get("user-agent"))
    if not crawler:  # lot 101 : l'aperçu d'un lien partagé n'est pas un scan
        qr.scan_count += 1
        await db.commit()

    digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
    # La référence [QR:code] permet au webhook de savoir exactement quel QR a généré ce
    # premier contact (section CRM.7) — jamais une supposition, une vraie traçabilité.
    message = quote(f"Bonjour, je suis intéressé(e) par : {product.name} [QR:{qr.code}]")
    wa_link = f"https://wa.me/{digits}?text={message}"
    # Lot 101 : une petite page avec l'aperçu du produit (photo, titre, prix), qui ouvre WhatsApp aussitôt.
    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, qr.tenant_id)
    shop = tenant.name if tenant else ""
    return link_preview.page(*link_preview.product_preview(product, shop), wa_link, shop)
