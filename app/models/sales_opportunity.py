import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OpportunityOutcome:
    """Issue d'une opportunité — toujours calculée à partir de faits enregistrés, jamais devinée."""

    PAID = "PAID"                    # une commande de l'épisode a été confirmée « Paiement reçu »
    ORDER_UNPAID = "ORDER_UNPAID"    # commande créée, paiement non confirmé
    CANCELLED = "CANCELLED"          # la ou les commandes de l'épisode ont été annulées
    TRANSFERRED = "TRANSFERRED"      # Bob a transmis à un humain, sans commande
    ABANDONED = "ABANDONED"          # pas de commande, client silencieux depuis le seuil
    IN_PROGRESS = "IN_PROGRESS"      # rien de tout cela, client actif récemment

    TERMINATED = frozenset({PAID, ORDER_UNPAID, CANCELLED, TRANSFERRED, ABANDONED})


class SalesOpportunity(Base):
    """
    Phase 0 de l'apprentissage : une opportunité = un épisode d'achat dans une conversation.
    Un client n'a qu'une conversation, qui reste ouverte : un nouvel épisode commence quand
    il écrit après une période de silence (voir opportunity_service).

    Table entièrement RECALCULÉE (nuit + à la demande) à partir des messages et commandes.
    L'identifiant est déterministe (conversation + début de l'épisode) : un recalcul redonne
    toujours les mêmes identifiants, pour que les phases suivantes (intentions, objections,
    stratégies) puissent s'y rattacher durablement.
    """

    __tablename__ = "sales_opportunities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_customer_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    outcome: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    # Contexte — uniquement des faits connus, jamais de données démographiques devinées.
    is_first_opportunity: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))  # source d'acquisition (1re opportunité seulement)
    contact_point_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    is_returning_buyer: Mapped[bool] = mapped_column(Boolean, nullable=False)  # avait déjà une commande payée
    city: Mapped[str | None] = mapped_column(String(255))
    followup_sent: Mapped[bool] = mapped_column(Boolean, nullable=False)

    customer_message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    order_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)  # hors commandes annulées
    paid_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    transfer_reason: Mapped[str | None] = mapped_column(String(300))

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
