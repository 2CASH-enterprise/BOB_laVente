"""
Règles du plan freemium — un seul endroit pour toutes les limites, jamais dupliquées
ailleurs dans le code. Un tenant est freemium tant que tenant.is_paid est False
(hors tenants de démo, qui ne sont soumis à aucune de ces limites : is_demo est
vérifié séparément par les appelants).
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation
from app.models.product import Product
from app.models.product_qr_code import ProductQrCode

FREEMIUM_MAX_PRODUCTS = 10
FREEMIUM_MAX_CONVERSATIONS_PER_MONTH = 100
MAX_QR_CODES_PER_TENANT = 5
FREEMIUM_MAX_CONTACT_POINTS = 1
PAID_MAX_CONTACT_POINTS = 10

FREEMIUM_QUOTA_MESSAGE = (
    "Merci pour votre message ! Notre service est temporairement limité pour ce mois-ci. "
    "Contactez directement {company_name} pour continuer, ou réessayez dans quelques jours."
)


async def count_active_products(db: AsyncSession, tenant_id) -> int:
    stmt = select(func.count(Product.id)).where(Product.tenant_id == tenant_id, Product.active.is_(True))
    return (await db.execute(stmt)).scalar_one()


async def remaining_product_slots(db: AsyncSession, tenant_id, is_paid: bool) -> int | None:
    """None = illimité (plan payant). Sinon, nombre de produits encore autorisables."""
    if is_paid:
        return None
    current = await count_active_products(db, tenant_id)
    return max(FREEMIUM_MAX_PRODUCTS - current, 0)


async def count_conversations_this_month(db: AsyncSession, tenant_id) -> int:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count(Conversation.id)).where(
        Conversation.tenant_id == tenant_id, Conversation.created_at >= month_start
    )
    return (await db.execute(stmt)).scalar_one()


async def is_conversation_quota_exceeded(db: AsyncSession, tenant_id, is_paid: bool) -> bool:
    if is_paid:
        return False
    count = await count_conversations_this_month(db, tenant_id)
    return count > FREEMIUM_MAX_CONVERSATIONS_PER_MONTH


async def count_qr_codes(db: AsyncSession, tenant_id) -> int:
    stmt = select(func.count(ProductQrCode.id)).where(ProductQrCode.tenant_id == tenant_id)
    return (await db.execute(stmt)).scalar_one()


async def can_create_qr_code(db: AsyncSession, tenant_id) -> bool:
    """Plafond de 5 QR par tenant, freemium ET payant — décision produit assumée (section QR)."""
    return await count_qr_codes(db, tenant_id) < MAX_QR_CODES_PER_TENANT


def max_contact_points(tenant) -> int | None:
    """Liens/widgets : 1 en freemium, 10 en payant ; None = illimité (tenant de démo)."""
    if tenant.is_demo:
        return None
    return PAID_MAX_CONTACT_POINTS if tenant.is_paid else FREEMIUM_MAX_CONTACT_POINTS


async def count_contact_points(db: AsyncSession, tenant_id) -> int:
    """Les points de contact archivés ne comptent plus dans la limite."""
    stmt = select(func.count(ContactPoint.id)).where(
        ContactPoint.tenant_id == tenant_id, ContactPoint.archived_at.is_(None)
    )
    return (await db.execute(stmt)).scalar_one()


async def can_create_contact_point(db: AsyncSession, tenant) -> bool:
    limit = max_contact_points(tenant)
    return limit is None or await count_contact_points(db, tenant.id) < limit
