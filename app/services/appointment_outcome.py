"""
Lot 36 — issue d'un rendez-vous passé (concession) et relance unique du prospect.

- Le conseiller indique l'issue : venu et vendu, venu et à relancer, venu et pas intéressé, absent.
- « Vendu » : le véhicule du rendez-vous passe en indisponible (stock 0), pour que Bob ne le
  propose plus. Il se remet en stock d'un clic dans Produits.
- Relance, UNE seule fois : pour un absent tout de suite, pour un « à relancer » deux jours après
  (entre 9 h et 19 h, heure de la boutique). Canal, dans l'ordre :
    1. WhatsApp, seulement si le prospect a écrit récemment (message gratuit) ;
    2. email s'il est connu — pour un « à relancer » (démarche commerciale), seulement si le
       prospect a accepté de recevoir des offres ; pour un absent (son rendez-vous), toujours ;
    3. sinon une tâche « À rappeler » pour la concession.
- Messages fixes, jamais rédigés par l'IA.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.appointment_request import (
    OUTCOME_FOLLOW_UP,
    OUTCOME_NO_SHOW,
    OUTCOME_SOLD,
    OUTCOMES,
    STATUS_CONFIRMED,
    AppointmentRequest,
)
from app.services.appointment_service import subject_phrase
from app.services.local_time import as_utc

FOLLOW_UP_DELAY = timedelta(days=2)
SEND_HOURS = (9, 19)  # heure de la boutique
TASK_VISIBLE_DAYS = 7

CHANNEL_WHATSAPP = "WHATSAPP"
CHANNEL_EMAIL = "EMAIL"
CHANNEL_TASK = "TASK"


class OutcomeError(Exception):
    pass


def _vehicle(appointment) -> str:
    return appointment.vehicle_label or "le véhicule"


def whatsapp_text(appointment, shop_name: str) -> str:
    if appointment.outcome == OUTCOME_NO_SHOW:
        return (f"Bonjour, nous ne vous avons pas vu pour votre {subject_phrase(appointment)} chez {shop_name}. "
                "Souhaitez-vous choisir un autre créneau ? Répondez simplement à ce message.")
    return (f"Bonjour, merci encore pour votre visite chez {shop_name} ! Avez-vous des questions sur "
            f"{_vehicle(appointment)} ? Nous restons à votre disposition pour un nouvel essai ou toute information.")


def email_text(appointment, shop_name: str) -> tuple[str, str]:
    if appointment.outcome == OUTCOME_NO_SHOW:
        return (f"Votre rendez-vous chez {shop_name}",
                f"Bonjour,\n\nNous ne vous avons pas vu pour votre {subject_phrase(appointment)}. Souhaitez-vous "
                "choisir un autre créneau ? Répondez simplement à cet email ou écrivez-nous sur WhatsApp.\n\n"
                f"À bientôt,\n{shop_name}")
    return (f"Merci pour votre visite chez {shop_name}",
            f"Bonjour,\n\nMerci encore pour votre visite. Avez-vous des questions sur {_vehicle(appointment)} ? "
            "Nous restons à votre disposition pour un nouvel essai ou toute information : répondez simplement à "
            f"cet email ou écrivez-nous sur WhatsApp.\n\nÀ bientôt,\n{shop_name}")


def followup_due(appointment, now: datetime, zone) -> bool:
    if appointment.outcome != OUTCOME_FOLLOW_UP or appointment.followup_sent_at is not None or appointment.outcome_at is None:
        return False
    if as_utc(appointment.outcome_at) + FOLLOW_UP_DELAY > as_utc(now):
        return False
    return SEND_HOURS[0] <= as_utc(now).astimezone(zone).hour < SEND_HOURS[1]


async def _latest_conversation(db, appointment):
    from app.models.conversation import Conversation

    return (await db.execute(
        select(Conversation).where(Conversation.tenant_id == appointment.tenant_id,
                                   Conversation.customer_id == appointment.customer_id)
        .order_by(Conversation.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def send_followup(db, tenant, appointment, now: datetime, send_email=None, send_whatsapp=None) -> str:
    """Relance unique : WhatsApp si la conversation est ouverte, sinon email, sinon tâche. Renvoie le canal."""
    from app.models.customer import Customer
    from app.services.appointment_service import send_fixed_message
    from app.services.human_reply import reply_window_closes_at

    if send_email is None:
        from app.services.email_service import send_email
    customer = await db.get(Customer, appointment.customer_id)
    channel = CHANNEL_TASK
    conversation = await _latest_conversation(db, appointment)
    closes_at = await reply_window_closes_at(db, conversation) if conversation is not None else None
    if customer is not None and closes_at is not None and as_utc(now) < closes_at:
        text = whatsapp_text(appointment, tenant.name)
        if send_whatsapp is not None:
            sent = await send_whatsapp(db, tenant, conversation, customer, text)
        else:
            sent, _ = await send_fixed_message(db, tenant, conversation, customer, text, "BOB", "appointment_followup")
        if sent:
            channel = CHANNEL_WHATSAPP
    if channel == CHANNEL_TASK and customer is not None and customer.email \
            and (appointment.outcome == OUTCOME_NO_SHOW or customer.marketing_consent):
        subject, body = email_text(appointment, tenant.name)
        if send_email(to=customer.email, subject=subject, body=body, from_name=tenant.name, reply_to=tenant.email):
            channel = CHANNEL_EMAIL
    appointment.followup_sent_at = now
    appointment.followup_channel = channel
    return channel


async def record_outcome(db, tenant, appointment, outcome: str, user_id: str, now: datetime | None = None,
                         send_email=None, send_whatsapp=None) -> dict:
    from app.models.product import Product

    now = now or datetime.now(timezone.utc)
    if outcome not in OUTCOMES:
        raise OutcomeError("Issue inconnue.")
    if appointment.status != STATUS_CONFIRMED or appointment.scheduled_at is None:
        raise OutcomeError("Seul un rendez-vous confirmé peut recevoir une issue.")
    if as_utc(appointment.scheduled_at) > as_utc(now):
        raise OutcomeError("Ce rendez-vous n'a pas encore eu lieu.")
    appointment.outcome, appointment.outcome_at, appointment.outcome_by = outcome, now, user_id
    result = {"vehicle_unavailable": False, "followup_channel": None}
    if outcome == OUTCOME_SOLD and appointment.product_id is not None:
        product = await db.get(Product, appointment.product_id)
        if product is not None and product.tenant_id == appointment.tenant_id:
            product.stock_quantity = 0
            result["vehicle_unavailable"] = True
    if outcome == OUTCOME_NO_SHOW and appointment.followup_sent_at is None:
        result["followup_channel"] = await send_followup(db, tenant, appointment, now, send_email, send_whatsapp)
    return result


def open_tasks(appointments, now: datetime) -> list:
    """Relances à faire par la concession (ni WhatsApp ni email possibles), visibles 7 jours."""
    return [a for a in appointments
            if a.followup_channel == CHANNEL_TASK and a.followup_sent_at is not None
            and as_utc(a.followup_sent_at) >= as_utc(now) - timedelta(days=TASK_VISIBLE_DAYS)]


async def appointments_needing_followup(db, now: datetime) -> list:
    return (await db.execute(select(AppointmentRequest.id).where(
        AppointmentRequest.outcome == OUTCOME_FOLLOW_UP,
        AppointmentRequest.followup_sent_at.is_(None),
        AppointmentRequest.outcome_at <= now - FOLLOW_UP_DELAY,
    ))).scalars().all()
