"""
Lot 50 — garde-fous de coût de l'IA.

- Limite par client : au-delà de MESSAGES_PER_HOUR messages en une heure (spam, robot, copier-coller
  en rafale), Bob ne consulte plus l'IA. Le client reçoit UNE fois un message fixe, puis plus rien
  jusqu'à ce que l'heure soit passée ; ses messages restent enregistrés et visibles par la boutique.
- Alerte de budget : voir app/workers/llm_budget.py.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Message, MessageSender

MESSAGES_PER_HOUR = 30
WINDOW = timedelta(hours=1)
OK, NOTIFY, SILENT = "OK", "NOTIFY", "SILENT"

RATE_LIMIT_MESSAGE = ("Vous avez envoyé beaucoup de messages en peu de temps. Je fais une petite pause : "
                      "écrivez-moi à nouveau dans une heure et je reprendrai notre échange avec plaisir.")
RATE_LIMIT_MESSAGE_TU = ("Tu as envoyé beaucoup de messages en peu de temps. Je fais une petite pause : "
                         "écris-moi à nouveau dans une heure et je reprendrai notre échange avec plaisir.")


async def customer_rate_limit(db: AsyncSession, tenant_id, customer_id, now: datetime | None = None) -> str:
    """OK : Bob répond normalement ; NOTIFY : message fixe (une fois) ; SILENT : aucune réponse."""
    since = (now or datetime.now(timezone.utc)) - WINDOW

    def of_customer(stmt):
        return stmt.join(Conversation, Conversation.id == Message.conversation_id).where(
            Message.tenant_id == tenant_id, Conversation.tenant_id == tenant_id,
            Conversation.customer_id == customer_id, Message.created_at >= since,
        )

    count = (await db.execute(of_customer(select(func.count(Message.id))).where(
        Message.sender == MessageSender.CUSTOMER))).scalar_one()
    if count <= MESSAGES_PER_HOUR:
        return OK
    already = (await db.execute(of_customer(select(func.count(Message.id))).where(
        Message.message_type == "rate_limited"))).scalar_one()
    return SILENT if already else NOTIFY
