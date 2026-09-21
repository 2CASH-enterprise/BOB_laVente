"""
Négociation de prix avec l'acheteur final (section 52).

Principes stricts :
- Bob commence toujours proche du prix catalogue et cède progressivement, jamais l'inverse.
- Jamais en dessous du prix plancher calculé à partir de max_discount_pct.
- Au-delà de max_rounds sans accord, transfert humain automatique (jamais de négociation infinie).
- Le prix catalogue et le plancher viennent TOUJOURS de la base, jamais du LLM (section 33).
"""
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.negotiation import Negotiation, NegotiationStatus
from app.models.negotiation_settings import TenantNegotiationSettings
from app.models.product import Product


class NegotiationDecision(StrEnum):
    ACCEPT = "ACCEPT"
    COUNTER = "COUNTER"
    REJECT = "REJECT"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"


class NegotiationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


async def _get_or_create_negotiation(db: AsyncSession, tenant_id, conversation_id, product: Product) -> Negotiation:
    stmt = select(Negotiation).where(
        Negotiation.tenant_id == tenant_id,
        Negotiation.conversation_id == conversation_id,
        Negotiation.product_id == product.id,
        Negotiation.status == NegotiationStatus.IN_PROGRESS,
    )
    negotiation = (await db.execute(stmt)).scalar_one_or_none()
    if negotiation is not None:
        return negotiation

    negotiation = Negotiation(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        product_id=product.id,
        initial_price=product.price,
        last_proposed_price=product.price,
        rounds=0,
    )
    db.add(negotiation)
    await db.flush()
    return negotiation


async def negotiate_price(
    db: AsyncSession,
    tenant_id,
    conversation: Conversation,
    product_id,
    customer_offer: Decimal,
    settings: TenantNegotiationSettings,
) -> dict:
    product_stmt = select(Product).where(Product.tenant_id == tenant_id, Product.id == product_id, Product.active.is_(True))
    product = (await db.execute(product_stmt)).scalar_one_or_none()
    if product is None:
        raise NegotiationError("Produit introuvable ou indisponible")

    if customer_offer <= 0:
        raise NegotiationError("L'offre doit être positive")

    negotiation = await _get_or_create_negotiation(db, tenant_id, conversation.id, product)

    catalog_price = Decimal(str(product.price))
    min_price = (catalog_price * (Decimal("1") - Decimal(str(settings.max_discount_pct)) / Decimal("100"))).quantize(
        Decimal("0.01")
    )

    # L'offre du client couvre déjà le prix catalogue : rien à négocier, on accepte tel quel.
    if customer_offer >= catalog_price:
        return await _resolve(db, negotiation, NegotiationDecision.ACCEPT, catalog_price, min_price)

    # L'offre respecte déjà le prix plancher : accepté directement, jamais besoin de round supplémentaire.
    if customer_offer >= min_price:
        return await _resolve(db, negotiation, NegotiationDecision.ACCEPT, customer_offer, min_price)

    negotiation.rounds += 1

    if negotiation.rounds > settings.max_rounds:
        negotiation.status = NegotiationStatus.ESCALATED
        conversation.status = ConversationStatus.WAITING_HUMAN
        db.add(
            Message(
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                sender=MessageSender.SYSTEM,
                message_type="negotiation_escalated",
                content=f"Négociation transférée à un humain : offre {customer_offer} en dessous du plancher {min_price} après {settings.max_rounds} rounds.",
            )
        )
        await db.flush()
        return {
            "decision": NegotiationDecision.ESCALATE_HUMAN.value,
            "reason": "Nombre maximal de rounds atteint sans accord",
            "min_price": float(min_price),
        }

    # Concession progressive : on répartit l'écart restant jusqu'au plancher sur les rounds
    # restants, jamais un saut direct au plancher (section 52.3 : céder progressivement).
    rounds_left = max(settings.max_rounds - negotiation.rounds + 1, 1)
    current = Decimal(str(negotiation.last_proposed_price))
    step = (current - min_price) / Decimal(rounds_left)
    proposed = max(current - step, min_price).quantize(Decimal("0.01"))

    negotiation.last_proposed_price = proposed
    await db.flush()

    return {
        "decision": NegotiationDecision.COUNTER.value,
        "proposed_price": float(proposed),
        "min_price": float(min_price),
        "currency": product.currency,
        "discount_pct": float(((catalog_price - proposed) / catalog_price * 100).quantize(Decimal("0.1"))),
        "remaining_rounds": max(settings.max_rounds - negotiation.rounds, 0),
    }


async def _resolve(
    db: AsyncSession,
    negotiation: Negotiation,
    decision: NegotiationDecision,
    price: Decimal,
    min_price: Decimal,
) -> dict:
    negotiation.status = NegotiationStatus.ACCEPTED
    negotiation.final_price = price
    await db.flush()
    return {
        "decision": decision.value,
        "final_price": float(price),
        "min_price": float(min_price),
    }
