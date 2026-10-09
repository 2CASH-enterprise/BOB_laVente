"""
Lot 57 — registre des réclamations et des sinistres du courtier (règlement CIMA 01-24, art. 11).

- Bob reconnaît une réclamation ou un sinistre (intention RECLAMATION) : le CODE crée la fiche (référence,
  heure de réception, premier message), une seule fois par client tant qu'elle est ouverte ; les messages
  suivants s'y rattachent. Le cabinet est prévenu par email ; le client reçoit un accusé de réception FIXE
  (référence, date et heure ; pour une réclamation, le délai de réponse en jours ouvrés choisi par le cabinet).
- Le cabinet peut aussi en saisir une à la main (téléphone, email, visite au cabinet).
- Suivi : Reçue → En cours → Traitée (réponse apportée obligatoire). Chaque étape est horodatée ; une
  réclamation non traitée après sa date limite est « en retard ». Rien n'est jamais supprimé.
- Le type (réclamation ou sinistre) est deviné par mots-clés, jamais par l'IA, et le cabinet peut le corriger.
"""
import csv
import io
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

RECLAMATION = "RECLAMATION"
SINISTRE = "SINISTRE"
KINDS = {RECLAMATION: "Réclamation", SINISTRE: "Sinistre"}
CHANNELS = {"WHATSAPP": "WhatsApp", "PHONE": "Téléphone", "EMAIL": "Email", "OFFICE": "Au cabinet", "OTHER": "Autre"}
RECEIVED = "RECEIVED"
IN_PROGRESS = "IN_PROGRESS"
RESOLVED = "RESOLVED"
STATUSES = {RECEIVED: "Reçue", IN_PROGRESS: "En cours", RESOLVED: "Traitée"}
OPEN = (RECEIVED, IN_PROGRESS)
DELAY_MIN, DELAY_MAX, DEFAULT_DELAY = 1, 60, 10
SUBJECT_MAX = 1000
RESOLUTION_MAX = 2000
LIST_LIMIT = 500

_SINISTRE_WORDS = re.compile(
    r"\b(sinistres?|accident\w*|accroch\w*|collision\w*|carambolage|choc|constat|vol|vole[es]?|volee?s?|cambriol\w*|"
    r"incendies?|brule\w*|feu|degats?|degat des eaux|inond\w*|fuite d.eau|bless\w*|deces|decede\w*|hospitalis\w*|"
    r"bris de glace|pare.brise|agress\w*|braqu\w*)\b")


class ComplaintError(ValueError):
    """Valeur ou étape refusée : le message est montré tel quel au cabinet."""


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return text.replace("'", " ").replace("’", " ")


def guess_kind(text: str) -> str:
    """Sinistre si le message en parle (accident, vol, incendie, dégât…), réclamation sinon."""
    return SINISTRE if _SINISTRE_WORDS.search(_plain(text)) else RECLAMATION


def add_business_days(start: date, days: int) -> date:
    """start + N jours ouvrés (du lundi au vendredi ; les jours fériés ne sont pas connus)."""
    current, left = start, days
    while left > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            left -= 1
    return current


def local_now(tenant, now: datetime | None = None) -> datetime:
    from app.services.local_time import tenant_zone

    return (now or datetime.now(timezone.utc)).astimezone(tenant_zone(tenant))


def due_date(tenant, kind: str, received_at: datetime) -> date | None:
    if kind != RECLAMATION:
        return None  # un sinistre suit les délais de l'assureur : aucun délai de réponse annoncé
    delay = tenant.complaint_delay_days or DEFAULT_DELAY
    return add_business_days(local_now(tenant, received_at).date(), delay)


def is_overdue(complaint, today: date) -> bool:
    return complaint.status in OPEN and complaint.due_on is not None and complaint.due_on < today


async def _next_reference(db, tenant, year: int) -> str:
    from app.models.insurance_complaint import InsuranceComplaint

    prefix = f"R-{year}-"
    taken = (await db.execute(select(func.count(InsuranceComplaint.id)).where(
        InsuranceComplaint.tenant_id == tenant.id, InsuranceComplaint.reference.like(prefix + "%")))).scalar_one()
    number = taken + 1
    while (await db.execute(select(InsuranceComplaint.id).where(
            InsuranceComplaint.tenant_id == tenant.id, InsuranceComplaint.reference == f"{prefix}{number:04d}"))).first():
        number += 1  # une référence n'est jamais réutilisée
    return f"{prefix}{number:04d}"


def _subject(text: str) -> str:
    cleaned = (text or "").strip()[:SUBJECT_MAX]
    if not cleaned:
        raise ComplaintError("Décrivez la réclamation en quelques mots")
    return cleaned


async def open_complaint(db, tenant_id, customer_id):
    from app.models.insurance_complaint import InsuranceComplaint

    return (await db.execute(select(InsuranceComplaint).where(
        InsuranceComplaint.tenant_id == tenant_id, InsuranceComplaint.customer_id == customer_id,
        InsuranceComplaint.status.in_(OPEN),
    ).order_by(InsuranceComplaint.received_at.desc()).limit(1))).scalar_one_or_none()


async def record_from_whatsapp(db, tenant, customer, conversation, text: str, now: datetime | None = None):
    """
    Le client vient d'écrire une réclamation ou de parler d'un sinistre. Renvoie (fiche, nouvelle ?) : une
    nouvelle fiche si aucune n'est ouverte pour ce client, sinon le message est rattaché à la fiche ouverte.
    """
    from app.models.insurance_complaint import InsuranceComplaint

    now = now or datetime.now(timezone.utc)
    current = await open_complaint(db, tenant.id, customer.id)
    if current is not None:
        current.follow_ups += 1  # le type reste celui du premier message : le cabinet le corrige s'il le faut
        current.last_message_at = now
        await db.flush()
        return current, False
    kind = guess_kind(text)
    complaint = InsuranceComplaint(
        tenant_id=tenant.id, customer_id=customer.id, conversation_id=conversation.id if conversation else None,
        reference=await _next_reference(db, tenant, local_now(tenant, now).year), kind=kind, channel="WHATSAPP",
        subject=(text or "").strip()[:SUBJECT_MAX] or "(message vide)", status=RECEIVED, received_at=now,
        due_on=due_date(tenant, kind, now), last_message_at=now, created_by="BOB",
    )
    db.add(complaint)
    await db.flush()
    return complaint, True


async def create_manual(db, tenant, data: dict, user_id, now: datetime | None = None):
    """Saisie par le cabinet (téléphone, email, visite) : le client est retrouvé par son téléphone ou créé."""
    from app.models.insurance_complaint import InsuranceComplaint
    from app.services.insurance_contracts import ContractError, find_or_create_customer

    channel = data.get("channel") or "PHONE"
    if channel not in CHANNELS:
        raise ComplaintError("Canal inconnu : " + ", ".join(CHANNELS.values()))
    kind = data.get("kind") or RECLAMATION
    if kind not in KINDS:
        raise ComplaintError("Type inconnu : réclamation ou sinistre")
    subject = _subject(data.get("subject"))
    now = now or datetime.now(timezone.utc)
    received_at = now
    if data.get("received_on"):
        try:
            day = date.fromisoformat(str(data["received_on"])[:10])
        except ValueError:
            raise ComplaintError("Date de réception invalide") from None
        today = local_now(tenant, now).date()
        if day > today or day < today - timedelta(days=365):
            raise ComplaintError("La date de réception doit être aujourd'hui ou dans l'année écoulée")
        if day != today:
            from app.services.local_time import local_to_utc, tenant_zone

            received_at = local_to_utc(datetime.combine(day, datetime.min.time()).replace(hour=12), tenant_zone(tenant))
    try:
        if data.get("customer_id"):
            from app.models.customer import Customer

            customer = await db.get(Customer, data["customer_id"])
            if customer is None or customer.tenant_id != tenant.id:
                raise ComplaintError("Client introuvable")
        else:
            customer, _ = await find_or_create_customer(db, tenant, data.get("phone"), data.get("first_name"),
                                                        data.get("last_name"), data.get("email"), now)
    except ContractError as exc:
        raise ComplaintError(str(exc)) from None
    complaint = InsuranceComplaint(
        tenant_id=tenant.id, customer_id=customer.id, reference=await _next_reference(db, tenant, local_now(tenant, received_at).year),
        kind=kind, channel=channel, subject=subject, status=RECEIVED, received_at=received_at,
        due_on=due_date(tenant, kind, received_at), last_message_at=received_at, created_by=str(user_id),
    )
    db.add(complaint)
    await db.flush()
    return complaint


def start(complaint, user_id, now: datetime | None = None) -> None:
    if complaint.status != RECEIVED:
        raise ComplaintError("Cette réclamation est déjà prise en charge")
    complaint.status = IN_PROGRESS
    complaint.started_at = now or datetime.now(timezone.utc)
    complaint.started_by = str(user_id)


def resolve(complaint, user_id, resolution: str, now: datetime | None = None) -> None:
    """Traitée : la réponse apportée au client est obligatoire (traçabilité)."""
    if complaint.status == RESOLVED:
        raise ComplaintError("Cette réclamation est déjà traitée")
    text = (resolution or "").strip()
    if len(text) < 5:
        raise ComplaintError("Indiquez la réponse apportée au client")
    now = now or datetime.now(timezone.utc)
    if complaint.started_at is None:
        complaint.started_at = now
        complaint.started_by = str(user_id)
    complaint.status = RESOLVED
    complaint.resolved_at = now
    complaint.resolved_by = str(user_id)
    complaint.resolution = text[:RESOLUTION_MAX]


def change_kind(tenant, complaint, kind: str) -> None:
    """Corriger le type : la date limite suit (une réclamation en a une, un sinistre non)."""
    if kind not in KINDS:
        raise ComplaintError("Type inconnu : réclamation ou sinistre")
    if kind != complaint.kind:
        complaint.kind = kind
        complaint.due_on = due_date(tenant, kind, complaint.received_at)


# --- Messages -------------------------------------------------------------------------------------

def _when(tenant, moment: datetime) -> str:
    local = local_now(tenant, moment)
    return f"{local.strftime('%d/%m/%Y')} à {local.hour} h {local.minute:02d}"


def acknowledgement(tenant, complaint) -> str:
    """Accusé de réception FIXE (écrit par le code), au vouvoiement, sans promesse de prise en charge."""
    when = _when(tenant, complaint.received_at)
    if complaint.kind == SINISTRE:
        text = (f"Votre déclaration est bien enregistrée sous la référence {complaint.reference}, le {when}. "
                "Un conseiller du cabinet va reprendre contact avec vous pour la suite.")
    else:
        delay = tenant.complaint_delay_days or DEFAULT_DELAY
        text = (f"Votre réclamation est bien enregistrée sous la référence {complaint.reference}, le {when}. "
                f"Le cabinet s'engage à vous répondre sous {delay} jour{'s' if delay > 1 else ''} ouvré"
                f"{'s' if delay > 1 else ''}.")
    contact = (getattr(tenant, "complaints_contact", None) or "").strip()
    if contact:
        text += f" Contact réclamations : {contact}."
    return text


def alert_email(tenant, customer, complaint, link: str | None) -> tuple[str, str]:
    from app.services.handoff_service import customer_display_name

    who = customer_display_name(customer)
    label = "Déclaration de sinistre" if complaint.kind == SINISTRE else "Nouvelle réclamation"
    subject = f"{label} {complaint.reference} — {who}"
    lines = [
        "Bonjour,",
        "",
        f"Bob a enregistré {'une déclaration de sinistre' if complaint.kind == SINISTRE else 'une réclamation'} pour {tenant.name}.",
        "",
        f"Référence : {complaint.reference}",
        f"Client : {who}",
        f"Reçue le : {_when(tenant, complaint.received_at)}",
        *([f"À traiter avant le : {complaint.due_on.strftime('%d/%m/%Y')}"] if complaint.due_on else []),
        f"Canal : {CHANNELS.get(complaint.channel, complaint.channel)}",
        "",
        "Message du client :",
        complaint.subject,
        "",
        "Bob a transmis la conversation au cabinet et envoyé au client un accusé de réception avec cette référence. "
        "Il n'a rien promis sur le fond.",
    ]
    if link:
        lines += ["", f"Voir la conversation : {link}"]
    lines += ["", "— Bob"]
    return subject, "\n".join(lines)


# --- Affichage, compteurs, export -----------------------------------------------------------------

def serialize(complaint, customer, today: date) -> dict:
    from app.services.handoff_service import customer_display_name

    return {
        "id": str(complaint.id),
        "reference": complaint.reference,
        "customer_id": str(complaint.customer_id),
        "customer": customer_display_name(customer) if customer else "Client",
        "conversation_id": str(complaint.conversation_id) if complaint.conversation_id else None,
        "kind": complaint.kind,
        "kind_label": KINDS.get(complaint.kind, complaint.kind),
        "channel": complaint.channel,
        "channel_label": CHANNELS.get(complaint.channel, complaint.channel),
        "subject": complaint.subject,
        "status": complaint.status,
        "status_label": STATUSES.get(complaint.status, complaint.status),
        "received_at": complaint.received_at,
        "due_on": complaint.due_on.isoformat() if complaint.due_on else None,
        "overdue": is_overdue(complaint, today),
        "days_left": (complaint.due_on - today).days if complaint.due_on and complaint.status in OPEN else None,
        "acknowledged_at": complaint.acknowledged_at,
        "follow_ups": complaint.follow_ups,
        "started_at": complaint.started_at,
        "resolved_at": complaint.resolved_at,
        "resolution": complaint.resolution,
        "created_by_bob": complaint.created_by == "BOB",
    }


VIEWS = {"todo": (RECEIVED,), "progress": (IN_PROGRESS,), "resolved": (RESOLVED,), "all": (RECEIVED, IN_PROGRESS, RESOLVED)}


async def list_complaints(db, tenant, view: str = "todo", now: datetime | None = None):
    from app.models.customer import Customer
    from app.models.insurance_complaint import InsuranceComplaint

    query = select(InsuranceComplaint, Customer).join(Customer, Customer.id == InsuranceComplaint.customer_id).where(
        InsuranceComplaint.tenant_id == tenant.id, Customer.tenant_id == tenant.id)
    if view == "late":
        query = query.where(InsuranceComplaint.status.in_(OPEN), InsuranceComplaint.due_on.is_not(None),
                            InsuranceComplaint.due_on < local_now(tenant, now).date())
    else:
        query = query.where(InsuranceComplaint.status.in_(VIEWS[view]))
    order = InsuranceComplaint.received_at.desc() if view in ("resolved", "all") else InsuranceComplaint.received_at
    return (await db.execute(query.order_by(order).limit(LIST_LIMIT))).all(), local_now(tenant, now).date()


async def counts(db, tenant, now: datetime | None = None) -> dict:
    from app.models.insurance_complaint import InsuranceComplaint

    today = local_now(tenant, now).date()
    rows = (await db.execute(select(InsuranceComplaint).where(
        InsuranceComplaint.tenant_id == tenant.id, InsuranceComplaint.status.in_(OPEN)))).scalars().all()
    return {"todo": sum(1 for c in rows if c.status == RECEIVED), "progress": sum(1 for c in rows if c.status == IN_PROGRESS),
            "late": sum(1 for c in rows if is_overdue(c, today))}


async def to_handle_count(db, tenant, now: datetime | None = None) -> int:
    """Pastille : réclamations pas encore prises en charge, plus celles en cours mais en retard."""
    figures = await counts(db, tenant, now)
    from app.models.insurance_complaint import InsuranceComplaint

    today = local_now(tenant, now).date()
    late_in_progress = (await db.execute(select(func.count(InsuranceComplaint.id)).where(
        InsuranceComplaint.tenant_id == tenant.id, InsuranceComplaint.status == IN_PROGRESS,
        InsuranceComplaint.due_on.is_not(None), InsuranceComplaint.due_on < today))).scalar_one()
    return figures["todo"] + late_in_progress


def _csv_when(tenant, moment) -> str:
    return local_now(tenant, moment).strftime("%Y-%m-%d %H:%M") if moment else ""


async def export_csv(db, tenant) -> str:
    """Registre complet (pour un contrôle) : une ligne par réclamation, toutes les étapes horodatées."""
    from app.models.customer import Customer
    from app.models.insurance_complaint import InsuranceComplaint
    from app.services.handoff_service import customer_display_name

    rows = (await db.execute(select(InsuranceComplaint, Customer).join(Customer, Customer.id == InsuranceComplaint.customer_id)
                             .where(InsuranceComplaint.tenant_id == tenant.id, Customer.tenant_id == tenant.id)
                             .order_by(InsuranceComplaint.received_at))).all()
    today = local_now(tenant).date()
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Référence", "Type", "Canal", "Client", "Téléphone", "Reçue le", "Accusé de réception",
                     "Date limite", "Statut", "En retard", "Prise en charge le", "Traitée le", "Réponse apportée",
                     "Message du client", "Messages suivants"])
    for c, customer in rows:
        writer.writerow([
            c.reference, KINDS.get(c.kind, c.kind), CHANNELS.get(c.channel, c.channel), customer_display_name(customer),
            f"+{customer.whatsapp_number}" if customer.whatsapp_number else "", _csv_when(tenant, c.received_at),
            _csv_when(tenant, c.acknowledged_at), c.due_on.isoformat() if c.due_on else "", STATUSES.get(c.status, c.status),
            "oui" if is_overdue(c, today) else "non", _csv_when(tenant, c.started_at), _csv_when(tenant, c.resolved_at),
            c.resolution or "", c.subject, c.follow_ups,
        ])
    return out.getvalue()
