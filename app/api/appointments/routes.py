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

from app.services.business_type import only_dealership
from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.appointment_request import (
    APPOINTMENT_KINDS,
    OUTCOMES,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_REQUESTED,
    AppointmentRequest,
)
from app.models.conversation import Conversation
from app.models.contact_point import ContactPoint
from app.models.customer import Customer
from app.models.tenant import Tenant
from app.services import appointment_service
from app.services.audit import log_audit_event
from app.services.handoff_service import customer_display_name
from app.services.human_reply import reply_window_closes_at
from app.services.local_time import as_utc, format_local, local_to_utc, tenant_zone

router = APIRouter(prefix="/api/v1/appointments", tags=["appointments"], dependencies=[Depends(only_dealership())])  # lot 32


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
    outcome: str | None = None  # lot 36
    outcome_label: str | None = None
    can_set_outcome: bool = False
    followup_channel: str | None = None
    followup_sent_at: datetime | None = None
    customer_change: str | None = None  # lot 35 : « Annulé par le client », « Déplacé par le client »
    whatsapp_reminder_sent_at: datetime | None = None  # lot 35
    confirmed_by_bob: bool = False  # lot 29 : créneau choisi par le client et réservé par Bob
    referred_by: str | None = None  # lot 27 : commercial dont le lien a amené le client
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
    referred_by = None
    if customer is not None and customer.referred_contact_point_id is not None:
        cp = await db.get(ContactPoint, customer.referred_contact_point_id)
        if cp is not None and cp.tenant_id == appointment.tenant_id:
            referred_by = cp.owner_name or cp.owner_email
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
        referred_by=referred_by,
        confirmed_by_bob=appointment.confirmed_by == "BOB",
        outcome=appointment.outcome,
        outcome_label=OUTCOMES.get(appointment.outcome) if appointment.outcome else None,
        can_set_outcome=(appointment.status == STATUS_CONFIRMED and appointment.scheduled_at is not None
                         and as_utc(appointment.scheduled_at) <= now),
        followup_channel=appointment.followup_channel,
        followup_sent_at=appointment.followup_sent_at,
        customer_change=("Annulé par le client" if appointment.cancelled_by == "CLIENT"
                         else "Déplacé par le client" if appointment.rescheduled_by == "CLIENT" else None),
        whatsapp_reminder_sent_at=appointment.customer_whatsapp_reminder_sent_at,
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


# --- Lot 36 : issue du rendez-vous --------------------------------------------------------------

class OutcomeRequest(BaseModel):
    outcome: str


class OutcomeResult(BaseModel):
    appointment: AppointmentOut
    vehicle_unavailable: bool
    followup_channel: str | None


@router.post("/{appointment_id}/outcome", response_model=OutcomeResult, dependencies=[Depends(require_role("AGENT"))])
async def set_outcome(
    appointment_id: UUID,
    payload: OutcomeRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OutcomeResult:
    from app.services.appointment_outcome import OutcomeError, record_outcome

    tenant = await db.get(Tenant, current_user.tenant_id)
    appointment = await _get(db, current_user.tenant_id, appointment_id)
    now = datetime.now(timezone.utc)
    user_id = str(current_user.user_id)
    try:
        result = await record_outcome(db, tenant, appointment, payload.outcome, user_id, now=now)
    except OutcomeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await log_audit_event(
        db, actor=user_id, action="APPOINTMENT_OUTCOME", tenant_id=tenant.id,
        details={"appointment_id": str(appointment.id), "outcome": payload.outcome, **result},
    )
    await db.commit()
    return OutcomeResult(appointment=await _out(db, appointment, tenant_zone(tenant), now), **result)


# --- Lot 29 : créneaux proposés par Bob ---------------------------------------------------------

class BookingSettings(BaseModel):
    online_booking: bool = False
    opening_hours: dict = {}
    slot_minutes: int = 60
    capacity: int = 1


class BookingSettingsOut(BookingSettings):
    timezone: str
    preview: list[str] = []  # les prochains créneaux libres, tels que Bob les proposerait


async def _settings_out(db: AsyncSession, tenant) -> BookingSettingsOut:
    from app.services import booking

    settings = await booking.load_settings(db, tenant.id)
    zone = tenant_zone(tenant)
    if settings is None:
        return BookingSettingsOut(timezone=zone.key)
    preview = []
    if booking.booking_enabled(settings):
        slots = await booking.free_slots(db, tenant, settings, zone, datetime.now(timezone.utc))
        preview = [s["label"] for s in booking.slots_for_ai(slots, zone)]
    return BookingSettingsOut(
        online_booking=settings.online_booking, opening_hours=settings.opening_hours,
        slot_minutes=settings.slot_minutes, capacity=settings.capacity, timezone=zone.key, preview=preview,
    )


@router.get("/settings", response_model=BookingSettingsOut)
async def get_booking_settings(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BookingSettingsOut:
    return await _settings_out(db, await db.get(Tenant, current_user.tenant_id))


@router.put("/settings", response_model=BookingSettingsOut, dependencies=[Depends(require_role("MANAGER"))])
async def update_booking_settings(
    payload: BookingSettings,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BookingSettingsOut:
    from app.models.appointment_settings import TenantAppointmentSettings
    from app.services import booking

    try:
        hours = booking.validate_opening_hours(payload.opening_hours)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if payload.slot_minutes not in booking.SLOT_CHOICES:
        raise HTTPException(status_code=422, detail="Durée d'un rendez-vous : 30, 45, 60, 90 ou 120 minutes.")
    if not 1 <= payload.capacity <= booking.MAX_CAPACITY:
        raise HTTPException(status_code=422, detail=f"Rendez-vous en même temps : entre 1 et {booking.MAX_CAPACITY}.")
    if payload.online_booking and not hours:
        raise HTTPException(status_code=422, detail="Indiquez au moins un jour d'ouverture pour que Bob propose des créneaux.")

    tenant = await db.get(Tenant, current_user.tenant_id)
    settings = await booking.load_settings(db, tenant.id)
    if settings is None:
        settings = TenantAppointmentSettings(tenant_id=tenant.id)
        db.add(settings)
    settings.online_booking = payload.online_booking
    settings.opening_hours = hours
    settings.slot_minutes = payload.slot_minutes
    settings.capacity = payload.capacity
    await log_audit_event(
        db, actor=str(current_user.user_id), action="BOOKING_SETTINGS_UPDATED", tenant_id=tenant.id,
        details={"online_booking": payload.online_booking, "slot_minutes": payload.slot_minutes, "capacity": payload.capacity},
    )
    await db.commit()
    return await _settings_out(db, tenant)
