"""
Réponse d'un humain au client depuis le tableau de bord (lot 23b).

Avant ce lot, un message écrit par le commerçant était enregistré mais jamais envoyé sur
WhatsApp. Règles appliquées ici :
- un message libre ne part que dans les 20 h qui suivent le dernier message du client.
  Meta accepte 24 h (au-delà, seul un modèle payant est possible) : la marge de 4 h évite
  qu'un message parte à la limite et soit refusé ou facturé. La fenêtre ne s'ouvre QUE
  sur un message du client, jamais sur un message de Bob ou d'un humain ;
- le message n'est enregistré dans la conversation QUE si Meta l'a accepté : jamais de
  « envoyé » affiché pour un message qui n'est pas parti.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Message, MessageSender

# Fenêtre de Meta : 24 h. Fenêtre de Bob : 20 h, choix du commerçant (lot 23b) pour garder une marge.
HUMAN_REPLY_WINDOW = timedelta(hours=20)


class HumanReplyError(Exception):
    def __init__(self, message: str, http_status: int):
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def _as_utc(value: datetime) -> datetime:
    # SQLite (tests) renvoie des dates sans fuseau, toujours en UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def reply_window_closes_at(db: AsyncSession, conversation: Conversation) -> datetime | None:
    """Fin de la fenêtre de 20 h ouverte par le dernier message du client ; None s'il n'a jamais écrit."""
    stmt = select(func.max(Message.created_at)).where(
        Message.tenant_id == conversation.tenant_id,
        Message.conversation_id == conversation.id,
        Message.sender == MessageSender.CUSTOMER,
    )
    last_customer_message = (await db.execute(stmt)).scalar_one_or_none()
    if last_customer_message is None:
        return None
    return _as_utc(last_customer_message) + HUMAN_REPLY_WINDOW


async def ensure_reply_window_open(db: AsyncSession, conversation: Conversation, now: datetime | None = None) -> None:
    closes_at = await reply_window_closes_at(db, conversation)
    now = now or datetime.now(timezone.utc)
    if closes_at is None or now >= closes_at:
        raise HumanReplyError(
            "Le client ne vous a pas écrit depuis plus de 20 h : pour éviter tout refus ou frais WhatsApp, "
            "la réponse libre est fermée. Elle se rouvrira dès que le client vous réécrira.",
            409,
        )
