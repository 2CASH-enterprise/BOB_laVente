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


async def send_due_reminders(session_factory=None, now: datetime | None = None, send=None) -> dict:
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
        return await _send_all(session_factory, now or datetime.now(timezone.utc), send)
    finally:
        if engine is not None:
            await engine.dispose()


async def _send_all(factory, now: datetime, send) -> dict:
    from app.models.appointment_request import STATUS_CONFIRMED, AppointmentRequest
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.models.tenant import Tenant
    from app.services import appointment_service
    from app.services.handoff_service import conversation_link, customer_display_name
    from app.services.local_time import tenant_zone

    async with factory() as db:
        ids = (await db.execute(select(AppointmentRequest.id).where(
            AppointmentRequest.status == STATUS_CONFIRMED,
            AppointmentRequest.scheduled_at.is_not(None),
            AppointmentRequest.scheduled_at > now,
        ))).scalars().all()

    report = {"staff": 0, "customer": 0, "failed": 0}
    for appointment_id in ids:
        async with factory() as db:
            appointment = await db.get(AppointmentRequest, appointment_id)
            tenant = await db.get(Tenant, appointment.tenant_id) if appointment else None
            if appointment is None or tenant is None or not tenant.active:
                continue
            customer = await db.get(Customer, appointment.customer_id)
            zone = tenant_zone(tenant)
            due = appointment_service.reminder_due(appointment, now, zone, customer.email if customer else None)
            try:
                if due.staff:
                    conversation = await db.get(Conversation, appointment.conversation_id)
                    subject, body = appointment_service.staff_reminder_email(
                        appointment, customer_display_name(customer) if customer else "Client", zone,
                        conversation_link(conversation),
                    )
                    if send(to=tenant.email, subject=subject, body=body):
                        appointment.reminder_sent_at = now
                        report["staff"] += 1
                    else:
                        report["failed"] += 1
                if due.customer:
                    subject, body = appointment_service.customer_reminder_email(appointment, tenant.name, zone)
                    if send(to=customer.email, subject=subject, body=body, from_name=tenant.name, reply_to=tenant.email):
                        appointment.customer_reminder_sent_at = now
                        report["customer"] += 1
                    else:
                        report["failed"] += 1
                await db.commit()
            except Exception:  # noqa: BLE001 — un rendez-vous en échec ne bloque jamais les autres
                report["failed"] += 1
                logger.warning("Rappel de rendez-vous impossible (rendez-vous %s)", appointment_id)
    return report


@celery_app.task(name="app.workers.appointment_reminders.send_appointment_reminders_task")
def send_appointment_reminders_task() -> dict:
    return asyncio.run(send_due_reminders())
