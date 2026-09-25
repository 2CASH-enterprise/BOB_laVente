"""Textes déterministes liés aux commandes — jamais laissés à la paraphrase du LLM."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Product


async def get_order_item_lines(db: AsyncSession, order_id, currency: str) -> list[str]:
    """Partagé entre confirmation de commande et reçu — jamais dupliqué, jamais désynchronisé."""
    stmt = select(OrderItem, Product.name).join(Product, Product.id == OrderItem.product_id).where(OrderItem.order_id == order_id)
    rows = (await db.execute(stmt)).all()
    return [f"{name} x{item.quantity} — {float(item.subtotal):,.0f} {currency}".replace(",", " ") for item, name in rows]


def generate_order_confirmation_text(order: Order, item_lines: list[str], tenant_name: str, payment_link: str | None) -> str:
    """
    Envoyé UNE SEULE FOIS, juste après la création de la commande — jamais un reçu de
    paiement (aucun paiement n'a encore été confirmé à ce stade, section 13/39).
    """
    order_ref = str(order.id)[:8].upper()
    items_block = "\n".join(f"- {line}" for line in item_lines)
    total_line = f"Total : {float(order.total_amount):,.0f} {order.currency}".replace(",", " ")

    payment_block = (
        f"\n\nVous pouvez régler via : {payment_link}" if payment_link
        else "\n\nContactez-nous pour connaître les moyens de paiement disponibles."
    )

    return (
        f"✅ Commande confirmée — {tenant_name}\n"
        f"Référence : {order_ref}\n\n"
        f"{items_block}\n\n"
        f"{total_line}"
        f"{payment_block}\n\n"
        f"Une fois la capture d'écran du paiement envoyée, je vous enverrai les détails de suivi."
    )


def generate_receipt_text(order: Order, item_lines: list[str], tenant_name: str) -> str:
    """Envoyé UNIQUEMENT après que le commerçant a confirmé avoir reçu le paiement."""
    order_ref = str(order.id)[:8].upper()
    date_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")
    items_block = "\n".join(f"- {line}" for line in item_lines)

    return (
        f"🧾 Reçu de commande — {tenant_name}\n"
        f"Référence : {order_ref}\n"
        f"Date : {date_str}\n\n"
        f"{items_block}\n\n"
        f"Total : {float(order.total_amount):,.0f} {order.currency}\n\n"
        f"_Reçu établi par Bob AI_"
    ).replace(",", " ")
