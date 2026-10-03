"""
Relance automatique des conversations abandonnées (section 23).

Séquence : conversation sans réponse -> attente N1 heures -> première relance ->
attente N2 heures -> deuxième relance -> STOP (jamais de troisième relance).

Lot 49 — les relances partent par EMAIL, jamais sur WhatsApp : passé 20 h sans message du
client, WhatsApp n'accepte plus de message libre. L'email (personnalisé, avec l'offre du moment
saisie par le commerçant) invite à reprendre la discussion sur WhatsApp. Il ne part qu'aux
clients qui ont donné leur email ET accepté de recevoir les offres, avec un lien de
désinscription. L'activation des relances par le commerçant suffit : aucun envoi WhatsApp.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.followup_settings import TenantFollowupSettings
from app.models.whatsapp_account import WhatsAppAccount

logger = logging.getLogger(__name__)

FOLLOWUP_MAX_AGE = timedelta(days=14)


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite (tests) renvoie parfois des datetime naïfs même sur une colonne timezone=True ;
    PostgreSQL (production) les renvoie toujours conscients du fuseau. On uniformise en UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def find_eligible_conversations(
    db: AsyncSession, tenant_id, settings: TenantFollowupSettings, now: datetime | None = None
) -> list[Conversation]:
    """
    Ne renvoie que les conversations réellement éligibles à CE moment précis : jamais
    WAITING_HUMAN (un humain est déjà dessus, section 28) ni CLOSED, jamais avant le
    délai configuré, jamais au-delà de la deuxième relance.
    """
    now = _ensure_aware(now or datetime.now(timezone.utc))

    stmt = select(Conversation).where(
        Conversation.tenant_id == tenant_id,
        Conversation.status == ConversationStatus.ACTIVE,
        Conversation.last_message_at.is_not(None),
        # Lot 50 — jamais de relance pour une vieille conversation (activation des relances, reprise…).
        Conversation.last_message_at >= now - FOLLOWUP_MAX_AGE,
    )
    candidates = list((await db.execute(stmt)).scalars().all())

    eligible = []
    for conv in candidates:
        if conv.followup_stage == 0:
            threshold = _ensure_aware(conv.last_message_at) + timedelta(hours=settings.first_followup_hours)
            if now >= threshold:
                eligible.append(conv)
        elif conv.followup_stage == 1:
            reference = conv.last_followup_at or conv.last_message_at
            threshold = _ensure_aware(reference) + timedelta(hours=settings.second_followup_hours)
            if now >= threshold:
                eligible.append(conv)
        # followup_stage >= 2 : STOP, jamais de relance supplémentaire (section 23)

    return eligible


async def send_followup(
    db: AsyncSession,
    tenant_id,
    conversation: Conversation,
    settings: TenantFollowupSettings,
    now: datetime | None = None,
    send_email=None,
) -> bool:
    """
    Lot 49 — la relance part par EMAIL, jamais sur WhatsApp (au-delà de 20 h, WhatsApp exige un
    modèle payant et approuvé). Seulement si le client a donné son email ET accepté les offres.
    L'étape avance dans tous les cas : un client sans email n'est jamais retenté en boucle.
    Retourne True si l'email est parti.
    """
    from app.models.tenant import Tenant

    now = now or datetime.now(timezone.utc)
    stage = conversation.followup_stage
    tenant = await db.get(Tenant, tenant_id)
    customer = await db.get(Customer, conversation.customer_id)
    sent = False
    if tenant is not None and customer is not None and customer.tenant_id == tenant_id \
            and customer.email and customer.marketing_consent:
        mail = await build_followup_email(db, tenant, customer, settings, stage, now)
        if send_email is None:
            from app.services.email_service import send_email
        try:
            sent = bool(send_email(**mail))
        except Exception:  # noqa: BLE001 — jamais interrompre le traitement des autres conversations
            logger.exception("Échec d'envoi de la relance par email (conversation %s)", conversation.id)
        if sent:
            db.add(Message(
                tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                message_type="followup_email", content=f"Relance envoyée par email : « {mail['subject']} »",
            ))
    conversation.followup_stage = stage + 1
    conversation.last_followup_at = now
    await db.commit()
    return sent


def active_offer(settings: TenantFollowupSettings, today) -> dict | None:
    """L'offre saisie par le commerçant, seulement si elle n'est pas terminée."""
    text = (settings.offer_text or "").strip()
    if not text or (settings.offer_ends_on is not None and settings.offer_ends_on < today):
        return None
    return {"text": text, "code": (settings.offer_code or "").strip() or None, "ends_on": settings.offer_ends_on}


async def _last_viewed(db: AsyncSession, tenant_id, customer_id) -> str | None:
    from app.models.customer_product_view import CustomerProductView
    from app.models.product import Product

    row = (await db.execute(
        select(Product.name).join(CustomerProductView, CustomerProductView.product_id == Product.id).where(
            CustomerProductView.tenant_id == tenant_id, CustomerProductView.customer_id == customer_id,
            Product.tenant_id == tenant_id, Product.active.is_(True),
        ).order_by(CustomerProductView.last_viewed_at.desc()).limit(1)
    )).scalar_one_or_none()
    return row


_UNSET = object()


async def build_followup_email(db: AsyncSession, tenant, customer, settings: TenantFollowupSettings, stage: int,
                               now: datetime, viewed=_UNSET) -> dict:
    """
    Email personnalisé : prénom, produit ou véhicule regardé, message et offre du commerçant, bouton
    « Reprendre sur WhatsApp », désinscription. Texte fixe assemblé par le code : Bob n'invente rien.
    """
    from urllib.parse import quote

    from app.services.address_form import uses_tu
    from app.services.business_type import is_dealership
    from app.services.campaign_service import _build_email_body, _unsubscribe_headers
    from app.services.unsubscribe_service import build_unsubscribe_url

    tu = uses_tu(tenant)
    first = (customer.first_name or "").strip()
    offer = active_offer(settings, now.date())
    if viewed is _UNSET:
        viewed = await _last_viewed(db, tenant.id, customer.id)
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()

    if offer:
        subject = (f"{first}, une offre pour toi chez {tenant.name}" if tu else f"{first}, une offre pour vous chez {tenant.name}") \
            if first else f"Une offre {'pour toi' if tu else 'pour vous'} chez {tenant.name}"
    elif stage == 0:
        subject = f"{first}, on reprend notre échange ?" if first else f"{tenant.name} : on reprend notre échange ?"
    else:
        subject = f"{tenant.name} {'t' + chr(39) + 'attend' if tu else 'vous attend'} toujours"

    lines = [f"Bonjour {first}," if first else "Bonjour,", ""]
    lines.append((settings.first_message if stage == 0 else settings.second_message).strip())
    if viewed:
        what = "le véhicule" if is_dealership(tenant) else "l'article"
        lines += ["", f"{'Tu regardais' if tu else 'Vous regardiez'} {what} « {viewed} ». "
                      f"{'Il t' + chr(39) + 'intéresse' if tu else 'Il vous intéresse'} toujours ?"]
    if offer:
        lines += ["", f"Offre du moment : {offer['text']}"]
        if offer["code"]:
            lines.append(f"Code promo : {offer['code']}")
        if offer["ends_on"]:
            lines.append(f"Valable jusqu'au : {offer['ends_on'].strftime('%d/%m/%Y')}")
    wa_link = None
    if account is not None and account.display_phone_number:
        digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
        hello = "Bonjour, je reviens vers vous suite à votre email"
        wa_link = f"https://wa.me/{digits}?text={quote(hello)}"
    unsubscribe_url = build_unsubscribe_url(customer.id)
    body = _build_email_body("\n".join(lines), None, tenant.name, unsubscribe_url, tu=tu, powered_by=not tenant.is_paid)
    if wa_link:
        cta = f"\n\nReprendre sur WhatsApp : {wa_link}"
        separator = body.index("\n\n—") if "\n\n—" in body else len(body)
        body = body[:separator] + cta + body[separator:]
    return {"to": customer.email, "subject": subject, "body": body, "from_name": tenant.name,
            "reply_to": tenant.email, "extra_headers": _unsubscribe_headers(unsubscribe_url)}


async def run_followups_for_tenant(db: AsyncSession, tenant_id, now: datetime | None = None, send_email=None) -> int:
    """Retourne le nombre de relances effectivement envoyées pour ce tenant."""
    from app.models.tenant import Tenant

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_paid:
        return 0  # relances réservées aux plans payants (freemium exclu)
    from app.services.bob_pause import is_paused

    if is_paused(tenant, now):
        return 0  # lot 51 : Bob en pause (boutique suspendue ou abonnement non renouvelé)

    settings_stmt = select(TenantFollowupSettings).where(TenantFollowupSettings.tenant_id == tenant_id)
    settings = (await db.execute(settings_stmt)).scalar_one_or_none()

    if settings is None or not settings.enabled:
        return 0

    eligible = await find_eligible_conversations(db, tenant_id, settings, now=now)
    sent_count = 0
    for conversation in eligible:
        if await send_followup(db, tenant_id, conversation, settings, now=now, send_email=send_email):
            sent_count += 1
    return sent_count


async def eligible_recipients(db: AsyncSession, tenant_id) -> int:
    """Clients qui peuvent recevoir une relance par email : email donné ET offres acceptées."""
    from sqlalchemy import func

    return (await db.execute(select(func.count(Customer.id)).where(
        Customer.tenant_id == tenant_id, Customer.email.is_not(None), Customer.email != "",
        Customer.marketing_consent.is_(True),
    ))).scalar_one()
