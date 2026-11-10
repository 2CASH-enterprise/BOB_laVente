"""
Lot 119 — « Équipe AgenC'AI » dans le Super Admin : qui a accès, avec quels rôles (plusieurs possibles), invitations
par e-mail (lien valable 48 h, la personne choisit son mot de passe ; puis code par e-mail à chaque connexion, lot 70),
désactivation, et journal de qui a fait quoi. Réservé au Propriétaire ; /me sert à chacun pour son menu.

Garde-fous : jamais moins d'un Propriétaire actif ; on ne se désactive pas soi-même ; seule l'empreinte du lien est
gardée ; un lien sert une seule fois.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import CurrentSuperAdmin, hash_password, need
from app.models.audit_log import AuditLog
from app.models.superadmin_user import SuperAdminUser
from app.services import sa_roles

router = APIRouter(prefix="/api/v1/superadmin", tags=["superadmin"])

INVITE_VALID = timedelta(hours=48)
MIN_PASSWORD = 10
JOURNAL_LIMIT = 60
TEAM_LABELS = {
    "SA_MEMBER_INVITED": "a invité",
    "SA_MEMBER_REINVITED": "a renvoyé l'invitation de",
    "SA_INVITE_ACCEPTED": "a rejoint l'équipe",
    "SA_MEMBER_ROLES_CHANGED": "a changé les rôles de",
    "SA_MEMBER_DISABLED": "a désactivé l'accès de",
    "SA_MEMBER_ENABLED": "a réactivé l'accès de",
    "PROSPECT_VIEWED": "a ouvert la fiche du prospect",
    "PROSPECTS_EXPORTED": "a exporté la liste des prospects",
    "SUPERADMIN_LOGIN_SUCCESS": "s'est connecté",
}


class InviteIn(BaseModel):
    email: str
    full_name: str
    roles: list[str]


class MemberUpdate(BaseModel):
    roles: list[str] | None = None
    active: bool | None = None


class AcceptIn(BaseModel):
    token: str
    password: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _roles(values) -> list[str]:
    roles = sa_roles.clean(values)
    if not roles or len(roles) != len(set(values or [])):
        raise HTTPException(status_code=422, detail="Choisissez au moins un rôle parmi ceux proposés.")
    return roles


def _status(member: SuperAdminUser) -> str:
    if member.invite_token_hash is not None:
        return "INVITED"
    return "ACTIVE" if member.active else "DISABLED"


def _view(member: SuperAdminUser) -> dict:
    return {"id": str(member.id), "full_name": member.full_name, "email": member.email, "roles": sa_roles.clean(member.roles),
            "status": _status(member), "last_login_at": _utc(member.last_login_at).isoformat() if member.last_login_at else None,
            "invite_expires_at": _utc(member.invite_expires_at).isoformat() if member.invite_expires_at else None}


async def _owners_left(db, without: UUID | None = None) -> int:
    members = (await db.execute(select(SuperAdminUser).where(SuperAdminUser.active.is_(True),
                                                             SuperAdminUser.invite_token_hash.is_(None)))).scalars().all()
    return sum(1 for m in members if m.id != without and sa_roles.OWNER in (m.roles or []))


async def _log(db, current: CurrentSuperAdmin, action: str, member: SuperAdminUser, details: dict | None = None) -> None:
    from app.services.audit import log_audit_event

    await log_audit_event(db, actor=f"superadmin:{current.superadmin_user_id}", action=action,
                          details={"member": member.full_name, **(details or {})})


def _send_invite(member: SuperAdminUser, inviter: str, now: datetime) -> tuple[str, bool]:
    from app.core.config import get_settings
    from app.services.email_service import send_email

    token = secrets.token_urlsafe(32)
    member.invite_token_hash = _digest(token)
    member.invite_expires_at = now + INVITE_VALID
    url = f"{get_settings().public_base_url.rstrip('/')}/superadmin/?invitation={token}"
    labels = ", ".join(sa_roles.ROLES[r][0] for r in sa_roles.clean(member.roles))
    sent = bool(send_email(
        to=member.email, subject="Invitation au Super Admin de Bob",
        body=(f"Bonjour {member.full_name},\n\n{inviter} vous invite à rejoindre l'équipe AgenC'AI dans le Super Admin de Bob "
              f"(rôle : {labels}).\n\nChoisissez votre mot de passe ici (lien valable 48 heures, une seule fois) :\n{url}\n\n"
              "À chaque connexion, un code vous sera ensuite envoyé par e-mail. Si vous n'attendiez pas cette invitation, "
              "ignorez ce message."),
    ))
    return url, sent


async def _member(db, member_id: UUID) -> SuperAdminUser:
    member = await db.get(SuperAdminUser, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Membre introuvable")
    return member


@router.get("/me")
async def me(current: CurrentSuperAdmin = Depends(need("overview")), db: AsyncSession = Depends(get_db)) -> dict:
    member = await _member(db, current.superadmin_user_id)
    return {**_view(member), "perms": sa_roles.perms_for(member.roles),
            "role_labels": [sa_roles.ROLES[r][0] for r in sa_roles.clean(member.roles)]}


@router.get("/team")
async def team(current: CurrentSuperAdmin = Depends(need("team.manage")), db: AsyncSession = Depends(get_db)) -> dict:
    members = (await db.execute(select(SuperAdminUser).order_by(SuperAdminUser.created_at))).scalars().all()
    return {"members": [_view(m) for m in members], "roles": sa_roles.catalogue(), "me": str(current.superadmin_user_id)}


@router.post("/team/invite", status_code=201)
async def invite(payload: InviteIn, current: CurrentSuperAdmin = Depends(need("team.manage")),
                 db: AsyncSession = Depends(get_db), limiter: RateLimiter = Depends(get_rate_limiter)) -> dict:
    email = (payload.email or "").strip().lower()
    name = " ".join((payload.full_name or "").split())
    if "@" not in email or len(email) > 255 or " " in email or not 1 < len(name) <= 120:
        raise HTTPException(status_code=422, detail="Nom et e-mail attendus.")
    roles = _roles(payload.roles)
    if not await limiter.is_allowed(f"sa-invite:{current.superadmin_user_id}", 20, 86400):
        raise HTTPException(status_code=429, detail="Trop d'invitations aujourd'hui.")
    if (await db.execute(select(SuperAdminUser.id).where(SuperAdminUser.email == email))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Cette personne fait déjà partie de l'équipe.")
    inviter = await _member(db, current.superadmin_user_id)
    member = SuperAdminUser(email=email, full_name=name, roles=roles, invited_by=inviter.id,
                            hashed_password=hash_password(secrets.token_urlsafe(32)))  # inutilisable avant l'invitation
    db.add(member)
    await db.flush()
    _, sent = _send_invite(member, inviter.full_name, datetime.now(timezone.utc))
    await _log(db, current, "SA_MEMBER_INVITED", member, {"roles": roles})
    await db.commit()
    return {**_view(member), "email_sent": sent}


@router.post("/team/{member_id}/reinvite")
async def reinvite(member_id: UUID, current: CurrentSuperAdmin = Depends(need("team.manage")),
                   db: AsyncSession = Depends(get_db)) -> dict:
    member = await _member(db, member_id)
    if member.invite_token_hash is None:
        raise HTTPException(status_code=409, detail="Cette personne a déjà rejoint l'équipe.")
    inviter = await _member(db, current.superadmin_user_id)
    _, sent = _send_invite(member, inviter.full_name, datetime.now(timezone.utc))
    member.active = True
    await _log(db, current, "SA_MEMBER_REINVITED", member)
    await db.commit()
    return {**_view(member), "email_sent": sent}


@router.put("/team/{member_id}")
async def update_member(member_id: UUID, payload: MemberUpdate, current: CurrentSuperAdmin = Depends(need("team.manage")),
                        db: AsyncSession = Depends(get_db)) -> dict:
    member = await _member(db, member_id)
    if payload.roles is not None:
        roles = _roles(payload.roles)
        if sa_roles.OWNER not in roles and sa_roles.OWNER in (member.roles or []) and await _owners_left(db, member.id) == 0:
            raise HTTPException(status_code=409, detail="Il faut toujours au moins un Propriétaire actif.")
        if roles != sa_roles.clean(member.roles):
            await _log(db, current, "SA_MEMBER_ROLES_CHANGED", member, {"from": sa_roles.clean(member.roles), "to": roles})
            member.roles = roles
    if payload.active is not None and payload.active != member.active:
        if member.id == current.superadmin_user_id:
            raise HTTPException(status_code=409, detail="Vous ne pouvez pas désactiver votre propre accès.")
        if not payload.active and sa_roles.OWNER in (member.roles or []) and await _owners_left(db, member.id) == 0:
            raise HTTPException(status_code=409, detail="Il faut toujours au moins un Propriétaire actif.")
        member.active = payload.active
        if not payload.active:
            member.invite_token_hash = member.invite_expires_at = None  # une invitation en cours ne sert plus
            member.mfa_otp_code_hash = member.mfa_otp_expires_at = None
        await _log(db, current, "SA_MEMBER_ENABLED" if payload.active else "SA_MEMBER_DISABLED", member)
    await db.commit()
    return _view(member)


@router.post("/team/accept", status_code=204)
async def accept(payload: AcceptIn, request: Request, db: AsyncSession = Depends(get_db),
                 limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    """Public : le lien de l'invitation et le mot de passe choisi. La connexion se fait ensuite normalement (code e-mail)."""
    from app.services.audit import log_audit_event

    ip = request.client.host if request.client else "unknown"
    if not await limiter.is_allowed(f"sa-accept:{ip}", 10, 600):
        raise HTTPException(status_code=429, detail="Trop de tentatives, réessayez plus tard.")
    if len(payload.password or "") < MIN_PASSWORD:
        raise HTTPException(status_code=422, detail=f"Mot de passe de {MIN_PASSWORD} caractères au moins.")
    token = payload.token if isinstance(payload.token, str) and len(payload.token) <= 100 else ""
    member = (await db.execute(select(SuperAdminUser).where(
        SuperAdminUser.invite_token_hash == _digest(token)))).scalar_one_or_none() if token else None
    if member is None or not member.active or member.invite_expires_at is None \
            or _utc(member.invite_expires_at) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Cette invitation a expiré ou a déjà servi. Demandez-en une nouvelle.")
    member.hashed_password = hash_password(payload.password)
    member.invite_token_hash = member.invite_expires_at = None
    await log_audit_event(db, actor=f"superadmin:{member.id}", action="SA_INVITE_ACCEPTED", ip_address=ip,
                          details={"member": member.full_name})
    await db.commit()


@router.get("/team/journal")
async def journal(current: CurrentSuperAdmin = Depends(need("team.manage")), db: AsyncSession = Depends(get_db)) -> dict:
    from app.api.superadmin.overview import ACTION_LABELS, describe
    from app.models.tenant import Tenant

    members = {f"superadmin:{m.id}": m.full_name or m.email for m in (await db.execute(select(SuperAdminUser))).scalars()}
    logs = (await db.execute(select(AuditLog).where(AuditLog.actor.like("superadmin:%"),
                                                    AuditLog.action.in_(list(ACTION_LABELS) + list(TEAM_LABELS)))
                             .order_by(AuditLog.created_at.desc()).limit(JOURNAL_LIMIT))).scalars().all()
    shops = {t.id: t.name for t in (await db.execute(select(Tenant).where(
        Tenant.id.in_({log.tenant_id for log in logs if log.tenant_id})))).scalars()} if logs else {}
    items = []
    for log in logs:
        if log.action in TEAM_LABELS:
            target = (log.details or {}).get("member") or (log.details or {}).get("prospect") or ""
            what = f"{TEAM_LABELS[log.action]} {target}".strip() if log.action not in ("SA_INVITE_ACCEPTED", "SUPERADMIN_LOGIN_SUCCESS", "PROSPECTS_EXPORTED") else TEAM_LABELS[log.action]
        else:
            what = describe(log)
        items.append({"at": _utc(log.created_at).isoformat() if log.created_at else None, "who": members.get(log.actor, "—"),
                      "what": what, "shop": shops.get(log.tenant_id)})
    return {"items": items}
