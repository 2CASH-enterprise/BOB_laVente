"""
Lot 73 — effacement des démos non transformées en compte, 30 jours après leur création.

- Seules les boutiques is_demo=True sont concernées ; une démo transformée en compte (promote) n'est plus is_demo.
- La base efface en cascade tout ce qui appartient à la démo (produits, conversations, commandes…).
- La trace « a essayé la démo » du prospect reste (prospects.demo_at ; le lien vers la démo devient vide),
  comme le comptage des démos par lien de publication (ad_link_events.tenant_id devient vide).
- Les photos de produits envoyées dans la démo sont effacées du disque si aucune autre boutique ne les utilise.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select, update

logger = logging.getLogger(__name__)

DEMO_RETENTION_DAYS = 30
BATCH = 200
UPLOADS = Path(__file__).resolve().parents[1] / "static" / "uploads" / "products"


NO_ACTION_LINKS = {
    ("categories", "parent_id"), ("conversations", "assigned_agent"), ("email_campaigns", "created_by"),
    ("negotiations", "product_id"), ("order_items", "product_id"), ("products", "category_id"),
}


def _upload_name(url: str | None) -> str | None:
    marker = "/uploads/products/"
    if not url or marker not in url:
        return None
    name = url.rsplit(marker, 1)[1].split("?", 1)[0]
    return name if name and "/" not in name and ".." not in name else None


async def purge_demos(db, now: datetime | None = None, uploads: Path | None = None) -> dict:
    from app.models.audit_log import AuditLog
    from app.models.category import Category
    from app.models.conversation import Conversation
    from app.models.email_campaign import EmailCampaign
    from app.models.negotiation import Negotiation
    from app.models.order import Order, OrderItem
    from app.models.messaging_settings import MessageSendAudit
    from app.models.product import Product
    from app.models.tenant import Tenant

    now = now or datetime.now(timezone.utc)
    uploads = uploads or UPLOADS
    limit = now - timedelta(days=DEMO_RETENTION_DAYS)
    ids = (await db.execute(select(Tenant.id).where(Tenant.is_demo.is_(True), Tenant.created_at < limit)
                            .order_by(Tenant.created_at).limit(BATCH))).scalars().all()
    report = {"demos": 0, "photos": 0, "errors": 0}
    for tenant_id in ids:
        try:
            urls = (await db.execute(select(Product.image_url).where(Product.tenant_id == tenant_id))).scalars().all()
            names = {n for n in map(_upload_name, urls) if n}
            # Liens internes sans cascade (NO ACTION) : PostgreSQL refuse la cascade s'ils sont encore là.
            # NO_ACTION_LINKS (plus bas) les liste ; un test vérifie que la liste suit le schéma.
            orders = select(Order.id).where(Order.tenant_id == tenant_id)
            await db.execute(delete(OrderItem).where(OrderItem.order_id.in_(orders)))
            await db.execute(delete(Negotiation).where(Negotiation.tenant_id == tenant_id))
            await db.execute(delete(EmailCampaign).where(EmailCampaign.tenant_id == tenant_id))
            await db.execute(update(Conversation).where(Conversation.tenant_id == tenant_id).values(assigned_agent=None))
            await db.execute(update(Product).where(Product.tenant_id == tenant_id).values(category_id=None))
            await db.execute(update(Category).where(Category.tenant_id == tenant_id).values(parent_id=None))
            await db.execute(delete(MessageSendAudit).where(MessageSendAudit.tenant_id == tenant_id))
            await db.execute(delete(AuditLog).where(AuditLog.tenant_id == tenant_id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id, Tenant.is_demo.is_(True)))
            await db.commit()
        except Exception:  # noqa: BLE001 — une démo qui résiste n'empêche pas les autres
            await db.rollback()
            report["errors"] += 1
            logger.exception("Effacement de la démo %s impossible", tenant_id)
            continue
        report["demos"] += 1
        for name in names:
            still_used = (await db.execute(select(Product.id).where(Product.image_url.like(f"%/uploads/products/{name}%"))
                                           .limit(1))).first()
            path = uploads / name
            if not still_used and path.is_file():
                try:
                    path.unlink()
                    report["photos"] += 1
                except OSError:
                    logger.warning("Photo de démo %s non effacée", name)
    if report["demos"] or report["errors"]:
        logger.info("Démos effacées après %s jours : %s", DEMO_RETENTION_DAYS, report)
    return report
