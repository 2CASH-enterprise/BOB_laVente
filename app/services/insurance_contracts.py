"""
Lot 55 — registre des contrats du courtier / agent d'assurance, et échéances (rétention).

- Le cabinet saisit ses contrats (tableau de bord ou import CSV) : client (retrouvé par son téléphone,
  créé s'il n'existe pas), branche, assureur, numéro, prime, dates, durée, statut.
- Avant l'échéance, le cabinet est prévenu (tâche « Échéances » + un récapitulatif par email par jour),
  puis le client reçoit UN rappel fixe, écrit par le code : WhatsApp si la fenêtre de 20 h est ouverte,
  sinon email (s'il ne s'est pas désinscrit), sinon le contrat est marqué « à appeler ».
- Délais : alerte au cabinet à J-45 au plus, jamais plus de la moitié de la durée du contrat (mensuel
  J-15) ; rappel au client à J-30 au plus, jamais plus du quart de la durée (mensuel J-7).
- Règlement CIMA : la prime et le numéro de contrat ne sortent jamais du tableau de bord (ni IA, ni
  message au client) ; la prime n'est vue et saisie que par les administrateurs du cabinet.
"""
import csv
import io
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.services import insurance

logger = logging.getLogger(__name__)

ACTIVE = "ACTIVE"
RENEWED = "RENEWED"
CANCELLED = "CANCELLED"
LOST = "LOST"
EXPIRED = "EXPIRED"
STATUS_LABELS = {ACTIVE: "En cours", RENEWED: "Renouvelé", CANCELLED: "Résilié", LOST: "Perdu", EXPIRED: "Échu"}
# Statuts qu'on peut choisir à la main (« Renouvelé » passe par le bouton Renouveler, qui crée la suite).
MANUAL_STATUSES = (ACTIVE, CANCELLED, LOST, EXPIRED)

TERM_MONTHS = {"MENSUEL": 1, "TRIMESTRIEL": 3, "SEMESTRIEL": 6, "ANNUEL": 12}
ALERT_DAYS = 45      # alerte au cabinet
REMINDER_DAYS = 30   # rappel au client
EXPIRED_GRACE_DAYS = 30  # une échéance dépassée depuis moins de 30 jours reste « à traiter »
LIST_LIMIT = 500
CSV_MAX_ROWS = 5000

CHANNEL_LABELS = {"WHATSAPP": "WhatsApp", "EMAIL": "email", "CALL": "à appeler"}

# Indicatifs des pays proposés à l'inscription (lot 41) : un numéro saisi sans indicatif est complété.
DIAL_CODES = {
    "SN": "221", "CI": "225", "ML": "223", "BF": "226", "BJ": "229", "TG": "228", "NE": "227", "GW": "245",
    "GN": "224", "MR": "222", "GM": "220", "SL": "232", "LR": "231", "GH": "233", "NG": "234",
    "CM": "237", "GA": "241", "CG": "242", "TD": "235", "CF": "236", "GQ": "240", "CD": "243",
    "MA": "212", "DZ": "213", "TN": "216", "FR": "33", "BE": "32", "LU": "352", "CH": "41", "CA": "1",
}
# Pays où le 0 initial d'un numéro national disparaît après l'indicatif (06… → 336…). En Côte d'Ivoire
# et au Bénin, le 0 fait partie du numéro (07… → 22507…).
TRUNK_ZERO = frozenset({"FR", "BE", "CH", "MA", "DZ", "TN", "GH", "NG", "CM", "GA", "CG", "CD", "GN", "GW", "GQ"})


# --- Délais ---------------------------------------------------------------------------------------

def _term_days(term: str | None) -> int | None:
    from app.services.insurance_prospect import TERM_DAYS

    return TERM_DAYS.get(term or "")


def alert_days(term: str | None) -> int:
    """Jours avant l'échéance où le cabinet est prévenu : 45, au plus la moitié de la durée (mensuel 15)."""
    days = _term_days(term)
    return ALERT_DAYS if days is None else min(ALERT_DAYS, days // 2)


def reminder_days(term: str | None) -> int:
    """Jours avant l'échéance où le client est rappelé : 30, au plus le quart de la durée (mensuel 7)."""
    days = _term_days(term)
    return REMINDER_DAYS if days is None else min(REMINDER_DAYS, days // 4)


def days_left(contract, today: date) -> int:
    return (contract.expires_on - today).days


def in_alert_window(contract, today: date) -> bool:
    """Contrat en cours dont l'échéance est proche (ou dépassée depuis moins de 30 jours)."""
    left = days_left(contract, today)
    return contract.status == ACTIVE and -EXPIRED_GRACE_DAYS <= left <= alert_days(contract.term)


def needs_attention(contract, today: date) -> bool:
    """Tâche « Échéances » : dans la fenêtre d'alerte, et pas encore prise en charge par le cabinet."""
    return in_alert_window(contract, today) and contract.renewal_handled_at is None


def reminder_due(contract, today: date) -> bool:
    """Rappel au client : une seule fois, avant l'échéance, si le cabinet ne s'en occupe pas déjà."""
    left = days_left(contract, today)
    return (contract.status == ACTIVE and contract.client_reminded_at is None and contract.renewal_handled_at is None
            and 0 <= left <= reminder_days(contract.term))


def add_term(start: date, term: str) -> date:
    """start + la durée du contrat (fin de mois respectée : 31/01 + 1 mois → 28 ou 29/02)."""
    months = start.month - 1 + TERM_MONTHS[term]
    year, month = start.year + months // 12, months % 12 + 1
    from calendar import monthrange

    return date(year, month, min(start.day, monthrange(year, month)[1]))


def today_for(tenant, now: datetime | None = None) -> date:
    from app.services.local_time import tenant_zone

    now = now or datetime.now(timezone.utc)
    return now.astimezone(tenant_zone(tenant)).date()


# --- Valeurs saisies ------------------------------------------------------------------------------

class ContractError(ValueError):
    """Valeur refusée : le message est montré tel quel au cabinet."""


def normalize_phone(raw, country: str | None) -> str | None:
    """Numéro WhatsApp en chiffres avec indicatif (comme les clients venus de WhatsApp), ou None."""
    if raw is None:
        return None
    text = str(raw).strip()
    international = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None
    if not international:
        code = DIAL_CODES.get((country or "").upper())
        if code and not (digits.startswith(code) and len(digits) >= len(code) + 8):
            if country in TRUNK_ZERO and digits.startswith("0"):
                digits = digits[1:]
            digits = code + digits
    return digits if 8 <= len(digits) <= 15 else None


def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


_BRANCH_WORDS = [  # (mots, branche) — le premier qui correspond l'emporte
    (("flotte",), "FLOTTE"),
    (("rc pro", "responsabilite civile pro", "rc professionnelle"), "RC_PRO"),
    (("multirisque pro", "multirisque professionnelle", "mrp"), "MULTIRISQUE_PRO"),
    (("marchandise", "transport", "facultes"), "MARCHANDISES"),
    (("moto", "scooter", "deux roues"), "MOTO"),
    (("auto", "voiture", "vehicule"), "AUTO"),
    (("sante", "maladie", "mutuelle"), "SANTE"),
    (("habitation", "logement", "maison", "mrh"), "HABITATION"),
    (("voyage",), "VOYAGE"),
    (("vie", "prevoyance", "obseques", "retraite", "epargne", "deces"), "VIE_PREVOYANCE"),
    (("scolaire", "ecole"), "SCOLAIRE"),
    (("autre",), "AUTRE"),
]


def parse_branch(value) -> str | None:
    """Code de branche (AUTO…) ou libellé courant (« Auto », « Santé famille », « RC Pro »…)."""
    if value is None:
        return None
    raw = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    if raw in insurance.BRANCHES:
        return raw
    plain = _plain(value)
    if not plain:
        return None
    for code, entry in insurance.BRANCHES.items():
        if plain == _plain(entry[0]):
            return code
    words = f" {plain} "
    for keys, code in _BRANCH_WORDS:
        if any(f" {key} " in words or words.strip().startswith(key) for key in keys):
            return code
    return None


_TERM_WORDS = {
    "MENSUEL": ("mensuel", "mensuelle", "mois", "1 mois", "1m"),
    "TRIMESTRIEL": ("trimestriel", "trimestrielle", "trimestre", "3 mois", "3m"),
    "SEMESTRIEL": ("semestriel", "semestrielle", "semestre", "6 mois", "6m"),
    "ANNUEL": ("annuel", "annuelle", "an", "1 an", "12 mois", "12m", "annee"),
}


def parse_term(value) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().upper()
    if raw in TERM_MONTHS:
        return raw
    plain = _plain(value)
    for code, words in _TERM_WORDS.items():
        if plain in words:
            return code
    raise ContractError(f"Durée inconnue : « {value} » (mensuel, trimestriel, semestriel ou annuel)")


def parse_day(value, label: str, required: bool = False) -> date | None:
    """AAAA-MM-JJ ou JJ/MM/AAAA (format des tableurs en français). Jamais devinée."""
    if value in (None, ""):
        if required:
            raise ContractError(f"{label} : date obligatoire")
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text)
        try:
            parsed = date(int(match[3]), int(match[2]), int(match[1])) if match else date.fromisoformat(text[:10])
        except ValueError:
            raise ContractError(f"{label} : date invalide « {value} » (JJ/MM/AAAA ou AAAA-MM-JJ)") from None
    if not 2000 <= parsed.year <= 2100:
        raise ContractError(f"{label} : date invalide « {value} »")
    return parsed


def parse_premium(value) -> Decimal | None:
    """« 85 000 », « 85000 FCFA », « 1 250,50 » → Decimal ; vide → None."""
    if value in (None, ""):
        return None
    text = re.sub(r"[^\d,.]", "", str(value).replace(" ", "").replace("\xa0", ""))
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")  # « 1.500.000 » : séparateur des milliers
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ContractError(f"Prime invalide : « {value} »") from None
    if amount < 0 or amount >= Decimal("1e12"):
        raise ContractError(f"Prime invalide : « {value} »")
    return amount.quantize(Decimal("0.01"))


def _text(value, limit: int) -> str | None:
    cleaned = " ".join(str(value or "").split())[:limit]
    return cleaned or None


def _email(value) -> str | None:
    text = (str(value or "")).strip().lower()
    if not text:
        return None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text) or len(text) > 255:
        raise ContractError(f"Email invalide : « {value} »")
    return text


# --- Client ---------------------------------------------------------------------------------------

async def find_or_create_customer(db, tenant, phone, first_name=None, last_name=None, email=None, now=None):
    """
    Le client du contrat, retrouvé par son numéro (celui de WhatsApp), créé sinon. Un nom ou un email déjà
    connus ne sont jamais remplacés. Renvoie (client, créé ?).
    """
    from app.models.customer import Customer

    number = normalize_phone(phone, tenant.country)
    if number is None:
        raise ContractError("Téléphone du client invalide (avec l'indicatif, par exemple +225 07 00 00 00 00)")
    email = _email(email)
    customer = (await db.execute(select(Customer).where(
        Customer.tenant_id == tenant.id, Customer.whatsapp_number == number))).scalar_one_or_none()
    created = customer is None
    if created:
        customer = Customer(tenant_id=tenant.id, whatsapp_number=number, acquisition_source="IMPORT",
                            acquisition_detail="Registre des contrats", tags=[], detected_preferences={})
        db.add(customer)
    if not customer.first_name and not customer.last_name:
        customer.first_name = _text(first_name, 255)
        customer.last_name = _text(last_name, 255)
    if email and not customer.email:
        customer.email = email
        customer.email_source = "MANUAL"
        customer.email_collected_at = now or datetime.now(timezone.utc)
    await db.flush()
    return customer, created


# --- Contrats -------------------------------------------------------------------------------------

def _apply(contract, data: dict, can_see_premium: bool, currency: str | None) -> None:
    """Valeurs vérifiées → contrat. Une clé absente ne change rien ; la prime n'est touchée que par un admin."""
    if "branch" in data:
        branch = parse_branch(data["branch"])
        if branch is None:
            raise ContractError(f"Branche inconnue : « {data['branch']} »")
        contract.branch = branch
    if "insurer" in data:
        contract.insurer = _text(data["insurer"], 150)
    if "policy_number" in data:
        contract.policy_number = _text(data["policy_number"], 64)
    if "term" in data:
        contract.term = parse_term(data["term"])
    if "effective_on" in data:
        contract.effective_on = parse_day(data["effective_on"], "Date d'effet")
    if "expires_on" in data:
        contract.expires_on = parse_day(data["expires_on"], "Échéance", required=True)
    if "note" in data:
        contract.note = _text(data["note"], 500)
    if can_see_premium and "premium" in data:
        contract.premium = parse_premium(data["premium"])
        contract.currency = (_text(data.get("currency"), 3) or currency or "").upper() or None if contract.premium else None
    if contract.effective_on and contract.expires_on and contract.effective_on >= contract.expires_on:
        raise ContractError("La date d'effet doit précéder l'échéance")


async def create_contract(db, tenant, data: dict, can_see_premium: bool, now: datetime | None = None):
    from app.models.insurance_contract import InsuranceContract

    if data.get("expires_on") in (None, ""):
        raise ContractError("Échéance : date obligatoire")
    if data.get("branch") in (None, ""):
        raise ContractError("Branche obligatoire")
    customer = None
    if data.get("customer_id"):
        from app.models.customer import Customer

        customer = await db.get(Customer, data["customer_id"])
        if customer is None or customer.tenant_id != tenant.id:
            raise ContractError("Client introuvable")
    else:
        customer, _ = await find_or_create_customer(db, tenant, data.get("phone"), data.get("first_name"),
                                                    data.get("last_name"), data.get("email"), now)
    contract = InsuranceContract(tenant_id=tenant.id, customer_id=customer.id, status=ACTIVE)
    _apply(contract, data, can_see_premium, tenant.currency)
    if data.get("quote_request_id"):
        from app.models.quote_request import QuoteRequest

        quote = await db.get(QuoteRequest, data["quote_request_id"])
        if quote is not None and quote.tenant_id == tenant.id and quote.customer_id == customer.id:
            contract.quote_request_id = quote.id
    db.add(contract)
    await db.flush()
    return contract


def update_contract(contract, data: dict, can_see_premium: bool, tenant) -> None:
    """Modifier un contrat. Une nouvelle échéance repart à zéro (alerte et rappel de nouveau possibles)."""
    previous = contract.expires_on
    _apply(contract, {k: v for k, v in data.items() if k != "customer_id"}, can_see_premium, tenant.currency)
    if contract.expires_on != previous:
        _reset_reminders(contract)


def _reset_reminders(contract) -> None:
    contract.broker_alerted_at = None
    contract.client_reminded_at = None
    contract.client_reminder_channel = None
    contract.renewal_handled_at = None
    contract.renewal_handled_by = None


def set_status(contract, status: str) -> None:
    if status not in MANUAL_STATUSES:
        raise ContractError("Statut inconnu : " + ", ".join(STATUS_LABELS[s] for s in MANUAL_STATUSES))
    if contract.status == RENEWED:
        raise ContractError("Ce contrat a déjà été renouvelé : modifiez le contrat qui lui succède")
    contract.status = status


async def renew_contract(db, tenant, contract, data: dict, can_see_premium: bool):
    """
    Renouveler : le contrat passe « Renouvelé » et une nouvelle ligne prend la suite (même client, branche,
    assureur, numéro et prime sauf changement), de l'échéance actuelle à la nouvelle.
    """
    from app.models.insurance_contract import InsuranceContract

    if contract.status != ACTIVE:
        raise ContractError("Seul un contrat en cours peut être renouvelé")
    term = parse_term(data["term"]) if data.get("term") not in (None, "") else contract.term
    expires_on = parse_day(data.get("expires_on"), "Nouvelle échéance")
    if expires_on is None:
        if term is None:
            raise ContractError("Indiquez la nouvelle échéance (la durée du contrat n'est pas connue)")
        expires_on = add_term(contract.expires_on, term)
    if expires_on <= contract.expires_on:
        raise ContractError("La nouvelle échéance doit être après l'échéance actuelle")
    successor = InsuranceContract(
        tenant_id=contract.tenant_id, customer_id=contract.customer_id, quote_request_id=contract.quote_request_id,
        renewed_from_id=contract.id, branch=contract.branch, insurer=contract.insurer,
        policy_number=contract.policy_number, premium=contract.premium, currency=contract.currency,
        effective_on=contract.expires_on, expires_on=expires_on, term=term, status=ACTIVE, note=contract.note,
    )
    changes = {k: data[k] for k in ("insurer", "policy_number", "premium", "currency", "note") if k in data}
    _apply(successor, changes, can_see_premium, tenant.currency)
    contract.status = RENEWED
    db.add(successor)
    await db.flush()
    return successor


def handle_renewal(contract, user_id, now: datetime | None = None) -> None:
    """« Prise en charge » de l'échéance : le cabinet s'en occupe, Bob ne rappelle plus le client."""
    if contract.status != ACTIVE:
        raise ContractError("Ce contrat n'est plus en cours")
    if contract.renewal_handled_at is None:
        contract.renewal_handled_at = now or datetime.now(timezone.utc)
        contract.renewal_handled_by = str(user_id)


# --- Affichage ------------------------------------------------------------------------------------

def _day_label(day: date) -> str:
    from app.services.calendar_check import label

    return f"{label(day)} {day.year}"


def _when_label(left: int) -> str:
    if left > 1:
        return f"échéance dans {left} jours"
    if left == 1:
        return "échéance demain"
    if left == 0:
        return "échéance aujourd'hui"
    return f"échu depuis {-left} jour{'s' if left < -1 else ''}"


def reminder_label(contract) -> str | None:
    if contract.client_reminded_at is None:
        return None
    when = contract.client_reminded_at.strftime("%d/%m/%Y")
    if contract.client_reminder_channel == "CALL":
        return f"À appeler : pas de WhatsApp ouvert ni d'email (noté le {when})"
    return f"Client rappelé par {CHANNEL_LABELS.get(contract.client_reminder_channel, '—')} le {when}"


def serialize(contract, customer, today: date, can_see_premium: bool, conversation_id=None) -> dict:
    from app.services.handoff_service import customer_display_name

    left = days_left(contract, today)
    out = {
        "id": str(contract.id),
        "customer_id": str(contract.customer_id),
        "customer": customer_display_name(customer) if customer else "Client",
        "phone": f"+{customer.whatsapp_number}" if customer and customer.whatsapp_number else None,
        "email": customer.email if customer else None,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "branch": contract.branch,
        "branch_label": insurance.branch_label(contract.branch),
        "insurer": contract.insurer,
        "policy_number": contract.policy_number,
        "effective_on": contract.effective_on.isoformat() if contract.effective_on else None,
        "expires_on": contract.expires_on.isoformat(),
        "term": contract.term,
        "term_label": insurance.TERMS.get(contract.term or "") if contract.term else None,
        "status": contract.status,
        "status_label": STATUS_LABELS.get(contract.status, contract.status),
        "note": contract.note,
        "days_left": left,
        "when_label": _when_label(left),
        "alert": in_alert_window(contract, today),
        "to_handle": needs_attention(contract, today),
        "reminder_on": (contract.expires_on - timedelta(days=reminder_days(contract.term))).isoformat(),
        "reminder": reminder_label(contract),
        "reminder_channel": contract.client_reminder_channel,
        "renewal_handled_at": contract.renewal_handled_at,
        "quote_request_id": str(contract.quote_request_id) if contract.quote_request_id else None,
        "renewed_from_id": str(contract.renewed_from_id) if contract.renewed_from_id else None,
        "premium_visible": can_see_premium,
    }
    if can_see_premium:  # règle du cabinet (4A) : la prime n'est montrée qu'aux administrateurs
        out["premium"] = float(contract.premium) if contract.premium is not None else None
        out["currency"] = contract.currency
    return out


VIEWS = ("all", "due30", "due60", "due90", "todo", "closed")


async def list_contracts(db, tenant, view: str = "all", branch: str | None = None, search: str | None = None,
                         now: datetime | None = None) -> tuple[list, date]:
    """Contrats du cabinet pour une vue de la page « Contrats » : [(contrat, client, conversation)]."""
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.models.insurance_contract import InsuranceContract

    today = today_for(tenant, now)
    query = select(InsuranceContract, Customer).join(Customer, Customer.id == InsuranceContract.customer_id).where(
        InsuranceContract.tenant_id == tenant.id, Customer.tenant_id == tenant.id)
    if view in ("due30", "due60", "due90"):
        horizon = int(view[3:])
        query = query.where(InsuranceContract.status == ACTIVE,
                            InsuranceContract.expires_on >= today - timedelta(days=EXPIRED_GRACE_DAYS),
                            InsuranceContract.expires_on <= today + timedelta(days=horizon))
    elif view == "todo":
        query = query.where(InsuranceContract.status == ACTIVE, InsuranceContract.renewal_handled_at.is_(None),
                            InsuranceContract.expires_on >= today - timedelta(days=EXPIRED_GRACE_DAYS),
                            InsuranceContract.expires_on <= today + timedelta(days=ALERT_DAYS))
    elif view == "closed":
        query = query.where(InsuranceContract.status != ACTIVE)
    else:
        query = query.where(InsuranceContract.status == ACTIVE)
    if branch:
        query = query.where(InsuranceContract.branch == branch)
    rows = (await db.execute(query.order_by(InsuranceContract.expires_on, InsuranceContract.created_at)
                             .limit(LIST_LIMIT * 2 if search else LIST_LIMIT))).all()
    if view == "todo":
        rows = [(c, cu) for c, cu in rows if needs_attention(c, today)]
    if search:
        needle = _plain(search)
        digits = re.sub(r"\D", "", search)
        rows = [(c, cu) for c, cu in rows if needle in _plain(" ".join(filter(None, (
            cu.first_name, cu.last_name, c.insurer, c.policy_number, insurance.branch_label(c.branch)))))
            or (len(digits) >= 4 and digits in (cu.whatsapp_number or ""))]
    rows = rows[:LIST_LIMIT]
    latest = {}
    customer_ids = list({cu.id for _, cu in rows})
    if customer_ids:
        for conv in (await db.execute(select(Conversation).where(
            Conversation.tenant_id == tenant.id, Conversation.customer_id.in_(customer_ids),
        ).order_by(Conversation.created_at))).scalars().all():
            latest[conv.customer_id] = conv.id
    return [(c, cu, latest.get(cu.id)) for c, cu in rows], today


async def to_handle_count(db, tenant, now: datetime | None = None) -> int:
    """Nombre de contrats « Échéances » à traiter (pastille et tâche)."""
    from app.models.insurance_contract import InsuranceContract

    today = today_for(tenant, now)
    candidates = (await db.execute(select(InsuranceContract).where(
        InsuranceContract.tenant_id == tenant.id, InsuranceContract.status == ACTIVE,
        InsuranceContract.renewal_handled_at.is_(None),
        InsuranceContract.expires_on >= today - timedelta(days=EXPIRED_GRACE_DAYS),
        InsuranceContract.expires_on <= today + timedelta(days=ALERT_DAYS),
    ))).scalars().all()
    return sum(1 for c in candidates if needs_attention(c, today))


async def summary(db, tenant, now: datetime | None = None) -> dict:
    """Chiffres de la page « Contrats » : en cours, échéances sous 30 / 60 / 90 jours, à traiter."""
    from app.models.insurance_contract import InsuranceContract

    today = today_for(tenant, now)
    active = (await db.execute(select(InsuranceContract).where(
        InsuranceContract.tenant_id == tenant.id, InsuranceContract.status == ACTIVE))).scalars().all()
    def within(n):
        return sum(1 for c in active if -EXPIRED_GRACE_DAYS <= days_left(c, today) <= n)
    return {"active": len(active), "due30": within(30), "due60": within(60), "due90": within(90),
            "todo": sum(1 for c in active if needs_attention(c, today))}


# --- Ce que Bob sait (jamais la prime ni le numéro) -----------------------------------------------

MEMORY_LIMIT = 5


async def memory_for_bob(db, tenant, customer_id, now: datetime | None = None) -> str:
    """
    Contrats en cours du client, pour que Bob comprenne une réponse au rappel d'échéance. Seulement la
    branche, l'assureur et l'échéance : la prime et le numéro ne sont JAMAIS donnés à l'IA (CIMA).
    """
    from app.models.insurance_contract import InsuranceContract
    from app.services.business_type import is_insurance

    if tenant is None or not is_insurance(tenant):
        return ""
    contracts = (await db.execute(select(InsuranceContract).where(
        InsuranceContract.tenant_id == tenant.id, InsuranceContract.customer_id == customer_id,
        InsuranceContract.status == ACTIVE,
    ).order_by(InsuranceContract.expires_on).limit(MEMORY_LIMIT))).scalars().all()
    if not contracts:
        return ""
    today = today_for(tenant, now)
    lines = []
    for c in contracts:
        line = f"- {insurance.branch_label(c.branch)}"
        if c.insurer:
            line += f" chez {c.insurer}"
        line += f" : échéance le {_day_label(c.expires_on)} ({_when_label(days_left(c, today))})"
        if c.client_reminded_at is not None and c.client_reminder_channel in ("WHATSAPP", "EMAIL"):
            line += f" — rappel d'échéance envoyé au client par {CHANNEL_LABELS[c.client_reminder_channel]}"
        lines.append(line)
    return ("\n\nCONTRATS DU CLIENT AU CABINET (registre tenu par le cabinet)\n" + "\n".join(lines) + "\n"
            "Si le client parle de son contrat ou de son renouvellement : propose que son conseiller le rappelle, "
            "ou un rendez-vous au cabinet (request_appointment). Tu ne connais ni la prime ni les conditions du "
            "contrat : ne donne jamais de montant et ne promets rien sur le renouvellement.")


# --- Messages au client (fixes, écrits par le code, toujours au vouvoiement) ----------------------

def _contract_words(contract) -> str:
    label = insurance.branch_label(contract.branch)
    label = label[0].lower() + label[1:]
    return f"votre contrat {'d' + chr(39) if label[0] in 'aeiouyéèh' else 'de '}{label}"


def whatsapp_reminder(tenant, customer, contract) -> str:
    first = (customer.first_name or "").strip()
    hello = f"Bonjour {first}, ici {tenant.name}." if first else f"Bonjour, ici {tenant.name}."
    insurer = f" ({contract.insurer})" if contract.insurer else ""
    return (f"{hello} {_contract_words(contract)[0].upper()}{_contract_words(contract)[1:]}{insurer} arrive à échéance "
            f"le {_day_label(contract.expires_on)}. Souhaitez-vous que votre conseiller étudie son renouvellement avec "
            f"vous ? Répondez simplement à ce message.")


async def reminder_email(db, tenant, customer, contract) -> dict:
    """Rappel d'échéance par email : message de service (pas une offre), avec lien de désinscription."""
    from urllib.parse import quote

    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.campaign_service import _unsubscribe_headers
    from app.services.email_layout import customer_footer
    from app.services.unsubscribe_service import build_unsubscribe_url

    first = (customer.first_name or "").strip()
    insurer = f" ({contract.insurer})" if contract.insurer else ""
    subject = f"{tenant.name} : {_contract_words(contract)} arrive à échéance le {contract.expires_on.strftime('%d/%m/%Y')}"
    lines = [
        f"Bonjour {first}," if first else "Bonjour,",
        "",
        f"{_contract_words(contract)[0].upper()}{_contract_words(contract)[1:]}{insurer} arrive à échéance le "
        f"{_day_label(contract.expires_on)}.",
        "",
        "Souhaitez-vous que votre conseiller étudie son renouvellement avec vous ? Répondez simplement à cet "
        "email, ou écrivez-nous sur WhatsApp.",
    ]
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()
    if account is not None and account.display_phone_number:
        digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
        hello = "Bonjour, je souhaite parler du renouvellement de mon contrat"
        lines += ["", f"Écrire sur WhatsApp : https://wa.me/{digits}?text={quote(hello)}"]
    unsubscribe_url = build_unsubscribe_url(customer.id)
    body = "\n".join(lines) + customer_footer(tenant.name, not tenant.is_paid, [
        f"Vous recevez cet email car vous êtes client de {tenant.name}, pour vous rappeler l'échéance de votre contrat.",
        f"Ne plus recevoir d'email : {unsubscribe_url}",
    ])
    return {"to": customer.email, "subject": subject, "body": body, "from_name": tenant.name,
            "reply_to": tenant.email, "extra_headers": _unsubscribe_headers(unsubscribe_url)}


def digest_email(tenant, rows: list, today: date) -> tuple[str, str]:
    """Récapitulatif quotidien au cabinet : les contrats qui viennent d'entrer dans la fenêtre d'alerte."""
    from app.core.config import get_settings
    from app.services.handoff_service import customer_display_name

    count = len(rows)
    subject = f"{count} échéance{'s' if count > 1 else ''} de contrat à préparer — {tenant.name}"
    items = []
    for contract, customer in rows:
        insurer = f", {contract.insurer}" if contract.insurer else ""
        number = f", n° {contract.policy_number}" if contract.policy_number else ""
        items.append(f"- {customer_display_name(customer)} — {insurance.branch_label(contract.branch)}{insurer}{number} — "
                     f"le {contract.expires_on.strftime('%d/%m/%Y')} ({_when_label(days_left(contract, today))})")
    base = get_settings().public_base_url.rstrip("/")
    body = "\n".join([
        "Bonjour,",
        "",
        "Contrats à renouveler :",
        *items,
        "",
        "Sans action de votre part, Bob rappellera chaque client 30 jours avant son échéance (7 jours pour un "
        "contrat mensuel) : par WhatsApp s'il vous a écrit dans les 20 dernières heures, sinon par email. "
        "Cliquez « Prise en charge » si vous vous en occupez : Bob ne le rappellera pas.",
        "",
        f"Voir les contrats : {base}/dashboard/?tab=contracts",
        "",
        "— Bob",
    ])
    return subject, body


# --- Tâche planifiée ------------------------------------------------------------------------------

DIGEST_HOUR = 8     # récapitulatif au cabinet à partir de 8 h, heure du pays
REMINDER_HOURS = range(9, 20)  # rappels aux clients entre 9 h et 20 h, heure du pays


async def run_for_tenant(db, tenant, now: datetime | None = None, send_email=None, send_whatsapp=None) -> dict:
    """
    Une boutique courtier : récapitulatif des nouvelles échéances au cabinet (une fois par jour), puis
    rappels aux clients. Rien pour une démo ni pendant une pause de Bob (lot 51).
    """
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.models.insurance_contract import InsuranceContract
    from app.services.bob_pause import is_paused
    from app.services.business_type import is_insurance
    from app.services.local_time import tenant_zone

    report = {"digest": 0, "whatsapp": 0, "email": 0, "call": 0, "failed": 0}
    now = now or datetime.now(timezone.utc)
    if tenant is None or not is_insurance(tenant) or tenant.is_demo or is_paused(tenant, now):  # suspendue = en pause
        return report
    if send_email is None:
        from app.services.email_service import send_email
    local = now.astimezone(tenant_zone(tenant))
    today = local.date()
    window = select(InsuranceContract, Customer).join(Customer, Customer.id == InsuranceContract.customer_id).where(
        InsuranceContract.tenant_id == tenant.id, Customer.tenant_id == tenant.id, InsuranceContract.status == ACTIVE,
        InsuranceContract.expires_on >= today - timedelta(days=EXPIRED_GRACE_DAYS),
        InsuranceContract.expires_on <= today + timedelta(days=ALERT_DAYS),
    ).order_by(InsuranceContract.expires_on)
    rows = (await db.execute(window)).all()

    # 1. Récapitulatif au cabinet : contrats entrés dans la fenêtre d'alerte, jamais annoncés.
    if local.hour >= DIGEST_HOUR and tenant.renewal_digest_on != today:
        fresh = [(c, cu) for c, cu in rows if needs_attention(c, today) and c.broker_alerted_at is None]
        if fresh:
            subject, body = digest_email(tenant, fresh, today)
            sent = False
            if tenant.email:
                try:
                    sent = bool(send_email(to=tenant.email, subject=subject, body=body))
                except Exception:  # noqa: BLE001
                    logger.warning("Récapitulatif des échéances impossible (boutique %s)", tenant.id)
            if sent:
                for contract, _ in fresh:
                    contract.broker_alerted_at = now
                tenant.renewal_digest_on = today
                report["digest"] = len(fresh)
            else:
                report["failed"] += 1
            await db.commit()

    # 2. Rappel au client : une fois, WhatsApp (fenêtre ouverte) sinon email sinon « à appeler ».
    if not tenant.renewal_reminders_enabled or local.hour not in REMINDER_HOURS:
        return report
    from app.services.human_reply import customer_window_open

    send_whatsapp = send_whatsapp or _send_whatsapp
    for contract, customer in rows:
        if not reminder_due(contract, today):
            continue
        try:
            channel = None
            conversation = (await db.execute(select(Conversation).where(
                Conversation.tenant_id == tenant.id, Conversation.customer_id == customer.id,
            ).order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()
            # Règle des 20 h (lot 49) : WhatsApp seulement si le client a écrit récemment, sinon email.
            if conversation is not None and await customer_window_open(db, tenant.id, customer.id, now):
                if await send_whatsapp(db, tenant, conversation, customer, whatsapp_reminder(tenant, customer, contract)):
                    channel = "WHATSAPP"
            if channel is None and customer.email and customer.marketing_consent_withdrawn_at is None:
                mail = await reminder_email(db, tenant, customer, contract)
                if send_email(**mail):
                    channel = "EMAIL"
                    if conversation is not None:
                        from app.models.conversation import Message, MessageSender

                        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                                       message_type="renewal_email",
                                       content=f"Rappel d'échéance envoyé par email : « {mail['subject']} »"))
                else:
                    report["failed"] += 1
                    continue  # nouvel essai à la prochaine heure
            elif channel is None and not (customer.email and customer.marketing_consent_withdrawn_at is None):
                channel = "CALL"
            if channel is None:
                continue
            contract.client_reminded_at = now
            contract.client_reminder_channel = channel
            report[channel.lower()] += 1
            await db.commit()
        except Exception:  # noqa: BLE001 — un contrat en échec ne bloque jamais les autres
            await db.rollback()
            report["failed"] += 1
            logger.warning("Rappel d'échéance impossible (contrat %s)", contract.id)
    return report


async def _send_whatsapp(db, tenant, conversation, customer, text) -> bool:
    from app.services.appointment_service import send_fixed_message

    sent, _ = await send_fixed_message(db, tenant, conversation, customer, text, "BOB", "renewal_reminder")
    return sent


# --- Import CSV -----------------------------------------------------------------------------------

CSV_COLUMNS = {  # colonne reconnue → champ (en-têtes sans accents ni majuscules)
    "telephone": "phone", "tel": "phone", "phone": "phone", "numero de telephone": "phone", "whatsapp": "phone",
    "mobile": "phone", "portable": "phone",
    "prenom": "first_name", "first name": "first_name",
    "nom": "last_name", "nom du client": "last_name", "client": "last_name", "last name": "last_name", "name": "last_name",
    "email": "email", "e mail": "email", "mail": "email", "courriel": "email",
    "branche": "branch", "assurance": "branch", "type": "branch", "produit": "branch", "garantie": "branch",
    "assureur": "insurer", "compagnie": "insurer", "insurer": "insurer",
    "numero": "policy_number", "numero de contrat": "policy_number", "n contrat": "policy_number",
    "n de contrat": "policy_number", "numero de police": "policy_number", "police": "policy_number",
    "contrat": "policy_number", "policy": "policy_number",
    "prime": "premium", "montant": "premium", "premium": "premium", "prime ttc": "premium",
    "devise": "currency", "monnaie": "currency", "currency": "currency",
    "date d effet": "effective_on", "date effet": "effective_on", "effet": "effective_on", "debut": "effective_on", "date de debut": "effective_on",
    "echeance": "expires_on", "date d echeance": "expires_on", "date echeance": "expires_on", "fin": "expires_on", "date de fin": "expires_on",
    "expiration": "expires_on",
    "duree": "term", "periodicite": "term", "frequence": "term",
    "note": "note", "remarque": "note", "commentaire": "note",
}


def _read_csv(content: str) -> tuple[list[dict], list[str]]:
    sample = content[:4096]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise ContractError("Le fichier est vide") from None
    fields, unknown = [], []
    for name in header:
        field = CSV_COLUMNS.get(_plain(name))
        fields.append(field)
        if field is None and name.strip():
            unknown.append(name.strip())
    if "phone" not in fields or "expires_on" not in fields or "branch" not in fields:
        raise ContractError("Colonnes obligatoires : téléphone, branche et échéance")
    rows = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        row = {}
        for field, value in zip(fields, values):
            if field and value.strip() and field not in row:
                row[field] = value.strip()
        rows.append(row)
        if len(rows) > CSV_MAX_ROWS:
            raise ContractError(f"Trop de lignes : {CSV_MAX_ROWS} contrats au plus par fichier")
    return rows, unknown


async def import_csv(db, tenant, content: str, can_see_premium: bool, now: datetime | None = None) -> dict:
    """
    Import du registre : une ligne = un contrat. Un numéro de contrat déjà présent (en cours) met la ligne à
    jour ; sans numéro, une ligne identique (client, branche, échéance) n'est pas créée deux fois.
    Une ligne invalide est signalée et ignorée, les autres sont importées.
    """
    from app.models.insurance_contract import InsuranceContract

    rows, unknown = _read_csv(content)
    report = {"created": 0, "updated": 0, "skipped": 0, "customers_created": 0, "errors": [],
              "ignored_columns": unknown, "premium_ignored": False}
    has_premium = any("premium" in r for r in rows)
    if has_premium and not can_see_premium:
        report["premium_ignored"] = True
    from app.models.customer import Customer

    for index, row in enumerate(rows, start=2):  # ligne 1 = en-têtes
        try:
            # Tout est vérifié AVANT d'écrire : une ligne refusée ne laisse rien derrière elle.
            if not row.get("branch"):
                raise ContractError("branche manquante")
            if not row.get("expires_on"):
                raise ContractError("échéance manquante")
            probe = InsuranceContract(tenant_id=tenant.id, status=ACTIVE)
            _apply(probe, row, can_see_premium, tenant.currency)
            number = normalize_phone(row.get("phone"), tenant.country)
            if number is None:
                raise ContractError("téléphone invalide (avec l'indicatif, par exemple +225 07 00 00 00 00)")
            _email(row.get("email"))
            existing = None
            if probe.policy_number:
                existing = (await db.execute(select(InsuranceContract).where(
                    InsuranceContract.tenant_id == tenant.id, InsuranceContract.status == ACTIVE,
                    InsuranceContract.policy_number == probe.policy_number,
                ).order_by(InsuranceContract.created_at.desc()).limit(1))).scalar_one_or_none()
                if existing is not None:
                    owner = await db.get(Customer, existing.customer_id)
                    if owner is None or owner.whatsapp_number != number:
                        raise ContractError("ce numéro de contrat appartient déjà à un autre client")
            customer, created = await find_or_create_customer(
                db, tenant, row.get("phone"), row.get("first_name"), row.get("last_name"), row.get("email"), now)
            if created:
                report["customers_created"] += 1
            if existing is not None:
                update_contract(existing, row, can_see_premium, tenant)
                report["updated"] += 1
                continue
            duplicate = None if probe.policy_number else (await db.execute(select(InsuranceContract.id).where(
                InsuranceContract.tenant_id == tenant.id, InsuranceContract.customer_id == customer.id,
                InsuranceContract.branch == probe.branch, InsuranceContract.expires_on == probe.expires_on,
                InsuranceContract.status == ACTIVE,
            ).limit(1))).scalar_one_or_none()
            if duplicate is not None:
                report["skipped"] += 1
                continue
            probe.customer_id = customer.id
            db.add(probe)
            await db.flush()
            report["created"] += 1
        except ContractError as exc:
            report["errors"].append({"line": index, "message": str(exc)})
    await db.commit()
    report["errors"] = report["errors"][:50]
    return report


CSV_TEMPLATE = ("telephone;prenom;nom;email;branche;assureur;numero;prime;devise;date_effet;echeance;duree\n"
                "+225 07 00 00 00 00;Awa;Koné;VOTRE_EMAIL_ICI;Auto;Assureur;POL-0001;85000;XOF;01/01/2026;31/12/2026;annuel\n")
