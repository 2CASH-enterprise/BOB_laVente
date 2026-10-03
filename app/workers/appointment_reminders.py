"""
Lot 25 — rappels de rendez-vous par email, la veille à partir de 18 h (heure de la boutique).

Tâche lancée toutes les 15 minutes. Chaque rendez-vous est traité dans sa propre session : un
échec (email refusé, boutique supprimée) n'empêche jamais les autres. Un rappel n'est marqué
envoyé que si l'email est réellement parti, et n'est jamais envoyé deux fois.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def send_due_reminders(session_factory=None, now: datetime | None = None, send=None, send_whatsapp=None) -> dict:
    engine = None
    if session_factory is None:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.core.config import get_settings

        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    if send is None:
        from app.services.email_service import send_email as send
    try:
        return await _send_all(session_factory, now or datetime.now(timezone.utc), send, send_whatsapp)
    finally:
        if engine is not None:
            await engine.dispose()


async def _whatsapp_reminder(db, tenant, conversation, customer, text) -> bool:
    """Lot 35 — message fixe, uniquement si la conversation est ouverte (sinon l'email suffit)."""
    from app.services.appointment_service import send_fixed_message

    sent, _ = await send_fixed_message(db, tenant, conversation, customer, text, "BOB", "appointment_reminder")
    return sent


async def _send_all(factory, now: datetime, send, send_whatsapp=None) -> dict:
    send_whatsapp = send_whatsapp or _whatsapp_reminder
    from app.models.appointment_request import STATUS_CONFIRMED, AppointmentRequest
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.models.tenant import Tenant
    from app.services import appointment_service
    from app.services.handoff_service import alert_emails, commercial_for_customer, conversation_link, customer_display_name
    from app.services.bob_pause import is_paused
    from app.services.local_time import tenant_zone

    async with factory() as db:
        ids = (await db.execute(select(AppointmentRequest.id).where(
            AppointmentRequest.status == STATUS_CONFIRMED,
            AppointmentRequest.scheduled_at.is_not(None),
            AppointmentRequest.scheduled_at > now,
        ))).scalars().all()

    report = {"staff": 0, "customer": 0, "whatsapp": 0, "followups": 0, "failed": 0}
    for appointment_id in ids:
        async with factory() as db:
            appointment = await db.get(AppointmentRequest, appointment_id)
            tenant = await db.get(Tenant, appointment.tenant_id) if appointment else None
            if appointment is None or tenant is None or is_paused(tenant, now):  # lot 51 : Bob en pause
                continue
            customer = await db.get(Customer, appointment.customer_id)
            zone = tenant_zone(tenant)
            due = appointment_service.reminder_due(appointment, now, zone, customer.email if customer else None)
            try:
                if due.staff:
                    conversation = await db.get(Conversation, appointment.conversation_id)
                    from app.services.prospect import build_fiche

                    subject, body = appointment_service.staff_reminder_email(
                        appointment, customer_display_name(customer) if customer else "Client", zone,
                        conversation_link(conversation), await build_fiche(db, tenant, customer, now),
                    )
                    # Lot 27 : la boutique ET le commercial qui a amené le client. Le rappel est
                    # marqué envoyé dès que la boutique l'a reçu (le commercial est un plus).
                    commercial = await commercial_for_customer(db, customer)
                    results = [send(**email) for email in alert_emails(tenant.email, commercial, subject, body)]
                    report["failed"] += results.count(False)
                    if results and results[0]:
                        appointment.reminder_sent_at = now
                        report["staff"] += 1
                if due.whatsapp and customer is not None:
                    # Lot 35 — rappel WhatsApp gratuit seulement si le prospect a écrit récemment
                    # (fenêtre ouverte) ; sinon rien sur WhatsApp : l'email fait le rappel.
                    from app.services.human_reply import reply_window_closes_at

                    # La conversation la plus récente du client (il a pu en ouvrir une nouvelle).
                    conversation = (await db.execute(
                        select(Conversation).where(Conversation.tenant_id == tenant.id, Conversation.customer_id == customer.id)
                        .order_by(Conversation.created_at.desc()).limit(1)
                    )).scalar_one_or_none()
                    closes_at = await reply_window_closes_at(db, conversation) if conversation else None
                    if closes_at is not None and now < closes_at:
                        text = appointment_service.whatsapp_reminder_message(appointment, tenant.name, zone)
                        if await send_whatsapp(db, tenant, conversation, customer, text):
                            appointment.customer_whatsapp_reminder_sent_at = now
                            report["whatsapp"] += 1
                        else:
                            report["failed"] += 1
                if due.customer:
                    subject, body = appointment_service.customer_reminder_email(appointment, tenant.name, zone,
                                                                                powered_by=not tenant.is_paid)
                    if send(to=customer.email, subject=subject, body=body, from_name=tenant.name, reply_to=tenant.email):
                        appointment.customer_reminder_sent_at = now
                        report["customer"] += 1
                    else:
                        report["failed"] += 1
                await db.commit()
            except Exception:  # noqa: BLE001 — un rendez-vous en échec ne bloque jamais les autres
                report["failed"] += 1
                logger.warning("Rappel de rendez-vous impossible (rendez-vous %s)", appointment_id)

    # Lot 36 — relance unique des prospects venus mais pas décidés, deux jours après la visite.
    from app.services import appointment_outcome

    async with factory() as db:
        followup_ids = await appointment_outcome.appointments_needing_followup(db, now)
    for appointment_id in followup_ids:
        async with factory() as db:
            appointment = await db.get(AppointmentRequest, appointment_id)
            tenant = await db.get(Tenant, appointment.tenant_id) if appointment else None
            if appointment is None or tenant is None or is_paused(tenant, now):  # lot 51 : Bob en pause
                continue
            if not appointment_outcome.followup_due(appointment, now, tenant_zone(tenant)):
                continue
            try:
                await appointment_outcome.send_followup(db, tenant, appointment, now, send_email=send,
                                                        send_whatsapp=send_whatsapp if send_whatsapp is not _whatsapp_reminder else None)
                await db.commit()
                report["followups"] += 1
            except Exception:  # noqa: BLE001
                report["failed"] += 1
                logger.warning("Relance après visite impossible (rendez-vous %s)", appointment_id)
    return report


@celery_app.task(name="app.workers.appointment_reminders.send_appointment_reminders_task")
def send_appointment_reminders_task() -> dict:
    return asyncio.run(send_due_reminders())
