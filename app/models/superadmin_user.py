import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SuperAdminUser(Base):
    """
    Compte plateforme, indépendant de tout tenant (contrairement à User). Table et
    authentification totalement séparées — aucune route existante n'est touchée par
    cet ajout, zéro risque sur ce qui fonctionne déjà.
    """

    __tablename__ = "superadmin_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Lot 70 — code de connexion à 6 chiffres envoyé par email (haché, valable 10 minutes) et dernière connexion.
    mfa_otp_code_hash: Mapped[str | None] = mapped_column(String(64))
    mfa_otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lot 119 — rôles (plusieurs possibles, voir services/sa_roles) ; par défaut « Lecture seule », jamais plus.
    roles: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: ["READONLY"])
    # Lot 119 — invitation par e-mail : lien valable 48 h (empreinte seule), la personne choisit son mot de passe.
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
