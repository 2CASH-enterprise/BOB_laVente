from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SuperAdminBootstrapRequest(BaseModel):
    email: str
    password: str
    full_name: str


class SuperAdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TenantSummaryForAdmin(BaseModel):
    id: UUID
    name: str
    email: str
    plan: str
    is_demo: bool
    active: bool
    product_count: int
    conversation_count_this_month: int
    created_at: datetime


class TenantDetailForAdmin(TenantSummaryForAdmin):
    country: str
    currency: str
    customer_count: int
    order_count: int


class TenantPlanUpdate(BaseModel):
    plan: str


class TenantActiveUpdate(BaseModel):
    active: bool


class PlatformStats(BaseModel):
    total_tenants: int
    tenants_by_plan: dict
    demo_tenants: int
    total_customers: int
    total_conversations_this_month: int
    total_orders: int
