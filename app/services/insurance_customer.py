"""
Lot 64 — fiche client du courtier : ses contrats (avec les actions de la page « Contrats »), quatre chiffres
(contrats en cours, prochaine échéance, cotations, réclamations ouvertes) et un historique court (cotations,
rendez-vous et appels, réclamations et sinistres), chaque ligne menant au bon endroit de la page concernée.
La prime n'est montrée qu'aux administrateurs (règle du lot 55).
"""
from datetime import datetime, timezone

from sqlalchemy import select

from app.services import insurance
from app.services import insurance_complaints as icp
from app.services import insurance_contracts as ict

# Vue de la page à ouvrir pour retrouver une ligne (mêmes vues que les boutons de chaque page).
QUOTE_VIEWS = {insurance.STATUS_DRAFT: "draft", insurance.STATUS_SUBMITTED: "todo", insurance.STATUS_HANDLED: "handled",
               insurance.STATUS_PROPOSAL: "proposal", insurance.STATUS_WON: "closed", insurance.STATUS_LOST: "closed"}
QUOTE_OPEN = (insurance.STATUS_SUBMITTED, insurance.STATUS_HANDLED, insurance.STATUS_PROPOSAL)
COMPLAINT_VIEWS = {icp.RECEIVED: "todo", icp.IN_PROGRESS: "progress", icp.RESOLVED: "resolved"}
APPOINTMENT_KINDS = {"APPEL": "Appel", "CABINET": "Rendez-vous au cabinet"}
OUTCOMES = {"SOLD": "contrat souscrit", "FOLLOW_UP": "à relancer", "NOT_INTERESTED": "pas intéressé",
            "NO_SHOW": "absent / injoignable"}
HISTORY_LIMIT = 20


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value else None


def contract_order(contract, today) -> tuple:
    """Contrats en cours d'abord (échéance la plus proche en tête), puis les autres (les plus récents en tête)."""
    if contract.status == ict.ACTIVE:
        return (0, contract.expires_on.toordinal())
    return (1, -contract.expires_on.toordinal())


def appointment_line(a, now: datetime) -> dict:
    from app.models.appointment_request import STATUS_CANCELLED, STATUS_CONFIRMED, STATUS_REQUESTED

    scheduled = _utc(a.scheduled_at)
    upcoming = scheduled is not None and scheduled >= now
    if a.status == STATUS_REQUESTED:
        status, view = "À confirmer", "pending"
    elif a.status == STATUS_CONFIRMED and upcoming:
        status, view = "Prévu", "confirmed"
    elif a.status == STATUS_CANCELLED:
        status, view = "Annulé", "closed"
    else:
        status, view = "Passé", "closed"
    if a.outcome:
        status += " · " + OUTCOMES.get(a.outcome, a.outcome)
    return {"id": str(a.id), "label": APPOINTMENT_KINDS.get(a.kind, "Rendez-vous"), "status_label": status,
            "at": _iso(scheduled or a.created_at), "scheduled": scheduled is not None, "view": view}


async def overview(db, tenant, customer, can_see_premium: bool, now: datetime | None = None) -> dict:
    from app.models.appointment_request import AppointmentRequest
    from app.models.conversation import Conversation
    from app.models.insurance_complaint import InsuranceComplaint
    from app.models.insurance_contract import InsuranceContract
    from app.models.quote_request import QuoteRequest

    now = _utc(now or datetime.now(timezone.utc))
    today = ict.today_for(tenant, now)
    conversation_id = (await db.execute(select(Conversation.id).where(
        Conversation.tenant_id == tenant.id, Conversation.customer_id == customer.id,
    ).order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()

    contracts = (await db.execute(select(InsuranceContract).where(
        InsuranceContract.tenant_id == tenant.id, InsuranceContract.customer_id == customer.id))).scalars().all()
    contracts = sorted(contracts, key=lambda c: contract_order(c, today))
    active = [c for c in contracts if c.status == ict.ACTIVE]
    upcoming = [c for c in active if ict.days_left(c, today) >= -ict.EXPIRED_GRACE_DAYS]
    nearest = upcoming[0] if upcoming else None

    quotes = (await db.execute(select(QuoteRequest).where(
        QuoteRequest.tenant_id == tenant.id, QuoteRequest.customer_id == customer.id,
    ).order_by(QuoteRequest.created_at.desc()))).scalars().all()
    appointments = (await db.execute(select(AppointmentRequest).where(
        AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.customer_id == customer.id,
    ).order_by(AppointmentRequest.created_at.desc()))).scalars().all()
    complaints = (await db.execute(select(InsuranceComplaint).where(
        InsuranceComplaint.tenant_id == tenant.id, InsuranceComplaint.customer_id == customer.id,
    ).order_by(InsuranceComplaint.received_at.desc()))).scalars().all()

    return {
        "summary": {
            "active": len(active),
            "next_expiry": {"date": nearest.expires_on.isoformat(), "when_label": ict._when_label(ict.days_left(nearest, today)).removeprefix("échéance "),
                            "branch_label": insurance.branch_label(nearest.branch), "alert": ict.in_alert_window(nearest, today)}
            if nearest else None,
            "quotes": len(quotes),
            "quotes_open": sum(1 for q in quotes if q.status in QUOTE_OPEN),
            "complaints_open": sum(1 for c in complaints if c.status in icp.OPEN),
            "complaints_late": sum(1 for c in complaints if c.status in icp.OPEN and icp.is_overdue(c, today)),
        },
        "contracts": [ict.serialize(c, customer, today, can_see_premium, conversation_id) for c in contracts],
        "quotes": [{"id": str(q.id), "label": insurance.branch_label(q.branch),
                    "status": q.status, "status_label": insurance.STATUS_LABELS.get(q.status, q.status),
                    "at": _iso(q.submitted_at or q.created_at), "view": QUOTE_VIEWS.get(q.status, "todo")}
                   for q in quotes[:HISTORY_LIMIT]],
        "appointments": [appointment_line(a, now) for a in appointments[:HISTORY_LIMIT]],
        "complaints": [{"id": str(c.id), "reference": c.reference, "label": icp.KINDS.get(c.kind, c.kind),
                        "status": c.status, "status_label": icp.STATUSES.get(c.status, c.status),
                        "overdue": c.status in icp.OPEN and icp.is_overdue(c, today),
                        "at": _iso(c.received_at), "view": COMPLAINT_VIEWS.get(c.status, "all")}
                       for c in complaints[:HISTORY_LIMIT]],
        "more": {"quotes": max(0, len(quotes) - HISTORY_LIMIT), "appointments": max(0, len(appointments) - HISTORY_LIMIT),
                 "complaints": max(0, len(complaints) - HISTORY_LIMIT)},
    }
