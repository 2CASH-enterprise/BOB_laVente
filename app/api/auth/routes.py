from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import (
    CurrentUser,
    create_access_token,
    create_mfa_pending_token,
    decode_mfa_pending_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginResponse,
    MfaToggleRequest,
    RegisterTenantRequest,
    ResendMfaRequest,
    ResetPasswordRequest,
    SignupCodeRequest,
    TenantCreatedResponse,
    TokenResponse,
    VerifyMfaRequest,
)
from app.services import sessions
from app.services.audit import log_audit_event
from app.services.email_service import send_email
from app.services.otp_service import generate_otp, hash_otp, verify_otp
from app.services.password_reset_service import (
    apply_new_password,
    build_password_changed_email,
    build_reset_email,
    build_reset_link,
    find_user_by_valid_token,
    issue_reset_token,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

LOGIN_RATE_LIMIT = 5  # tentatives
LOGIN_RATE_WINDOW_SECONDS = 60


@router.get("/signup-options")
async def signup_options() -> dict:
    """Lot 41 — pays (avec devise habituelle), devises et activités proposés à l'inscription."""
    from app.services.business_type import BUSINESS_TYPES
    from app.services.countries import signup_options as options

    return {**options(), "business_types": [{"code": c, **info} for c, info in BUSINESS_TYPES.items()]}


@router.post("/signup/send-code", status_code=status.HTTP_202_ACCEPTED)
async def send_signup_code(
    payload: SignupCodeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> dict:
    """Lot 41 — code à 6 chiffres envoyé à l'adresse, avant toute création de compte."""
    from app.services import email_verification

    client_ip = request.client.host if request.client else "unknown"
    email = email_verification.normalize(payload.email)
    if not await rate_limiter.is_allowed(f"signup-code:ip:{client_ip}", limit=10, window_seconds=600) or \
            not await rate_limiter.is_allowed(f"signup-code:email:{email}", limit=3, window_seconds=600):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Trop de codes demandés. Réessayez dans quelques minutes.")
    if await UserRepository(db).get_by_email(str(payload.email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cet email est déjà utilisé")
    code = await email_verification.new_code(db, email)
    await db.commit()
    subject, body = email_verification.code_email(code)
    background_tasks.add_task(send_email, to=str(payload.email), subject=subject, body=body)
    return {"sent": True, "expires_in_minutes": 10}


@router.post("/register-tenant", response_model=TenantCreatedResponse, status_code=status.HTTP_201_CREATED)
async def register_tenant(
    payload: RegisterTenantRequest, request: Request, db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> TenantCreatedResponse:
    """
    Section 6 — Ajouter une entreprise.
    Crée le tenant et son premier utilisateur, avec le rôle OWNER.
    Lot 41 : email vérifié par code, pays et devise de la liste, activité choisie dès l'inscription.
    """
    from app.services import email_verification
    from app.services.business_type import BUSINESS_TYPES
    from app.services.countries import COUNTRY_CODES, CURRENCIES

    client_ip = request.client.host if request.client else "unknown"
    if not await rate_limiter.is_allowed(f"register:ip:{client_ip}", limit=10, window_seconds=600):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de tentatives, réessayez plus tard")
    country, currency = payload.country.upper(), payload.currency.upper()
    if country not in COUNTRY_CODES:
        raise HTTPException(status_code=422, detail="Pays non proposé : choisissez-le dans la liste")
    if currency not in CURRENCIES:
        raise HTTPException(status_code=422, detail="Devise non proposée : choisissez-la dans la liste")
    if payload.business_type is not None and payload.business_type not in BUSINESS_TYPES:
        raise HTTPException(status_code=422, detail="Type d'activité inconnu")

    repo = UserRepository(db)
    existing = await repo.get_by_email(payload.owner_email)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cet email est déjà utilisé")
    if not await email_verification.check_code(db, payload.owner_email, payload.verification_code):
        await db.commit()  # l'essai raté est compté
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=email_verification.INVALID_CODE)

    tenant = Tenant(
        name=payload.company_name,
        country=country,
        currency=currency,
        phone=payload.phone,
        email=payload.owner_email,
    )
    if payload.business_type is not None:  # choisi à l'inscription : définitif (lot 32)
        tenant.business_type = payload.business_type
        tenant.business_type_chosen_at = datetime.now(timezone.utc)
    db.add(tenant)
    await db.flush()  # obtient tenant.id sans committer
    if tenant.business_type == "INSURANCE_BROKER":
        from app.services.insurance import seed_products

        await seed_products(db, tenant)  # lot 53 : produits d'assurance par défaut, sans prix

    owner = User(
        tenant_id=tenant.id,
        email=payload.owner_email,
        hashed_password=hash_password(payload.owner_password),
        full_name=payload.owner_full_name,
        role=Role.OWNER,
    )
    db.add(owner)
    await db.flush()  # nécessaire pour obtenir owner.id avant de journaliser

    await log_audit_event(
        db,
        actor=str(owner.id),
        action="TENANT_REGISTERED",
        tenant_id=tenant.id,
        details={"company_name": payload.company_name, "owner_email": payload.owner_email,
                 "business_type": payload.business_type, "email_verified": True},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()

    return TenantCreatedResponse(tenant_id=tenant.id, owner_user_id=owner.id)


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    remember: bool = Form(False),  # lot 37 : « Rester connecté sur cet appareil »
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> LoginResponse:
    client_ip = request.client.host if request.client else "unknown"

    # Section 32 — protection brute-force : limite par IP ET par email visé, pour bloquer
    # à la fois un attaquant qui varie les emails et un attaquant qui varie les IP.
    allowed_ip = await rate_limiter.is_allowed(f"login:ip:{client_ip}", limit=20, window_seconds=60)
    allowed_email = await rate_limiter.is_allowed(f"login:email:{form_data.username}", limit=LOGIN_RATE_LIMIT, window_seconds=LOGIN_RATE_WINDOW_SECONDS)
    if not allowed_ip or not allowed_email:
        await log_audit_event(db, actor="ANONYMOUS", action="LOGIN_RATE_LIMITED", ip_address=client_ip, details={"email": form_data.username})
        await db.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de tentatives, réessayez plus tard")

    repo = UserRepository(db)
    user = await repo.get_by_email(form_data.username)  # OAuth2 form utilise "username" pour l'email

    if user is None or not verify_password(form_data.password, user.hashed_password):
        await log_audit_event(
            db, actor="ANONYMOUS", action="LOGIN_FAILED", ip_address=client_ip, details={"email": form_data.username}
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou mot de passe incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.mfa_enabled:
        await _send_mfa_code(db, user)
        await log_audit_event(db, actor=str(user.id), action="LOGIN_MFA_CODE_SENT", tenant_id=user.tenant_id, ip_address=client_ip)
        await db.commit()
        return LoginResponse(mfa_required=True, mfa_pending_token=create_mfa_pending_token(user.id))

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    await log_audit_event(db, actor=str(user.id), action="LOGIN_SUCCESS", tenant_id=user.tenant_id, ip_address=client_ip,
                          details={"remember": remember})
    if remember:
        sessions.set_cookie(response, await sessions.create_session(db, user, request.headers.get("user-agent")))
    await db.commit()

    return LoginResponse(access_token=token)


async def _send_mfa_code(db: AsyncSession, user: User) -> None:
    code = generate_otp()
    user.mfa_otp_code_hash = hash_otp(code)
    user.mfa_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    send_email(
        to=user.email,
        subject="Votre code de connexion Bob",
        # Lot 40 : le code seul sur sa ligne s'affiche en grand dans l'email.
        body=(f"Bonjour,\n\nVoici votre code de connexion à Bob :\n\n{code}\n\nIl expire dans 10 minutes. "
              "Si vous n'êtes pas à l'origine de cette tentative de connexion, ignorez cet email : votre compte reste protégé."),
    )


@router.post("/verify-mfa", response_model=TokenResponse)
async def verify_mfa(payload: VerifyMfaRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db), rate_limiter: RateLimiter = Depends(get_rate_limiter)) -> TokenResponse:
    user_id = decode_mfa_pending_token(payload.mfa_pending_token)
    client_ip = request.client.host if request.client else "unknown"

    # Un code à 6 chiffres a peu de combinaisons : limiter strictement les tentatives.
    allowed = await rate_limiter.is_allowed(f"mfa:verify:{user_id}", limit=5, window_seconds=300)
    if not allowed:
        await db.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de tentatives, redemandez un code")

    user = await db.get(User, user_id)
    if user is None or user.mfa_otp_code_hash is None or user.mfa_otp_expires_at is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Aucune vérification en attente")

    expires_at = user.mfa_otp_expires_at
    if expires_at.tzinfo is None:  # SQLite (tests) renvoie parfois une valeur naïve
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Code expiré, redemandez-en un")

    if not verify_otp(payload.code, user.mfa_otp_code_hash):
        await log_audit_event(db, actor=str(user.id), action="LOGIN_MFA_CODE_INVALID", tenant_id=user.tenant_id, ip_address=client_ip)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Code incorrect")

    user.mfa_otp_code_hash = None
    user.mfa_otp_expires_at = None

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    await log_audit_event(db, actor=str(user.id), action="LOGIN_SUCCESS", tenant_id=user.tenant_id, ip_address=client_ip,
                          details={"mfa": True, "remember": payload.remember})
    if payload.remember:  # lot 37 : la session longue ne s'ouvre qu'APRÈS le code à 6 chiffres
        sessions.set_cookie(response, await sessions.create_session(db, user, request.headers.get("user-agent")))
    await db.commit()

    return TokenResponse(access_token=token)


@router.post("/mfa/resend", status_code=status.HTTP_204_NO_CONTENT)
async def resend_mfa(payload: ResendMfaRequest, db: AsyncSession = Depends(get_db), rate_limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    user_id = decode_mfa_pending_token(payload.mfa_pending_token)
    allowed = await rate_limiter.is_allowed(f"mfa:resend:{user_id}", limit=3, window_seconds=300)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de demandes, patientez")

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalide")
    await _send_mfa_code(db, user)
    await db.commit()


@router.put("/mfa/toggle", response_model=TokenResponse)
async def toggle_mfa(
    payload: MfaToggleRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Réutilise le même token (aucune réauthentification exigée pour ce MVP — voir section 2FA)."""
    user = await db.get(User, current_user.user_id)
    user.mfa_enabled = payload.enabled
    if not payload.enabled:
        user.mfa_otp_code_hash = None
        user.mfa_otp_expires_at = None
    await db.commit()
    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    return TokenResponse(access_token=token)


@router.get("/me")
async def get_me(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, current_user.user_id)
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name, "role": user.role.value, "mfa_enabled": user.mfa_enabled}


# ---------------------------------------------------------------------------
# Mot de passe oublié
# ---------------------------------------------------------------------------

FORGOT_RATE_LIMIT_PER_EMAIL = 3
FORGOT_RATE_LIMIT_PER_IP = 10
FORGOT_RATE_WINDOW_SECONDS = 900  # 15 min
RESET_RATE_LIMIT_PER_IP = 10
RESET_RATE_WINDOW_SECONDS = 300


@router.post("/forgot-password", response_model=ForgotPasswordResponse, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> ForgotPasswordResponse:
    """
    Réponse STRICTEMENT identique que le compte existe ou non. L'email part en tâche de
    fond, après la réponse : sinon le temps de connexion SMTP trahirait l'existence du compte.
    """
    client_ip = request.client.host if request.client else "unknown"
    email = str(payload.email)

    allowed_ip = await rate_limiter.is_allowed(f"forgot:ip:{client_ip}", limit=FORGOT_RATE_LIMIT_PER_IP, window_seconds=FORGOT_RATE_WINDOW_SECONDS)
    allowed_email = await rate_limiter.is_allowed(f"forgot:email:{email.lower()}", limit=FORGOT_RATE_LIMIT_PER_EMAIL, window_seconds=FORGOT_RATE_WINDOW_SECONDS)
    if not allowed_ip or not allowed_email:
        # 429 appliqué à n'importe quel email, existant ou non : ne révèle rien.
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de demandes, réessayez plus tard")

    user = await UserRepository(db).get_by_email(email)
    if user is None:
        await log_audit_event(db, actor="ANONYMOUS", action="PASSWORD_RESET_REQUESTED_UNKNOWN", ip_address=client_ip, details={"email": email})
        await db.commit()
        return ForgotPasswordResponse()

    token = issue_reset_token(user)
    await log_audit_event(db, actor=str(user.id), action="PASSWORD_RESET_REQUESTED", tenant_id=user.tenant_id, ip_address=client_ip)
    await db.commit()  # le token doit être en base AVANT que l'email ne parte

    subject, body = build_reset_email(user.full_name, build_reset_link(token))
    background_tasks.add_task(send_email, to=user.email, subject=subject, body=body)
    return ForgotPasswordResponse()


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """
    Ne connecte PAS l'utilisateur : il doit ensuite se connecter normalement,
    ce qui garantit que la 2FA reste exigée si elle est activée.
    """
    client_ip = request.client.host if request.client else "unknown"
    allowed = await rate_limiter.is_allowed(f"reset:ip:{client_ip}", limit=RESET_RATE_LIMIT_PER_IP, window_seconds=RESET_RATE_WINDOW_SECONDS)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de tentatives, réessayez plus tard")

    user = await find_user_by_valid_token(db, payload.token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Lien invalide ou expiré, refaites une demande")

    apply_new_password(user, payload.new_password)
    await sessions.revoke_all(db, user.id)  # lot 37 : nouveau mot de passe → tous les appareils déconnectés
    await log_audit_event(db, actor=str(user.id), action="PASSWORD_RESET_COMPLETED", tenant_id=user.tenant_id, ip_address=client_ip)
    await db.commit()

    subject, body = build_password_changed_email(user.full_name)
    background_tasks.add_task(send_email, to=user.email, subject=subject, body=body)


# ---------------------------------------------------------------------------
# Lot 37 — « Rester connecté » : session longue par cookie protégé
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=TokenResponse)
async def refresh_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> TokenResponse:
    """Nouvel accès à partir de la session de cet appareil ; la clé est remplacée à chaque fois."""
    client_ip = request.client.host if request.client else "unknown"
    if not await rate_limiter.is_allowed(f"refresh:ip:{client_ip}", limit=60, window_seconds=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Trop de tentatives, réessayez plus tard")
    session, new_raw = await sessions.use_session(db, request.cookies.get(sessions.COOKIE_NAME))
    user = await db.get(User, session.user_id) if session is not None else None
    tenant = await db.get(Tenant, user.tenant_id) if user is not None else None
    if session is None or user is None or not user.active or tenant is None or not tenant.active:
        if session is not None:
            session.revoked_at = datetime.now(timezone.utc)  # compte suspendu : la session ne sert plus
        await db.commit()
        # Réponse construite ici (pas d'exception) : sinon l'effacement du cookie serait perdu.
        failed = JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Session expirée, reconnectez-vous."})
        sessions.clear_cookie(failed)
        return failed
    if new_raw is not None:
        sessions.set_cookie(response, new_raw)
    await db.commit()
    return TokenResponse(access_token=create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    """Ferme la session longue de CET appareil (sans effet s'il n'y en a pas)."""
    await sessions.revoke(db, request.cookies.get(sessions.COOKIE_NAME))
    await db.commit()
    sessions.clear_cookie(response)


@router.post("/sessions/revoke-all", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_all_sessions(
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """« Déconnecter tous mes appareils » (téléphone perdu, ordinateur partagé…)."""
    await sessions.revoke_all(db, current_user.user_id)
    client_ip = request.client.host if request.client else "unknown"
    await log_audit_event(db, actor=str(current_user.user_id), action="SESSIONS_REVOKED_ALL",
                          tenant_id=current_user.tenant_id, ip_address=client_ip)
    await db.commit()
    sessions.clear_cookie(response)
