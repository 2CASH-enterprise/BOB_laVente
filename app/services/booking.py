"""
Lot 29 — créneaux de rendez-vous proposés par Bob (concession).

Le CODE calcule les créneaux libres à partir des horaires d'ouverture, de la durée d'un
rendez-vous et du nombre de clients reçus en même temps ; Bob ne fait que les proposer. Au moment
de réserver, le créneau est revérifié (verrou par boutique) : un créneau inventé, fermé, passé ou
déjà plein n'est jamais réservé.

Toutes les heures sont celles du pays de la boutique (services/local_time.py).
"""
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.models.appointment_request import STATUS_CONFIRMED, AppointmentRequest
from app.models.appointment_settings import TenantAppointmentSettings
from app.services.local_time import as_utc, format_local

SLOT_CHOICES = (30, 45, 60, 90, 120)
MAX_CAPACITY = 20
MAX_RANGES_PER_DAY = 2
HORIZON_DAYS = 7  # aujourd'hui + 7 jours
MIN_NOTICE = timedelta(hours=2)  # jamais un créneau qui commence dans moins de 2 h
MAX_PROPOSED = 3
PARTS_OF_DAY = {"MATIN": (0, 12 * 60), "APRES_MIDI": (12 * 60, 18 * 60), "SOIR": (18 * 60, 24 * 60)}
DAY_NAMES = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _minutes(value: str) -> int:
    match = _TIME.match(value or "")
    if not match:
        raise ValueError(f"Heure invalide : {value} (format HH:MM)")
    return int(match.group(1)) * 60 + int(match.group(2))


def validate_opening_hours(raw) -> dict:
    """{"0": [["09:00", "12:30"], ["14:00", "18:00"]], ...} → même forme, vérifiée et triée."""
    if not isinstance(raw, dict):
        raise ValueError("Horaires invalides")
    result = {}
    for day, ranges in raw.items():
        if str(day) not in {str(i) for i in range(7)}:
            raise ValueError(f"Jour invalide : {day}")
        if not ranges:
            continue
        if not isinstance(ranges, list) or len(ranges) > MAX_RANGES_PER_DAY:
            raise ValueError(f"{DAY_NAMES[int(day)]} : au plus {MAX_RANGES_PER_DAY} plages horaires")
        cleaned = []
        for item in ranges:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError(f"{DAY_NAMES[int(day)]} : plage invalide")
            start, end = _minutes(item[0]), _minutes(item[1])
            if start >= end:
                raise ValueError(f"{DAY_NAMES[int(day)]} : l'heure de fin doit être après l'heure de début")
            cleaned.append((start, end, item[0], item[1]))
        cleaned.sort()
        for previous, current in zip(cleaned, cleaned[1:]):
            if current[0] < previous[1]:
                raise ValueError(f"{DAY_NAMES[int(day)]} : les plages horaires se chevauchent")
        result[str(day)] = [[c[2], c[3]] for c in cleaned]
    return result


async def load_settings(db, tenant_id, lock: bool = False) -> TenantAppointmentSettings | None:
    stmt = select(TenantAppointmentSettings).where(TenantAppointmentSettings.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update()  # deux clients ne réservent jamais la dernière place en même temps
    return (await db.execute(stmt)).scalar_one_or_none()


def booking_enabled(settings: TenantAppointmentSettings | None) -> bool:
    return bool(settings and settings.online_booking and any(settings.opening_hours.values()))


def _starts_of_day(settings, zone, day: date) -> list[datetime]:
    starts = []
    for start_txt, end_txt in settings.opening_hours.get(str(day.weekday())) or []:
        start, end = _minutes(start_txt), _minutes(end_txt)
        minute = start
        while minute + settings.slot_minutes <= end:
            starts.append(datetime.combine(day, time(minute // 60, minute % 60), tzinfo=zone))
            minute += settings.slot_minutes
    return starts


def _is_free(start: datetime, settings, booked: list[datetime]) -> bool:
    length = timedelta(minutes=settings.slot_minutes)
    start_utc = as_utc(start)
    overlapping = sum(1 for b in booked if b < start_utc + length and start_utc < b + length)
    return overlapping < settings.capacity


async def _booked(db, tenant_id, now: datetime, exclude_id=None) -> list[datetime]:
    stmt = select(AppointmentRequest.scheduled_at).where(
        AppointmentRequest.tenant_id == tenant_id,
        AppointmentRequest.status == STATUS_CONFIRMED,
        AppointmentRequest.scheduled_at.is_not(None),
        AppointmentRequest.scheduled_at >= now - timedelta(hours=4),
    )
    if exclude_id is not None:  # lot 35 : un rendez-vous déplacé ne se bloque pas lui-même
        stmt = stmt.where(AppointmentRequest.id != exclude_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [as_utc(r) for r in rows]


def _candidates(settings, zone, now: datetime, day: date | None = None, part: str | None = None) -> list[datetime]:
    today = as_utc(now).astimezone(zone).date()
    earliest = as_utc(now) + MIN_NOTICE
    days = [today + timedelta(days=i) for i in range(HORIZON_DAYS + 1)]
    if day is not None:
        days = [d for d in days if d == day]
    result = []
    for d in days:
        for start in _starts_of_day(settings, zone, d):
            if as_utc(start) < earliest:
                continue
            if part is not None:
                low, high = PARTS_OF_DAY[part]
                if not low <= start.hour * 60 + start.minute < high:
                    continue
            result.append(start)
    return result


def _spread(slots: list[datetime], limit: int) -> list[datetime]:
    """Sans préférence du client : le premier créneau de plusieurs jours différents."""
    chosen, seen_days = [], set()
    for slot in slots:
        if slot.date() not in seen_days:
            chosen.append(slot)
            seen_days.add(slot.date())
        if len(chosen) == limit:
            return chosen
    for slot in slots:  # moins de jours que de créneaux voulus : on complète dans l'ordre
        if slot not in chosen:
            chosen.append(slot)
        if len(chosen) == limit:
            break
    return sorted(chosen)


async def free_slots(db, tenant, settings, zone, now: datetime, day: date | None = None,
                     part: str | None = None, limit: int = MAX_PROPOSED) -> list[datetime]:
    booked = await _booked(db, tenant.id, now)
    free = [s for s in _candidates(settings, zone, now, day, part) if _is_free(s, settings, booked)]
    if day is not None or part is not None:
        return free[:limit]
    return _spread(free, limit)


async def is_bookable(db, tenant, settings, zone, now: datetime, start: datetime, exclude_id=None) -> bool:
    """Revérifié au moment de réserver : horaires, alignement, délai, horizon et places libres."""
    if start not in _candidates(settings, zone, now, day=start.date()):
        return False
    return _is_free(start, settings, await _booked(db, tenant.id, now, exclude_id))


def slot_id(start: datetime) -> str:
    return start.strftime("%Y-%m-%dT%H:%M")


def parse_slot_id(value, zone) -> datetime:
    """« 2026-10-03T10:00 » (heure de la boutique) → datetime locale ; ValueError sinon."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", value):
        raise ValueError("Créneau invalide")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=zone)


def slots_for_ai(slots: list[datetime], zone) -> list[dict]:
    return [{"slot": slot_id(s), "label": format_local(s.astimezone(timezone.utc), zone)} for s in slots]


def today_for_ai(now: datetime, zone) -> dict:
    local = as_utc(now).astimezone(zone)
    from app.services.local_time import _MONTHS

    return {"today": f"{DAY_NAMES[local.weekday()].lower()} {local.day} {_MONTHS[local.month - 1]} {local.year} "
                     f"({local.date().isoformat()})"}
