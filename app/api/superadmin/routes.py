"""
Dashboard Super Admin (Étape 3) — remplace les requêtes SQL manuelles pour gérer les
tenants (plan, suspension, commission). Authentification totalement séparée du système
tenant (app.core.security.get_current_superadmin), aucune route existante n'est touchée.
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import bob_pause
from app.services.business_type import normalize as normalize_business_type
from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import (
    CurrentSuperAdmin,
    create_superadmin_access_token,
    create_superadmin_mfa_pending_token,
    decode_superadmin_mfa_pending_token,
    get_current_superadmin,
    hash_password,
    verify_password,
)
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.order import Order
from app.models.order_commission import OrderCommission
from app.models.product import Product
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan
from app.schemas.superadmin import (
    PlatformStats,
    SuperAdminBootstrapRequest,
    SuperAdminLoginResponse,
    SuperAdminResendRequest,
    SuperAdminVerifyRequest,
    TenantActiveUpdate,
    TenantBusinessTypeUpdate,
    TenantCommissionRateUpdate,
    TenantDetailForAdmin,
    TenantPaidUntilUpdate,
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


# Lot 70 — connexion Super Admin protégée comme celle des commerçants, en plus strict : essais limités par email ET
# par adresse, code à 6 chiffres par email (valable 10 minutes, 5 essais), email d'alerte à chaque connexion réussie.
SA_LOGIN_EMAIL_LIMIT, SA_LOGIN_EMAIL_WINDOW = 5, 900
SA_LOGIN_IP_LIMIT, SA_LOGIN_IP_WINDOW = 10, 60
SA_TOO_MANY = "Trop de tentatives, réessayez dans quelques minutes"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _send_code(admin: SuperAdminUser) -> bool:
    from datetime import timedelta

    from app.services.email_service import send_email
    from app.services.otp_service import generate_otp, hash_otp

    code = generate_otp()
    admin.mfa_otp_code_hash = hash_otp(code)
    admin.mfa_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    return bool(send_email(
        to=admin.email, subject="Votre code de connexion au Super Admin de Bob",
        body=(f"Bonjour,\n\nVoici votre code de connexion au Super Admin de Bob :\n\n{code}\n\nIl expire dans 10 minutes. "
              "Si vous n'êtes pas à l'origine de cette connexion, changez votre mot de passe tout de suite : "
              "quelqu'un le connaît."),
    ))


def _alert_login(admin: SuperAdminUser, request: Request, now: datetime) -> None:
    from app.services.email_service import send_email
    from app.services.local_time import as_utc

    when = as_utc(now).strftime("%d/%m/%Y à %H:%M") + " (heure UTC)"
    agent = (request.headers.get("user-agent") or "inconnu")[:200]
    send_email(to=admin.email, subject="Bob : nouvelle connexion au Super Admin",
               body=(f"Bonjour,\n\nUne connexion au Super Admin de Bob avec votre compte vient d'avoir lieu.\n\n"
                     f"Date : {when}\nAdresse : {_ip(request)}\nNavigateur : {agent}\n\n"
                     "Si c'est vous, il n'y a rien à faire. Sinon, changez votre mot de passe tout de suite."))


async def _open_session(db: AsyncSession, admin: SuperAdminUser, request: Request) -> SuperAdminLoginResponse:
    from app.services.audit import log_audit_event

    now = datetime.now(timezone.utc)
    admin.mfa_otp_code_hash = None
    admin.mfa_otp_expires_at = None
    admin.last_login_at = now
    await log_audit_event(db, actor=f"superadmin:{admin.id}", action="SUPERADMIN_LOGIN_SUCCESS", ip_address=_ip(request))
    await db.commit()
    _alert_login(admin, request, now)
    return SuperAdminLoginResponse(access_token=create_superadmin_access_token(admin.id))


@router.post("/login", response_model=SuperAdminLoginResponse)
async def login_superadmin(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db),
                           rate_limiter: RateLimiter = Depends(get_rate_limiter)):
    from app.services.audit import log_audit_event
    from app.core.config import get_settings

    email = form_data.username.strip().lower()
    ip = _ip(request)
    allowed_ip = await rate_limiter.is_allowed(f"sa-login:ip:{ip}", limit=SA_LOGIN_IP_LIMIT, window_seconds=SA_LOGIN_IP_WINDOW)
    allowed_email = await rate_limiter.is_allowed(f"sa-login:email:{email}", limit=SA_LOGIN_EMAIL_LIMIT,
                                                  window_seconds=SA_LOGIN_EMAIL_WINDOW)
    if not allowed_ip or not allowed_email:
        await log_audit_event(db, actor="ANONYMOUS", action="SUPERADMIN_LOGIN_RATE_LIMITED", ip_address=ip, details={"email": email})
        await db.commit()
        raise HTTPException(status_code=429, detail=SA_TOO_MANY)

    stmt = select(SuperAdminUser).where(SuperAdminUser.email == email, SuperAdminUser.active.is_(True))
    admin = (await db.execute(stmt)).scalar_one_or_none()
    if admin is None or not verify_password(form_data.password, admin.hashed_password):
        await log_audit_event(db, actor="ANONYMOUS", action="SUPERADMIN_LOGIN_FAILED", ip_address=ip, details={"email": email})
        await db.commit()
        raise HTTPException(status_code=401, detail="Email ou mot de passe incorrect")

    if not get_settings().superadmin_mfa:
        return await _open_session(db, admin, request)
    if not _send_code(admin):
        raise HTTPException(status_code=503, detail="Le code de connexion n'a pas pu être envoyé par email. Réessayez dans un instant.")
    await db.commit()
    return SuperAdminLoginResponse(mfa_required=True, mfa_pending_token=create_superadmin_mfa_pending_token(admin.id))


@router.post("/login/verify", response_model=SuperAdminLoginResponse)
async def verify_superadmin_code(payload: SuperAdminVerifyRequest, request: Request, db: AsyncSession = Depends(get_db),
                                 rate_limiter: RateLimiter = Depends(get_rate_limiter)):
    from app.services.audit import log_audit_event
    from app.services.otp_service import verify_otp

    admin_id = decode_superadmin_mfa_pending_token(payload.mfa_pending_token)
    if not await rate_limiter.is_allowed(f"sa-mfa:verify:{admin_id}", limit=5, window_seconds=300):
        raise HTTPException(status_code=429, detail="Trop d'essais : reconnectez-vous pour recevoir un nouveau code")
    admin = await db.get(SuperAdminUser, admin_id)
    if admin is None or not admin.active or admin.mfa_otp_code_hash is None or admin.mfa_otp_expires_at is None:
        raise HTTPException(status_code=401, detail="Aucune vérification en attente, reconnectez-vous")
    expires = admin.mfa_otp_expires_at if admin.mfa_otp_expires_at.tzinfo else admin.mfa_otp_expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=401, detail="Code expiré : reconnectez-vous pour en recevoir un autre")
    if not verify_otp((payload.code or "").strip(), admin.mfa_otp_code_hash):
        await log_audit_event(db, actor=f"superadmin:{admin.id}", action="SUPERADMIN_LOGIN_CODE_INVALID", ip_address=_ip(request))
        await db.commit()
        raise HTTPException(status_code=401, detail="Code incorrect")
    return await _open_session(db, admin, request)


@router.post("/login/resend", status_code=204)
async def resend_superadmin_code(payload: SuperAdminResendRequest, db: AsyncSession = Depends(get_db),
                                 rate_limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    admin_id = decode_superadmin_mfa_pending_token(payload.mfa_pending_token)
    if not await rate_limiter.is_allowed(f"sa-mfa:resend:{admin_id}", limit=3, window_seconds=300):
        raise HTTPException(status_code=429, detail="Trop de demandes, patientez quelques minutes")
    admin = await db.get(SuperAdminUser, admin_id)
    if admin is None or not admin.active:
        raise HTTPException(status_code=401, detail="Session invalide, reconnectez-vous")
    if not _send_code(admin):
        raise HTTPException(status_code=503, detail="Le code n'a pas pu être envoyé. Réessayez dans un instant.")
    await db.commit()


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


async def _total_commission_due(db: AsyncSession, tenant_id) -> float:
    stmt = select(func.coalesce(func.sum(OrderCommission.commission_amount), 0)).where(OrderCommission.tenant_id == tenant_id)
    return float((await db.execute(stmt)).scalar_one())


async def _trace(db, current: CurrentSuperAdmin, tenant: Tenant, action: str, before=None, after=None) -> None:
    """Lot 66 — chaque réglage changé depuis le Super Admin est tracé (historique de la fiche)."""
    from app.services.audit import log_audit_event

    details = {"from": before, "to": after} if before is not None or after is not None else None
    await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}", action=action, tenant_id=tenant.id,
                          details=details)


async def _build_summary(db: AsyncSession, tenant: Tenant) -> TenantSummaryForAdmin:
    return TenantSummaryForAdmin(
        id=tenant.id, name=tenant.name, email=tenant.email, plan=tenant.plan.value,
        is_demo=tenant.is_demo, active=tenant.active,
        product_count=await _product_count(db, tenant.id),
        conversation_count_this_month=await _conversation_count_this_month(db, tenant.id),
        commission_rate=float(tenant.commission_rate) if tenant.commission_rate is not None else None,
        business_type=normalize_business_type(tenant.business_type),
        created_at=tenant.created_at,
        paid_until=tenant.paid_until, bob_status=bob_pause.status(tenant).as_dict(),
        country=tenant.country, phone=tenant.phone,
        prereg_locked=tenant.prereg_at is not None and tenant.access_opened_at is None,
    )


async def _build_detail(db: AsyncSession, tenant: Tenant) -> TenantDetailForAdmin:
    summary = await _build_summary(db, tenant)
    customer_count = (await db.execute(select(func.count(Customer.id)).where(Customer.tenant_id == tenant.id))).scalar_one()
    order_count = (await db.execute(select(func.count(Order.id)).where(Order.tenant_id == tenant.id))).scalar_one()
    return TenantDetailForAdmin(
        **summary.model_dump(), currency=tenant.currency,
        customer_count=customer_count, order_count=order_count,
        total_commission_due=await _total_commission_due(db, tenant.id),
    )


@router.get("/tenants", response_model=list[TenantSummaryForAdmin])
async def list_tenants(
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    tenants = (await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))).scalars().all()
    return [await _build_summary(db, tenant) for tenant in tenants]


@router.get("/tenants/{tenant_id}", response_model=TenantDetailForAdmin)
async def get_tenant_detail(
    tenant_id: UUID,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    return await _build_detail(db, tenant)


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
    previous = tenant.plan.value
    try:
        tenant.plan = TenantPlan(payload.plan)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Plan invalide. Valeurs possibles : {[p.value for p in TenantPlan]}") from None
    await _trace(db, current, tenant, "PLAN_CHANGED_BY_ADMIN", previous, tenant.plan.value)
    await db.commit()
    return await _build_summary(db, tenant)


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
    if tenant.active != payload.active:
        await _trace(db, current, tenant, "TENANT_REACTIVATED_BY_ADMIN" if payload.active else "TENANT_SUSPENDED_BY_ADMIN")
    tenant.active = payload.active
    await db.commit()
    return await _build_summary(db, tenant)


@router.put("/tenants/{tenant_id}/paid-until", response_model=TenantSummaryForAdmin)
async def update_tenant_paid_until(
    tenant_id: UUID,
    payload: TenantPaidUntilUpdate,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """
    Lot 51 — dernier jour payé de l'abonnement (vide = pas d'échéance). Repousser la date relance Bob
    immédiatement ; les emails d'échéance repartent à zéro pour la nouvelle date.
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    previous = tenant.paid_until
    tenant.paid_until = payload.paid_until
    if previous != payload.paid_until:
        await _trace(db, current, tenant, "PAID_UNTIL_CHANGED_BY_ADMIN", previous.isoformat() if previous else None,
                     payload.paid_until.isoformat() if payload.paid_until else None)
    await db.commit()
    return await _build_summary(db, tenant)


@router.put("/tenants/{tenant_id}/commission-rate", response_model=TenantSummaryForAdmin)
async def update_commission_rate(
    tenant_id: UUID,
    payload: TenantCommissionRateUpdate,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """
    Section 13 — taux négocié B2B, jamais modifiable par le commerçant lui-même.
    S'applique seulement aux commandes créées APRÈS ce changement (taux figé par commande).
    """
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    if payload.commission_rate is not None and not (0 <= payload.commission_rate <= 100):
        raise HTTPException(status_code=400, detail="Le taux doit être compris entre 0 et 100")
    previous = float(tenant.commission_rate) if tenant.commission_rate is not None else None
    tenant.commission_rate = payload.commission_rate
    if previous != payload.commission_rate:
        await _trace(db, current, tenant, "COMMISSION_CHANGED_BY_ADMIN", previous, payload.commission_rate)
    await db.commit()
    return await _build_summary(db, tenant)


@router.put("/tenants/{tenant_id}/business-type", response_model=TenantSummaryForAdmin)
async def update_tenant_business_type(
    tenant_id: UUID,
    payload: TenantBusinessTypeUpdate,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """
    Lot 32 — le type d'activité (boutique en ligne / concession) ne se change plus par le client
    après l'inscription : uniquement ici, à sa demande, et toujours tracé.
    """
    from datetime import datetime, timezone

    from app.services.audit import log_audit_event
    from app.services.business_type import BUSINESS_TYPES

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant introuvable")
    if payload.business_type not in BUSINESS_TYPES:
        raise HTTPException(status_code=400, detail=f"Type d'activité invalide. Valeurs possibles : {list(BUSINESS_TYPES)}")
    previous = normalize_business_type(tenant.business_type)
    tenant.business_type = payload.business_type
    tenant.business_type_chosen_at = tenant.business_type_chosen_at or datetime.now(timezone.utc)
    await log_audit_event(
        db, actor=f"superadmin:{current.superadmin_user_id}", action="BUSINESS_TYPE_CHANGED_BY_ADMIN", tenant_id=tenant.id,
        details={"from": previous, "to": payload.business_type},
    )
    if payload.business_type == "INSURANCE_BROKER":
        from app.services.insurance import seed_products

        await seed_products(db, tenant)  # lot 53 : seulement si la boutique n'a encore aucun produit
    await db.commit()
    return await _build_summary(db, tenant)


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


@router.get("/ai-costs")
async def ai_costs(
    month: str | None = None,
    current: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
):
    """
    Lot 50 — coût IA estimé par boutique pour un mois (« 2026-10 », mois en cours par défaut) :
    appels, tokens envoyés / servis par le cache / reçus, coût, coût par conversation, taux de cache.
    """
    import re

    from app.core.config import get_settings
    from app.services.llm_usage_service import costs_by_tenant, month_bounds

    if month is not None and not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="Mois attendu au format AAAA-MM.")
    start, end, label = month_bounds(month)
    rows = await costs_by_tenant(db, start, end)
    totals = {key: sum(r[key] for r in rows) for key in ("calls", "conversations", "prompt_tokens", "cached_tokens",
                                                         "completion_tokens")}
    totals["cost_usd"] = round(sum(r["cost_usd"] for r in rows), 4)
    totals["cache_rate_pct"] = round(totals["cached_tokens"] / totals["prompt_tokens"] * 100, 1) if totals["prompt_tokens"] else None
    return {"month": label, "threshold_usd": get_settings().llm_monthly_alert_usd, "rows": rows, "totals": totals}
