"""
Codes QR trackés vers un produit vedette (section QR). Le QR imprimé encode toujours
/qr/{code}, jamais un lien wa.me direct — le commerçant peut changer le produit ciblé
sans jamais réimprimer le QR, et chaque scan est compté.
"""
import secrets
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
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


@router.get("/qr/{code}")
async def redirect_qr_code(code: str, db: AsyncSession = Depends(get_db)):
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

    qr.scan_count += 1
    await db.commit()

    digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
    message = quote(f"Bonjour, je suis intéressé(e) par : {product.name}")
    wa_link = f"https://wa.me/{digits}?text={message}"

    return RedirectResponse(url=wa_link, status_code=302)
