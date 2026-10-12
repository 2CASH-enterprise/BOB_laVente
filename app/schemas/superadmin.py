from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class SuperAdminBootstrapRequest(BaseModel):
    email: str
    password: str
    full_name: str


class SuperAdminLoginResponse(BaseModel):
    access_token: str | None = None  # lot 70 : absent tant que le code envoyé par email n'est pas saisi
    token_type: str = "bearer"
    mfa_required: bool = False
    mfa_pending_token: str | None = None


class SuperAdminVerifyRequest(BaseModel):  # lot 70
    mfa_pending_token: str
    code: str


class SuperAdminResendRequest(BaseModel):  # lot 70
    mfa_pending_token: str


class TenantSummaryForAdmin(BaseModel):
    id: UUID
    name: str
    email: str
    plan: str
    is_demo: bool
    active: bool
    product_count: int
    conversation_count_this_month: int
    commission_rate: float | None
    business_type: str = "ONLINE_STORE"  # lot 32
    created_at: datetime
    # Lot 51 — abonnement : dernier jour payé (vide = pas d'échéance) et état de Bob (pause, rappel).
    paid_until: date | None = None
    bob_status: dict | None = None
    # Lot 66 — cartes de la page Boutiques : pays, téléphone, pré-inscrit (accès pas encore ouvert).
    country: str | None = None
    phone: str | None = None
    prereg_locked: bool = False


class TenantDetailForAdmin(TenantSummaryForAdmin):
    currency: str
    customer_count: int
    order_count: int
    total_commission_due: float


class TenantPlanUpdate(BaseModel):
    plan: str


class TenantCommissionRateUpdate(BaseModel):
    commission_rate: float | None


class TenantBusinessTypeUpdate(BaseModel):
    business_type: str


class TenantActiveUpdate(BaseModel):
    active: bool


class TenantPaidUntilUpdate(BaseModel):
    paid_until: date | None


class PlatformStats(BaseModel):
    total_tenants: int
    tenants_by_plan: dict
    demo_tenants: int
    total_customers: int
    total_conversations_this_month: int
    total_orders: int
