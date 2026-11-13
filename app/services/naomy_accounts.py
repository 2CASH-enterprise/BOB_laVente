"""
Lot 118 — « Mon équipe » par Naomy (contrat 1.4) : un commerçant qui s'inscrit chez Naomy et choisit « Naomy + Bob »
reçoit un compte Bob créé par Naomy, déjà relié (portée complète, assistant allumé), sans formulaire ni mot de passe.

Garde-fous, pour ne casser aucune règle existante :
- lot 89 (Naomy à partir d'Indépendant) : formule Indépendant avec un essai équipe de 7 jours (`paid_until`, lot 51 :
  rappel de grâce puis pause, comme un abonnement non renouvelé). Le rappel « 7 jours avant » est marqué déjà parti :
  le rapport mensuel ne part pas le jour de l'inscription ;
- lot 41 (e-mail vérifié par code) : le numéro est prouvé par Naomy (c'est lui qui lui écrit) ; l'e-mail est obligatoire
  et vérifié plus tard ; aucun mot de passe : un lien de connexion à usage unique (24 h) ouvre « Relier WhatsApp » ;
- lot 92 (une boutique existe déjà) : jamais de doublon. Même numéro (WhatsApp relié ou numéro de la boutique) ou même
  e-mail : 409 `boutique_existante` et la demande « Accepter / Refuser » habituelle est envoyée à la boutique ;
- lot 65 (pré-inscription) : même règle que chez Bob. Pré-inscrit : l'essai démarre à l'ouverture de l'accès ;
- lot 61 (prospects) : le compte est rattaché au prospect de même e-mail ;
- lot 70 : 3 créations par numéro et par 24 h, 200 par jour au total.
"""
import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.services.naomy_bridge import NaomyError

logger = logging.getLogger(__name__)

ORIGIN = "NAOMY"
TRIAL_DAYS = 7
PER_NUMBER = 3
PER_DAY = 200
FIELDS = {"compte_naomy", "numero_whatsapp", "nom_boutique", "secteur", "pays", "ville", "email", "nom_contact",
          "url_evenements"}
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value if 0 < len(value) <= limit else None


def validate(payload: dict) -> dict:
    from app.services.business_type import BUSINESS_TYPES

    bad = NaomyError(400, "requete_invalide",
                     "Champs attendus : compte_naomy, numero_whatsapp (+…), nom_boutique, secteur, pays, email, "
                     "nom_contact, url_evenements (ville facultative).")
    if not set(payload) <= FIELDS:
        raise bad
    number = payload.get("numero_whatsapp")
    email = payload.get("email")
    data = {"account": _text(payload.get("compte_naomy"), 64), "name": _text(payload.get("nom_boutique"), 120),
            "contact": _text(payload.get("nom_contact"), 120), "sector": payload.get("secteur"),
            "country": payload.get("pays"), "url": payload.get("url_evenements"),
            "city": _text(payload.get("ville"), 100) if payload.get("ville") is not None else None}
    if not (isinstance(number, str) and re.fullmatch(r"\+[1-9][0-9]{7,14}", number)
            and isinstance(email, str) and len(email) <= 255 and EMAIL.match(email.strip())
            and data["account"] and data["name"] and data["contact"] and data["sector"] in BUSINESS_TYPES
            and isinstance(data["country"], str) and re.fullmatch(r"[A-Z]{2}", data["country"])
            and isinstance(data["url"], str)
            and (payload.get("ville") is None or data["city"])):
        raise bad
    data["number"], data["digits"], data["email"] = number, number[1:], email.strip().lower()
    return data


async def existing_tenant(db, digits: str, email: str):
    """La boutique qui a déjà ce numéro (WhatsApp relié ou numéro de la boutique) ou cet e-mail, sinon None."""
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.models.whatsapp_account import WhatsAppAccount

    rows = (await db.execute(select(WhatsAppAccount.tenant_id, WhatsAppAccount.display_phone_number))).all()
    tenant_id = next((t for t, shown in rows if re.sub(r"\D", "", shown or "") == digits), None)
    if tenant_id is None:
        phones = (await db.execute(select(Tenant.id, Tenant.phone).where(Tenant.phone.is_not(None)))).all()
        tenant_id = next((t for t, phone in phones if _same_number(phone, digits)), None)
    if tenant_id is None:
        from sqlalchemy import func

        tenant_id = (await db.execute(select(User.tenant_id).where(func.lower(User.email) == email).limit(1))).scalar_one_or_none()
        if tenant_id is None:
            tenant_id = (await db.execute(select(Tenant.id).where(func.lower(Tenant.email) == email).limit(1))).scalar_one_or_none()
    return await db.get(Tenant, tenant_id) if tenant_id is not None else None


def _same_number(phone: str | None, digits: str) -> bool:
    """« +225 07… », « 225 07… » ou « 07… » (numéro local, même fin sur 8 chiffres au moins)."""
    mine = re.sub(r"\D", "", phone or "")
    if len(mine) < 8:
        return False
    return mine == digits or (len(mine) <= 10 and digits.endswith(mine))


async def _ask_existing(db, tenant, data, now, mailer=None) -> None:
    """La demande « Naomy demande à relier votre boutique » (lot 92), au plus une par 24 h ; jamais si déjà reliée."""
    from app.models.naomy import NaomyPairingRequest
    from app.services import naomy_bridge as nb

    if tenant.is_demo or not tenant.active or await nb.active_connection(db, tenant.id) is not None:
        return
    recent = (await db.execute(select(NaomyPairingRequest.id).where(
        NaomyPairingRequest.tenant_id == tenant.id, NaomyPairingRequest.status == "PENDING",
        NaomyPairingRequest.created_at >= now - timedelta(hours=24)).limit(1))).scalar_one_or_none()
    if recent is not None:
        return
    db.add(NaomyPairingRequest(tenant_id=tenant.id, naomy_account=data["account"], shop_name=data["name"],
                               status="PENDING", created_at=now))
    await db.commit()
    if (tenant.email or "").strip():
        if mailer is None:
            from app.services.email_service import send_email as mailer
        subject, body = nb.request_email(tenant, data["name"])
        try:
            await asyncio.to_thread(mailer, to=tenant.email, subject=subject, body=body)
        except Exception:  # noqa: BLE001 — la carte du tableau de bord reste
            logger.warning("E-mail de demande d'appairage Naomy non envoyé (boutique %s)", tenant.id)


def start_trial(tenant, now: datetime | None = None) -> None:
    """Essai équipe : formule Indépendant jusqu'au 7e jour (heure du pays). Le rappel « 7 jours avant » ne part pas."""
    from app.models.tenant import TenantPlan
    from app.services.bob_pause import WARNING, local_today

    tenant.plan = TenantPlan.INDEPENDANT
    tenant.paid_until = local_today(tenant, now) + timedelta(days=TRIAL_DAYS - 1)
    tenant.billing_notice_stage, tenant.billing_notice_for = WARNING, tenant.paid_until


async def create_account(db, payload: dict, limiter=None, now: datetime | None = None, mailer=None) -> dict:
    from app.core.config import get_settings
    from app.core.security import hash_password
    from app.models.naomy import NaomyPairingCode
    from app.models.tenant import Tenant, TenantPlan
    from app.models.user import Role, User
    from app.services import magic_login, preregistration, prospection
    from app.services import naomy_bridge as nb
    from app.services.audit import log_audit_event
    from app.services.countries import COUNTRIES

    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    data = validate(payload)
    currency = next((cur for code, _, cur in COUNTRIES if code == data["country"]), None)
    if currency is None:
        raise NaomyError(422, "pays_inconnu", "Pays non proposé par Bob.")
    if not nb.events_url_ok(data["url"], settings.naomy_allowed_host, settings.naomy_allowed_path):
        raise NaomyError(422, "url_evenements_refusee",
                         f"L'adresse doit commencer par https://{settings.naomy_allowed_host}{settings.naomy_allowed_path}.")
    if limiter is not None:
        if not await limiter.is_allowed(f"naomy-compte:{nb.digest(data['digits'])}", PER_NUMBER, 86400):
            raise NaomyError(429, "trop_de_requetes", "Trop de créations pour ce numéro : réessayez demain.",
                             {"Retry-After": "86400"})
        if not await limiter.is_allowed("naomy-compte:jour", PER_DAY, 86400):
            raise NaomyError(429, "trop_de_requetes", "Trop de créations aujourd'hui : réessayez demain.",
                             {"Retry-After": "3600"})

    found = await existing_tenant(db, data["digits"], data["email"])
    if found is not None:
        await _ask_existing(db, found, data, now, mailer)
        raise NaomyError(409, "boutique_existante",
                         "Ce numéro ou cet e-mail a déjà une boutique Bob : la demande de connexion lui a été envoyée "
                         "(à accepter dans Bob).")

    tenant = Tenant(name=data["name"], country=data["country"], currency=currency, phone=data["number"],
                    email=data["email"], business_type=data["sector"], business_type_chosen_at=now,
                    plan=TenantPlan.INDEPENDANT, created_via=ORIGIN, team_mode="TEAM", team_trial_at=now)
    db.add(tenant)
    await db.flush()
    if tenant.business_type == "INSURANCE_BROKER":
        from app.services.insurance import seed_products

        await seed_products(db, tenant)
    owner = User(tenant_id=tenant.id, email=data["email"], full_name=data["contact"], role=Role.OWNER,
                 hashed_password=hash_password(secrets.token_urlsafe(32)))  # aucun mot de passe connu : lien, ou « oublié »
    db.add(owner)
    await db.flush()
    prereg = await preregistration.on_signup(db, tenant)
    if not prereg:
        start_trial(tenant, now)  # pré-inscrit : l'essai démarre à l'ouverture de l'accès
    await log_audit_event(db, actor="NAOMY", action="TENANT_CREATED_BY_NAOMY", tenant_id=tenant.id,
                          details={"company_name": data["name"], "business_type": data["sector"], "city": data["city"],
                                   "preregistered": prereg})
    code = nb.new_code()
    db.add(NaomyPairingCode(tenant_id=tenant.id, code_hash=nb.digest(code), expires_at=now + nb.CODE_TTL, scope="complete"))
    await db.commit()

    linked = await nb.pair(db, {"code": code, "compte_naomy": data["account"], "numero_confirmation": data["number"],
                                "url_evenements": data["url"]}, now)
    connection = await nb.active_connection(db, tenant.id)
    connection.assistant_enabled, connection.assistant_enabled_at = True, now  # « Mon équipe » : l'assistant de Bob passe par Naomy dès le départ
    link = await magic_login.create(db, owner, now)
    await db.commit()

    await prospection.attach(db, None, tenant.id, "SIGNUP", email=data["email"])
    if prereg:
        await preregistration.send_signup_emails(db, tenant, owner)
    return {**linked, "boutique": await nb.shop_view(db, tenant), "lien_connexion": link,
            "essai_jusqu_au": tenant.paid_until.isoformat() if tenant.paid_until else None, "pre_inscrit": prereg}


async def new_link(db, tenant, limiter=None, now: datetime | None = None) -> dict:
    """POST /lien-connexion : un nouveau lien « Connecter mon WhatsApp à Bob »."""
    from app.models.user import Role, User
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services import magic_login

    now = now or datetime.now(timezone.utc)
    if (await db.execute(select(WhatsAppAccount.id).where(WhatsAppAccount.tenant_id == tenant.id).limit(1))).scalar_one_or_none():
        raise NaomyError(409, "deja_connecte", "Le WhatsApp de cette boutique est déjà connecté à Bob.")
    owner = (await db.execute(select(User).where(User.tenant_id == tenant.id, User.role == Role.OWNER,
                                                 User.active.is_(True)).order_by(User.created_at).limit(1))).scalar_one_or_none()
    if owner is None:
        raise NaomyError(409, "deja_connecte", "Aucun propriétaire actif pour cette boutique.")
    if owner.mfa_enabled:
        raise NaomyError(409, "double_verification", "Le propriétaire a activé la double vérification : il se connecte normalement.")
    if await magic_login.issued_today(db, tenant.id, now) >= magic_login.PER_DAY or (
            limiter is not None and not await limiter.is_allowed(f"naomy-lien:{tenant.id}", magic_login.PER_DAY, 86400)):
        raise NaomyError(429, "trop_de_requetes", "Plus de 5 liens en 24 h : réessayez demain.", {"Retry-After": "3600"})
    link = await magic_login.create(db, owner, now)
    await db.commit()
    return link
