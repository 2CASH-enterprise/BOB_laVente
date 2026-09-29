"""
Page « Rendez-vous » (lot 25, concession) : lister, confirmer, annuler.
Toujours limité à la boutique de l'utilisateur ; confirmation et annulation par un humain (AGENT+).
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.appointment_request import (
    APPOINTMENT_KINDS,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_REQUESTED,
    AppointmentRequest,
)
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.services import appointment_service
from app.services.audit import log_audit_event
from app.services.handoff_service import customer_display_name
from app.services.human_reply import reply_window_closes_at
from app.services.local_time import as_utc, format_local, local_to_utc, tenant_zone

router = APIRouter(prefix="/api/v1/appointments", tags=["appointments"])


class AppointmentOut(BaseModel):
    id: UUID
    kind: str
    kind_label: str
    vehicle: str | None
    availability: str
    need: str | None
    budget: str | None
    trade_in: str | None
    financing_interest: bool | None
    notes: str | None
    status: str
    scheduled_at: datetime | None
    scheduled_label: str | None
    created_at: datetime
    customer: str
    conversation_id: UUID
    can_notify: bool  # le client a écrit il y a moins de 20 h : un message WhatsApp peut partir


class AppointmentList(BaseModel):
    timezone: str
    items: list[AppointmentOut]


class ConfirmRequest(BaseModel):
    scheduled_local: datetime  # heure de la boutique, sans fuseau (champ date + heure du navigateur)
    notify_customer: bool = True


class CancelRequest(BaseModel):
    notify_customer: bool = False


class ActionResult(BaseModel):
    appointment: AppointmentOut
    customer_notified: bool | None  # None : on n'a pas demandé à prévenir le client
    notify_error: str | None = None


async def _out(db: AsyncSession, appointment: AppointmentRequest, zone, now: datetime) -> AppointmentOut:
    customer = await db.get(Customer, appointment.customer_id)
    conversation = await db.get(Conversation, appointment.conversation_id)
    closes_at = await reply_window_closes_at(db, conversation) if conversation is not None else None
    return AppointmentOut(
        id=appointment.id,
        kind=appointment.kind,
        kind_label=APPOINTMENT_KINDS.get(appointment.kind, appointment.kind),
        vehicle=appointment.vehicle_label,
        availability=appointment.availability,
        need=appointment.need,
        budget=appointment.budget,
        trade_in=appointment.trade_in,
        financing_interest=appointment.financing_interest,
        notes=appointment.notes,
        status=appointment.status,
        scheduled_at=appointment.scheduled_at,
        scheduled_label=format_local(appointment.scheduled_at, zone) if appointment.scheduled_at else None,
        created_at=appointment.created_at,
        customer=customer_display_name(customer) if customer else "Client",
        conversation_id=appointment.conversation_id,
        can_notify=closes_at is not None and now < closes_at,
    )


async def _get(db: AsyncSession, tenant_id, appointment_id: UUID) -> AppointmentRequest:
    appointment = (await db.execute(select(AppointmentRequest).where(
        AppointmentRequest.id == appointment_id, AppointmentRequest.tenant_id == tenant_id,
    ))).scalar_one_or_none()
    if appointment is None:
        raise HTTPException(status_code=404, detail="Rendez-vous introuvable")
    return appointment


@router.get("", response_model=AppointmentList)
async def list_appointments(
    view: str = Query("pending", pattern="^(pending|confirmed|closed)$"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppointmentList:
    tenant = await db.get(Tenant, current_user.tenant_id)
    zone = tenant_zone(tenant)
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(AppointmentRequest).where(AppointmentRequest.tenant_id == current_user.tenant_id)
    )).scalars().all()

    def in_view(a: AppointmentRequest) -> bool:
        upcoming = a.scheduled_at is not None and as_utc(a.scheduled_at) >= now
        if view == "pending":
            return a.status == STATUS_REQUESTED
        if view == "confirmed":
            return a.status == STATUS_CONFIRMED and upcoming
        return a.status == STATUS_CANCELLED or (a.status == STATUS_CONFIRMED and not upcoming)

    selected = [a for a in rows if in_view(a)]
    if view == "confirmed":
        selected.sort(key=lambda a: as_utc(a.scheduled_at))
    else:
        selected.sort(key=lambda a: as_utc(a.created_at), reverse=(view == "closed"))
    return AppointmentList(timezone=zone.key, items=[await _out(db, a, zone, now) for a in selected])


@router.post("/{appointment_id}/confirm", response_model=ActionResult, dependencies=[Depends(require_role("AGENT"))])
async def confirm_appointment(
    appointment_id: UUID,
    payload: ConfirmRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ActionResult:
    tenant = await db.get(Tenant, current_user.tenant_id)
    appointment = await _get(db, current_user.tenant_id, appointment_id)
    if appointment.status == STATUS_CANCELLED:
        raise HTTPException(status_code=409, detail="Ce rendez-vous est annulé.")
    zone = tenant_zone(tenant)
    now = datetime.now(timezone.utc)
    scheduled = local_to_utc(payload.scheduled_local.replace(tzinfo=None), zone)
    if scheduled <= now:
        raise HTTPException(status_code=422, detail="La date du rendez-vous est déjà passée.")

    user_id = str(current_user.user_id)
    appointment_service.confirm(appointment, scheduled, user_id, now=now)
    notified, error = None, None
    if payload.notify_customer:
        conversation = await db.get(Conversation, appointment.conversation_id)
        customer = await db.get(Customer, appointment.customer_id)
        text = appointment_service.confirmation_message(appointment, tenant.name, zone)
        notified, error = await appointment_service.send_fixed_message(
            db, tenant, conversation, customer, text, user_id, "appointment_confirmed",
        )
    await log_audit_event(
        db, actor=user_id, action="APPOINTMENT_CONFIRMED", tenant_id=tenant.id,
        details={"appointment_id": str(appointment.id), "scheduled_at": scheduled.isoformat(), "customer_notified": notified},
    )
    await db.commit()
    return ActionResult(appointment=await _out(db, appointment, zone, now), customer_notified=notified, notify_error=error)


@router.post("/{appointment_id}/cancel", response_model=ActionResult, dependencies=[Depends(require_role("AGENT"))])
async def cancel_appointment(
    appointment_id: UUID,
    payload: CancelRequest | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ActionResult:
    payload = payload or CancelRequest()
    tenant = await db.get(Tenant, current_user.tenant_id)
    appointment = await _get(db, current_user.tenant_id, appointment_id)
    if appointment.status == STATUS_CANCELLED:
        raise HTTPException(status_code=409, detail="Ce rendez-vous est déjà annulé.")
    zone = tenant_zone(tenant)
    now = datetime.now(timezone.utc)
    user_id = str(current_user.user_id)
    notified, error = None, None
    if payload.notify_customer:
        conversation = await db.get(Conversation, appointment.conversation_id)
        customer = await db.get(Customer, appointment.customer_id)
        text = appointment_service.cancellation_message(appointment, tenant.name, zone)
        notified, error = await appointment_service.send_fixed_message(
            db, tenant, conversation, customer, text, user_id, "appointment_cancelled",
        )
    appointment_service.cancel(appointment, now=now)
    await log_audit_event(
        db, actor=user_id, action="APPOINTMENT_CANCELLED", tenant_id=tenant.id,
        details={"appointment_id": str(appointment.id), "customer_notified": notified},
    )
    await db.commit()
    return ActionResult(appointment=await _out(db, appointment, zone, now), customer_notified=notified, notify_error=error)
