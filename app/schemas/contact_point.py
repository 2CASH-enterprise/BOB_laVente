from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

DEFAULT_GREETING = "Bonjour, je souhaite avoir des informations."


class ContactPointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    greeting: str = Field(default=DEFAULT_GREETING, min_length=1, max_length=300)
    position: Literal["LEFT", "RIGHT"] = "RIGHT"


class ContactPointUpdate(BaseModel):
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
    short_path: str  # ex. "/w/Xk3p9Qa" — à préfixer par le domaine public côté client
    created_at: datetime | None = None


class ContactPointLimits(BaseModel):
    used: int
    max: int | None  # None = illimité
