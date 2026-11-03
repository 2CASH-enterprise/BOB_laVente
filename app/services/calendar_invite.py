"""
Lot 102 — le rendez-vous dans l'agenda du conseiller (validé le 03/11).

Les e-mails de rendez-vous envoyés au conseiller (réservé par Bob, déplacé ou annulé par le client, confirmé ou annulé
depuis la page Rendez-vous) portent une invitation d'agenda standard (iCalendar, RFC 5545) : Gmail l'ajoute à l'agenda
Google en un toucher (ou tout seul selon ses réglages), comme Outlook et l'agenda de l'iPhone. Même identifiant pour
toute la vie du rendez-vous : un déplacement met l'événement à jour, une annulation le retire. Rien à connecter, aucun
coût ; jamais d'invitation envoyée au client (il a sa confirmation sur WhatsApp).
"""
from datetime import datetime, timedelta, timezone

DEFAULT_MINUTES = 60


def _stamp(moment: datetime) -> str:
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _text(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lignes de 75 octets au plus, la suite commence par une espace (RFC 5545, 3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


async def event_for(db, tenant, appointment, customer_name: str, link: str) -> dict | None:
    """L'événement d'un rendez-vous qui a une heure ; None sinon (une demande sans heure n'a rien à mettre à l'agenda)."""
    from app.services import booking
    from app.services.appointment_service import subject_phrase

    if appointment.scheduled_at is None:
        return None
    settings = await booking.load_settings(db, tenant.id)
    minutes = getattr(settings, "slot_minutes", None) or DEFAULT_MINUTES
    start = appointment.scheduled_at if appointment.scheduled_at.tzinfo else appointment.scheduled_at.replace(tzinfo=timezone.utc)
    cancelled = appointment.status == "CANCELLED"
    return {
        "uid": f"rdv-{appointment.id}@agenc-ai.com",
        "start": start,
        "end": start + timedelta(minutes=minutes),
        "summary": f"{subject_phrase(appointment)[:1].upper()}{subject_phrase(appointment)[1:]} — {customer_name}",
        "description": f"Rendez-vous pris avec Bob pour {tenant.name}.\nOuvrir la conversation : {link}",
        "location": (getattr(tenant, "pickup_address", None) or "").strip(),
        "cancelled": cancelled,
        "sequence": int(datetime.now(timezone.utc).timestamp()),  # toujours croissant : la dernière version l'emporte
    }


def ics(event: dict, attendee: str, organizer: str, now: datetime | None = None) -> str:
    """Le fichier .ics d'une invitation (REQUEST) ou d'une annulation (CANCEL) pour ce destinataire."""
    now = now or datetime.now(timezone.utc)
    method = "CANCEL" if event.get("cancelled") else "REQUEST"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AgenC'AI//Bob//FR", "CALSCALE:GREGORIAN", f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{event['uid']}",
        f"SEQUENCE:{event['sequence']}",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(event['start'])}",
        f"DTEND:{_stamp(event['end'])}",
        f"SUMMARY:{_text(event['summary'])}",
        f"DESCRIPTION:{_text(event['description'])}",
    ]
    if event.get("location"):
        lines.append(f"LOCATION:{_text(event['location'])}")
    lines += [
        f"ORGANIZER;CN=Bob:mailto:{organizer}",
        f"ATTENDEE;CN={_text(attendee)};ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:{attendee}",
        f"STATUS:{'CANCELLED' if method == 'CANCEL' else 'CONFIRMED'}",
        "TRANSP:OPAQUE",
    ]
    if method == "REQUEST":
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Rappel de rendez-vous", "TRIGGER:-PT1H", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
