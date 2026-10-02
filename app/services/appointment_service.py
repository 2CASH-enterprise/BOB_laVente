"""
Rendez-vous de concession (lot 25) : confirmation par un humain, messages fixes au client,
rappels par email.

Règles :
- Bob ne confirme jamais un rendez-vous ; seul un conseiller le fait, avec une date et une heure
  saisies dans l'heure locale de la boutique.
- Les messages au client sont FIXES (jamais rédigés par l'IA), envoyés seulement dans les 20 h
  qui suivent le dernier message du client (lot 23b) et enregistrés seulement si Meta les accepte.
- Rappel au conseiller la veille à partir de 18 h (heure de la boutique) ; au client par email
  seulement si son adresse est connue. Un rappel n'est marqué envoyé que si l'email est parti.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment_request import (
    APPOINTMENT_KINDS,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    AppointmentRequest,
)
from app.services.local_time import as_utc, format_local

REMINDER_HOUR = 18  # la veille, heure locale de la boutique


def subject_phrase(appointment: AppointmentRequest) -> str:
    """« essai (Peugeot 3008) », « visite », « estimation de reprise »."""
    label = APPOINTMENT_KINDS.get(appointment.kind, appointment.kind).lower()
    return f"{label} ({appointment.vehicle_label})" if appointment.vehicle_label else label


def confirmation_message(appointment: AppointmentRequest, shop_name: str, zone) -> str:
    when = format_local(appointment.scheduled_at, zone)
    if appointment.kind == "ESSAI":
        what = f"Votre essai{f' ({appointment.vehicle_label})' if appointment.vehicle_label else ''} est confirmé"
    elif appointment.kind == "VISITE":
        what = f"Votre visite{f' ({appointment.vehicle_label})' if appointment.vehicle_label else ''} est confirmée"
    else:
        what = "Votre rendez-vous d'estimation de reprise est confirmé"
    return f"Bonjour ! {what} le {when}. À bientôt chez {shop_name} !"


def cancellation_message(appointment: AppointmentRequest, shop_name: str, zone) -> str:
    if appointment.scheduled_at is not None:
        what = f"votre rendez-vous du {format_local(appointment.scheduled_at, zone)} est annulé"
    else:
        what = "votre demande de rendez-vous est annulée"
    return f"Bonjour, {what}. N'hésitez pas à nous écrire pour en fixer un autre. {shop_name}"


@dataclass
class ReminderDue:
    staff: bool
    customer: bool
    whatsapp: bool = False  # lot 35 : tenté seulement si la conversation WhatsApp est ouverte


def reminder_due(appointment: AppointmentRequest, now: datetime, zone, customer_email: str | None) -> ReminderDue:
    """La veille du rendez-vous (heure de la boutique), à partir de 18 h."""
    if appointment.status != STATUS_CONFIRMED or appointment.scheduled_at is None:
        return ReminderDue(False, False, False)
    local_now = as_utc(now).astimezone(zone)
    local_when = as_utc(appointment.scheduled_at).astimezone(zone)
    is_eve = local_when.date() == local_now.date() + timedelta(days=1) and local_now.hour >= REMINDER_HOUR
    return ReminderDue(
        staff=is_eve and appointment.reminder_sent_at is None,
        customer=is_eve and bool(customer_email) and appointment.customer_reminder_sent_at is None,
        whatsapp=is_eve and appointment.customer_whatsapp_reminder_sent_at is None,
    )


def whatsapp_reminder_message(appointment: AppointmentRequest, shop_name: str, zone) -> str:
    """Lot 35 — message fixe (jamais rédigé par l'IA), la veille du rendez-vous."""
    when = format_local(appointment.scheduled_at, zone)
    return (f"Bonjour ! Petit rappel : votre {subject_phrase(appointment)} chez {shop_name} est prévu demain, "
            f"{when}. Pour le déplacer ou l'annuler, répondez simplement à ce message.")


def rescheduled_message(appointment: AppointmentRequest, shop_name: str, zone) -> str:
    return f"C'est noté : votre {subject_phrase(appointment)} est déplacé au {format_local(appointment.scheduled_at, zone)}. À bientôt chez {shop_name} !"


def staff_change_email(appointment: AppointmentRequest, customer_name: str, zone, link: str,
                       previous_when: datetime | None) -> tuple[str, str]:
    """Lot 35 — le prospect a déplacé ou annulé lui-même son rendez-vous en écrivant à Bob."""
    before = format_local(previous_when, zone) if previous_when else None
    if appointment.status == STATUS_CANCELLED:
        subject = f"Rendez-vous annulé par le client : {customer_name}"
        what = f"{customer_name} a annulé son rendez-vous ({subject_phrase(appointment)}{', ' + before if before else ''})."
    else:
        subject = f"Rendez-vous déplacé par le client : {customer_name}"
        what = (f"{customer_name} a déplacé son rendez-vous ({subject_phrase(appointment)}) au "
                f"{format_local(appointment.scheduled_at, zone)}{' (au lieu du ' + before + ')' if before else ''}.")
    body = f"Bonjour,\n\n{what}\n\nOuvrir la conversation : {link}"
    return subject, body


def staff_reminder_email(appointment: AppointmentRequest, customer_name: str, zone, link: str,
                         fiche: dict | None = None) -> tuple[str, str]:
    when = format_local(appointment.scheduled_at, zone)
    lines = [
        "Bonjour,",
        "",
        f"Rappel : {subject_phrase(appointment)} avec {customer_name}, {when}.",
        "",
    ]
    qualification = qualification_lines(appointment)
    if fiche:  # lot 43 : la fiche prospect complète, avec son score
        from app.services.prospect import fiche_block

        lines.append(fiche_block(fiche).rstrip("\n"))
        lines.append("")
    elif qualification:
        lines += ["Ce que Bob a appris :", *[f"- {q}" for q in qualification], ""]
    lines.append(f"Ouvrir la conversation : {link}")
    return f"Rappel : rendez-vous demain avec {customer_name}", "\n".join(lines)


def booking_alert_email(appointment: AppointmentRequest, customer_name: str, zone, link: str,
                        fiche: dict | None = None) -> tuple[str, str]:
    """Lot 29 — Bob a réservé un créneau : la boutique (et le commercial) sont prévenus."""
    when = format_local(appointment.scheduled_at, zone)
    lines = [
        "Bonjour,",
        "",
        f"Bob a fixé un rendez-vous : {subject_phrase(appointment)} avec {customer_name}, {when}.",
        "Le client a choisi ce créneau parmi vos créneaux libres ; il a reçu la confirmation sur WhatsApp.",
        "Pour changer l'heure ou annuler, ouvrez la page Rendez-vous (le client peut être prévenu).",
        "",
    ]
    qualification = qualification_lines(appointment)
    if fiche:  # lot 43
        from app.services.prospect import fiche_block

        lines.append(fiche_block(fiche).rstrip("\n"))
        lines.append("")
    elif qualification:
        lines += ["Ce que Bob a appris :", *[f"- {q}" for q in qualification], ""]
    lines.append(f"Ouvrir la conversation : {link}")
    return f"Nouveau rendez-vous : {customer_name}, {when}", "\n".join(lines)


def customer_reminder_email(appointment: AppointmentRequest, shop_name: str, zone, powered_by: bool = False) -> tuple[str, str]:
    from app.services.email_layout import customer_footer

    when = format_local(appointment.scheduled_at, zone)
    body = (
        "Bonjour,\n\n"
        f"Nous vous rappelons votre rendez-vous chez {shop_name} :\n\n"
        f"Rendez-vous : {subject_phrase(appointment)}\n"
        f"Date : {when}\n\n"
        "Un empêchement ? Écrivez-nous simplement sur WhatsApp ou répondez à cet email.\n\n"
        f"À demain,\n{shop_name}"
        + customer_footer(shop_name, powered_by)
    )
    return f"Rappel de votre rendez-vous chez {shop_name}", body


def qualification_lines(appointment: AppointmentRequest) -> list[str]:
    lines = []
    if appointment.need:
        lines.append(f"Besoin : {appointment.need}")
    if appointment.budget:
        lines.append(f"Budget : {appointment.budget}")
    if appointment.trade_in:
        lines.append(f"Reprise : {appointment.trade_in}")
    if appointment.financing_interest is True:
        lines.append("Financement : intéressé")
    elif appointment.financing_interest is False:
        lines.append("Financement : non")
    if appointment.notes:
        lines.append(f"Notes : {appointment.notes}")
    return lines


def confirm(appointment: AppointmentRequest, scheduled_at_utc: datetime, user_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    rescheduled = appointment.status == STATUS_CONFIRMED and appointment.scheduled_at != scheduled_at_utc
    appointment.status = STATUS_CONFIRMED
    appointment.scheduled_at = scheduled_at_utc
    appointment.confirmed_at = now
    appointment.confirmed_by = user_id
    appointment.cancelled_at = None
    if rescheduled:
        # Nouvelle date : les rappels de l'ancienne ne comptent plus.
        appointment.reminder_sent_at = None
        appointment.customer_reminder_sent_at = None


def cancel(appointment: AppointmentRequest, now: datetime | None = None) -> None:
    appointment.status = STATUS_CANCELLED
    appointment.cancelled_at = now or datetime.now(timezone.utc)


async def send_fixed_message(
    db: AsyncSession, tenant, conversation, customer, text: str, user_id: str, message_type: str,
) -> tuple[bool, str | None]:
    """
    Envoie un message fixe au client. Renvoie (envoyé, raison si non envoyé). Mêmes garde-fous que
    la réponse humaine du lot 23b : fenêtre de 20 h ouverte par le client, garde-fou d'envoi
    (kill switch, journal), message enregistré seulement si Meta l'accepte.
    """
    from sqlalchemy import select

    from app.integrations.whatsapp.client import WhatsAppClient, WhatsAppSendError
    from app.models.conversation import Message, MessageSender
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.human_reply import HumanReplyError, ensure_reply_window_open
    from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound

    try:
        await ensure_reply_window_open(db, conversation)
    except HumanReplyError:
        return False, ("Le client ne vous a pas écrit depuis plus de 20 h : WhatsApp ne permet pas de le "
                       "prévenir maintenant. Appelez-le, ou attendez qu'il vous réécrive.")
    account = (await db.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id)
    )).scalar_one_or_none()
    if account is None:
        return False, "Aucun compte WhatsApp n'est connecté à votre boutique."
    try:
        # Message fixe déclenché par un humain : autorisé même en mode « IA uniquement »,
        # mais jamais contre le kill switch de la plateforme.
        await check_and_log_outbound(db, tenant_id=tenant.id, requested_by=user_id,
                                     permission=Permission.CAN_REPLY_TO_CUSTOMER, human_in_control=True)
    except OutboundDenied as exc:
        return False, exc.reason
    try:
        client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
        await client.send_text_message(to=customer.whatsapp_number, body=text)
    except WhatsAppSendError as exc:
        return False, f"WhatsApp a refusé le message : {exc.details}"
    except Exception:  # noqa: BLE001 — réseau, délai : jamais de faux « envoyé »
        return False, "Envoi impossible pour le moment."
    db.add(Message(
        tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
        message_type=message_type, content=text, message_metadata={"sent_by": user_id},
    ))
    return True, None
