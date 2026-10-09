"""
Lot 54 — score du prospect chez le courtier / agent d'assurance.

Règles FIXES (jamais un avis de l'IA), expliquées au cabinet en une ligne :
  Souscrit = le dernier rendez-vous a abouti à un contrat (ce n'est plus un prospect)
  Froid    = le client a dit au rendez-vous qu'il n'était pas intéressé
  Chaud    = l'échéance de son contrat actuel est proche, OU demande de cotation transmise ET appel /
             rendez-vous prévu
  Tiède    = demande transmise, OU appel / rendez-vous prévu, OU rendez-vous honoré à relancer, OU
             échéance connue (pas encore proche)
  Froid    = sinon (simple renseignement, absent au rendez-vous…)

« Échéance proche » (choix du 09/10, contrats auto d'un mois) : l'échéance tombe dans la moitié de la
durée du contrat, avec un maximum de 60 jours — mensuel 15 jours, trimestriel 45 jours, semestriel et
annuel 60 jours ; durée inconnue : 60 jours. Un contrat échu depuis moins de 30 jours compte aussi
(le client n'est peut-être plus assuré). Au-delà, l'échéance n'est plus utilisée.
"""
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.services import insurance

MAX_WINDOW_DAYS = 60
EXPIRED_GRACE_DAYS = 30
TERM_DAYS = {"MENSUEL": 30, "TRIMESTRIEL": 91, "SEMESTRIEL": 182, "ANNUEL": 365}
SCORES = {"VENDU": "Souscrit", "CHAUD": "Chaud", "TIEDE": "Tiède", "FROID": "Froid"}
OUTCOME_REASONS = {"FOLLOW_UP": "rendez-vous honoré, à relancer", "NOT_INTERESTED": "pas intéressé au rendez-vous",
                   "NO_SHOW": "absent au rendez-vous"}


def hot_window(term: str | None) -> int:
    """Nombre de jours avant l'échéance à partir duquel le prospect est « Chaud »."""
    days = TERM_DAYS.get(term or "")
    return MAX_WINDOW_DAYS if days is None else min(MAX_WINDOW_DAYS, days // 2)


def expiry_reason(days_left: int, term: str | None) -> str:
    if days_left < 0:
        text = f"contrat actuel échu depuis {-days_left} jour{'s' if -days_left > 1 else ''}"
    elif days_left == 0:
        text = "échéance aujourd'hui"
    else:
        text = f"échéance dans {days_left} jour{'s' if days_left > 1 else ''}"
    return f"{text} (contrat {insurance.TERMS[term].lower()})" if term in insurance.TERMS else text


def nearest_expiry(requests, today: date) -> tuple[date, str | None, int] | None:
    """L'échéance la plus urgente déclarée par le client : (date, durée, jours restants), ou None."""
    best = None
    for request in requests:
        details = request.details or {}
        expiry = insurance.parse_date(details.get("current_expiry"))
        if expiry is None:
            continue
        days_left = (expiry - today).days
        if days_left < -EXPIRED_GRACE_DAYS:
            continue
        term = details.get("current_term") if details.get("current_term") in insurance.TERMS else None
        if best is None or days_left < best[2]:
            best = (expiry, term, days_left)
    return best


def score(submitted: bool, has_appointment: bool, outcome: str | None, expiry: tuple | None) -> tuple[str, list[str]]:
    """(VENDU | CHAUD | TIEDE | FROID, raisons lisibles). Règles fixes, voir l'en-tête du module."""
    if outcome == "SOLD":
        return "VENDU", ["contrat souscrit"]
    reasons = []
    if outcome in OUTCOME_REASONS:
        reasons.append(OUTCOME_REASONS[outcome])
    if expiry is not None:
        reasons.append(expiry_reason(expiry[2], expiry[1]))
    if submitted:
        reasons.append("demande de cotation transmise")
    if has_appointment:
        reasons.append("appel ou rendez-vous prévu")
    if outcome == "NOT_INTERESTED":
        return "FROID", reasons
    soon = expiry is not None and expiry[2] <= hot_window(expiry[1])
    if soon or (submitted and has_appointment):
        return "CHAUD", reasons
    if submitted or has_appointment or outcome == "FOLLOW_UP" or expiry is not None:
        return "TIEDE", reasons
    return "FROID", reasons or ["simple renseignement pour l'instant"]


def _today(tenant, now: datetime) -> date:
    from app.services.local_time import tenant_zone

    return now.astimezone(tenant_zone(tenant)).date()


async def for_customers(db, tenant, customer_ids=None, now: datetime | None = None) -> dict:
    """
    Score de chaque prospect du cabinet (tous, ou ceux de customer_ids) : client → dict. Seuls les clients
    qui ont une demande de cotation ou un rendez-vous sont des prospects. Deux requêtes, quel que soit le nombre.
    """
    from app.models.appointment_request import STATUS_CANCELLED, AppointmentRequest
    from app.models.quote_request import QuoteRequest
    from app.services.prospect import _has_upcoming

    now = now or datetime.now(timezone.utc)
    if customer_ids is not None and not customer_ids:
        return {}
    request_query = select(QuoteRequest).where(QuoteRequest.tenant_id == tenant.id)
    appointment_query = select(AppointmentRequest).where(
        AppointmentRequest.tenant_id == tenant.id, AppointmentRequest.status != STATUS_CANCELLED)
    if customer_ids is not None:
        request_query = request_query.where(QuoteRequest.customer_id.in_(list(customer_ids)))
        appointment_query = appointment_query.where(AppointmentRequest.customer_id.in_(list(customer_ids)))
    requests: dict = {}
    for request in (await db.execute(request_query.order_by(QuoteRequest.created_at.desc()))).scalars().all():
        requests.setdefault(request.customer_id, []).append(request)
    latest: dict = {}
    for appointment in (await db.execute(appointment_query.order_by(AppointmentRequest.created_at))).scalars().all():
        latest[appointment.customer_id] = appointment  # trié par date : le dernier l'emporte

    today = _today(tenant, now)
    result = {}
    for customer_id in set(requests) | set(latest):
        own = requests.get(customer_id, [])
        appointment = latest.get(customer_id)
        expiry = nearest_expiry(own, today)
        has_appointment = _has_upcoming(appointment, now)
        outcome = appointment.outcome if appointment is not None else None
        submitted = any(r.status in (insurance.STATUS_SUBMITTED, insurance.STATUS_HANDLED) for r in own)
        code, reasons = score(submitted, has_appointment, outcome, expiry)
        result[customer_id] = {
            "score": code, "score_label": SCORES[code], "reasons": reasons,
            "branches": list(dict.fromkeys(insurance.branch_label(r.branch) for r in own)),
            "current_insurer": next(((r.details or {}).get("current_insurer") for r in own
                                     if (r.details or {}).get("current_insurer")), None),
            "expiry": expiry[0] if expiry else None, "term": expiry[1] if expiry else None,
            "days_left": expiry[2] if expiry else None,
            "expiry_soon": expiry is not None and expiry[2] <= hot_window(expiry[1]),
            "has_appointment": has_appointment, "outcome": outcome,
        }
    return result


async def build_fiche(db, tenant, customer, now: datetime | None = None) -> dict | None:
    """Fiche du prospect (mêmes clés que la fiche de la concession) ; None si Bob n'a encore rien appris."""
    from app.services.handoff_service import customer_display_name

    prospect = (await for_customers(db, tenant, [customer.id], now)).get(customer.id)
    if prospect is None:
        return None
    fiche = {
        **prospect,
        "prospect": " ".join(p for p in (customer.first_name, customer.last_name) if p) or customer_display_name(customer),
        "phone": f"+{customer.whatsapp_number}" if customer.whatsapp_number and customer.whatsapp_number.isdigit() else None,
        "city": customer.city,
    }
    expiry = None
    if fiche["expiry"] is not None:
        expiry = fiche["expiry"].strftime("%d/%m/%Y")
        if fiche["term"]:
            expiry += f" ({insurance.TERMS[fiche['term']].lower()})"
    rows = [
        ("Prospect", fiche["prospect"]),
        ("Téléphone", fiche["phone"]),
        ("Ville", fiche["city"]),
        ("Assurance demandée", ", ".join(fiche["branches"])),
        ("Assureur actuel", fiche["current_insurer"]),
        ("Échéance du contrat actuel", expiry),
        ("Score commercial", f"{fiche['score_label']} ({', '.join(fiche['reasons'])})"),
    ]
    fiche["lines"] = [f"{label} : {' '.join(str(value).split())}" for label, value in rows if value]
    return fiche
