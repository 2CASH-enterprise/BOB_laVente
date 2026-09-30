from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

DEFAULT_GREETING = "Bonjour, je souhaite avoir des informations."


class _OwnerFields(BaseModel):
    """Lot 27 — commercial propriétaire du lien (facultatif). Chaîne vide = retirer."""

    owner_name: str | None = Field(default=None, max_length=80)
    owner_email: EmailStr | None = None

    @field_validator("owner_email", mode="before")
    @classmethod
    def _empty_email_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    # Lot 28 — canal où le lien est publié (GOOGLE, YOUTUBE…). Chaîne vide = non précisé.
    channel: str | None = None

    @field_validator("channel", mode="before")
    @classmethod
    def _known_channel(cls, value):
        from app.services.acquisition import normalize_link_channel

        return normalize_link_channel(value)


class ContactPointCreate(_OwnerFields):
    name: str = Field(min_length=1, max_length=80)
    greeting: str = Field(default=DEFAULT_GREETING, min_length=1, max_length=300)
    position: Literal["LEFT", "RIGHT"] = "RIGHT"


class ContactPointUpdate(_OwnerFields):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    greeting: str | None = Field(default=None, min_length=1, max_length=300)
    position: Literal["LEFT", "RIGHT"] | None = None
    active: bool | None = None


class ContactPointResponse(BaseModel):
    id: UUID
    code: str
    name: str
    greeting: str
    position: str
    active: bool
    click_count: int
    customer_count: int  # clients arrivés par ce point de contact
    owner_name: str | None = None
    owner_email: str | None = None
    channel: str | None = None
    channel_label: str | None = None
    short_path: str  # ex. "/w/Xk3p9Qa" — à préfixer par le domaine public côté client
    created_at: datetime | None = None


class ContactPointLimits(BaseModel):
    used: int
    max: int | None  # None = illimité
