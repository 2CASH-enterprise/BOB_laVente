"""
Dashboard Super Admin (Étape 3) — remplace les requêtes SQL manuelles pour gérer les
tenants (plan, suspension). Authentification totalement séparée du système tenant
(app.core.security.get_current_superadmin), aucune route existante n'est touchée.
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    CurrentSuperAdmin,
    create_superadmin_access_token,
    get_current_superadmin,
    hash_password,
    verify_password,
)
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.order import Order
from app.models.product import Product
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan
from app.schemas.superadmin import (
    PlatformStats,
    SuperAdminBootstrapRequest,
    SuperAdminLoginResponse,
    TenantActiveUpdate,
    TenantDetailForAdmin,
    TenantPlanUpdate,
    TenantSummaryForAdmin,
)

router = APIRouter(prefix="/api/v1/superadmin", tags=["superadmin"])


@router.post("/bootstrap", response_model=SuperAdminLoginResponse)
async def bootstrap_superadmin(payload: SuperAdminBootstrapRequest, db: AsyncSession = Depends(get_db)):
    """
    Ne fonctionne QUE si aucun compte Super Admin n'existe encore — empêche quiconque
    de créer un accès plateforme après coup. Le tout premier compte se crée ici, une
    seule fois ; les suivants passent par /create-colleague, authentifié.
    """
    count = (await db.execute(select(func.count(SuperAdminUser.id)))).scalar_one()
    if count > 0:
        raise HTTPException(status_code=403, detail="Un compte Super Admin existe déjà — utilisez /create-colleague")

    admin = SuperAdminUser(
        email=payload.email.strip().lower(), hashed_password=hash_password(payload.password), full_name=payload.full_name
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)

    token = create_superadmin_access_token(admin.id)
    return SuperAdminLoginResponse(access_token=token)


@router.post("/login", response_model=SuperAdminLoginResponse)
async def login_superadmin(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    stmt = select(SuperAdminUser).where(SuperAdminUser.email == form_data.username.strip().lower(), SuperAdminUser.active.is_(True))
    admin = (await db.execute(stmt)).scalar_one_or_none()
    if admin is None or not verify_password(form_data.password, admin.hashed_password):
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect")

    token = create_superadmin_access_token(admin.id)
    return SuperAdminLoginResponse(access_token=token)


@router.post("/create-colleague", response_model=SuperAdminLoginResponse)
async def create_colleague(
    payload: SuperAdminBootstrapRequest,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    existing = (await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == payload.email.strip().lower()))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Cet email est déjà utilisé")

    admin = SuperAdminUser(
        email=payload.email.strip().lower(), hashed_password=hash_password(payload.password), full_name=payload.full_name
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)

    token = create_superadmin_access_token(admin.id)
    return SuperAdminLoginResponse(access_token=token)


async def _product_count(db: AsyncSession, tenant_id) -> int:
    stmt = select(func.count(Product.id)).where(Product.tenant_id == tenant_id, Product.active.is_(True))
    return (await db.execute(stmt)).scalar_one()


async def _conversation_count_this_month(db: AsyncSession, tenant_id) -> int:
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count(Conversation.id)).where(Conversation.tenant_id == tenant_id, Conversation.created_at >= month_start)
    return (await db.execute(stmt)).scalar_one()


@router.get("/tenants", response_model=list[TenantSummaryForAdmin])
async def list_tenants(
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    tenants = (await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))).scalars().all()
    results = []
    for tenant in tenants:
        results.append(
            TenantSummaryForAdmin(
                id=tenant.id, name=tenant.name, email=tenant.email, plan=tenant.plan.value,
                is_demo=tenant.is_demo, active=tenant.active,
                product_count=await _product_count(db, tenant.id),
                conversation_count_this_month=await _conversation_count_this_month(db, tenant.id),
                created_at=tenant.created_at,
            )
        )
    return results


@router.get("/tenants/{tenant_id}", response_model=TenantDetailForAdmin)
async def get_tenant_detail(
    tenant_id: UUID,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")

    customer_count = (await db.execute(select(func.count(Customer.id)).where(Customer.tenant_id == tenant.id))).scalar_one()
    order_count = (await db.execute(select(func.count(Order.id)).where(Order.tenant_id == tenant.id))).scalar_one()

    return TenantDetailForAdmin(
        id=tenant.id, name=tenant.name, email=tenant.email, plan=tenant.plan.value,
        is_demo=tenant.is_demo, active=tenant.active,
        product_count=await _product_count(db, tenant.id),
        conversation_count_this_month=await _conversation_count_this_month(db, tenant.id),
        created_at=tenant.created_at, country=tenant.country, currency=tenant.currency,
        customer_count=customer_count, order_count=order_count,
    )


@router.put("/tenants/{tenant_id}/plan", response_model=TenantSummaryForAdmin)
async def update_tenant_plan(
    tenant_id: UUID,
    payload: TenantPlanUpdate,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    try:
        tenant.plan = TenantPlan(payload.plan)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Plan invalide. Valeurs possibles : {[p.value for p in TenantPlan]}") from None

    await db.commit()
    return TenantSummaryForAdmin(
        id=tenant.id, name=tenant.name, email=tenant.email, plan=tenant.plan.value,
        is_demo=tenant.is_demo, active=tenant.active,
        product_count=await _product_count(db, tenant.id),
        conversation_count_this_month=await _conversation_count_this_month(db, tenant.id),
        created_at=tenant.created_at,
    )


@router.put("/tenants/{tenant_id}/active", response_model=TenantSummaryForAdmin)
async def update_tenant_active(
    tenant_id: UUID,
    payload: TenantActiveUpdate,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """Suspendre/réactiver un tenant — le champ `active` existait déjà, jamais exposé jusqu'ici."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    tenant.active = payload.active
    await db.commit()
    return TenantSummaryForAdmin(
        id=tenant.id, name=tenant.name, email=tenant.email, plan=tenant.plan.value,
        is_demo=tenant.is_demo, active=tenant.active,
        product_count=await _product_count(db, tenant.id),
        conversation_count_this_month=await _conversation_count_this_month(db, tenant.id),
        created_at=tenant.created_at,
    )


@router.get("/stats", response_model=PlatformStats)
async def platform_stats(
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    total_tenants = (await db.execute(select(func.count(Tenant.id)))).scalar_one()
    demo_tenants = (await db.execute(select(func.count(Tenant.id)).where(Tenant.is_demo.is_(True)))).scalar_one()

    plan_rows = (await db.execute(select(Tenant.plan, func.count(Tenant.id)).group_by(Tenant.plan))).all()
    tenants_by_plan = {plan.value: count for plan, count in plan_rows}

    total_customers = (await db.execute(select(func.count(Customer.id)))).scalar_one()
    total_orders = (await db.execute(select(func.count(Order.id)))).scalar_one()

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_conversations_this_month = (
        await db.execute(select(func.count(Conversation.id)).where(Conversation.created_at >= month_start))
    ).scalar_one()

    return PlatformStats(
        total_tenants=total_tenants, tenants_by_plan=tenants_by_plan, demo_tenants=demo_tenants,
        total_customers=total_customers, total_conversations_this_month=total_conversations_this_month,
        total_orders=total_orders,
    )
