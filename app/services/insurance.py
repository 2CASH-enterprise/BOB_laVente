"""
Lot 53 — courtier / agent d'assurance.

- Branches d'assurance : chacune a ses questions de qualification (minimum à réunir avant de
  transmettre une demande de cotation au cabinet) et un produit par défaut créé à l'inscription.
- Demande de cotation : Bob range ce que le client dit avec l'outil update_insurance_request ; c'est
  le CODE qui décide quand le minimum est réuni, enregistre la demande, prévient le cabinet (email +
  tâche) et dit à Bob de proposer un appel ou un rendez-vous. Bob ne décide jamais seul.
- Montants : Bob ne donne JAMAIS de prime, de tarif, de franchise ni de montant de garantie. Le code
  relit chaque réponse ; un montant est corrigé une fois, puis remplacé par un message fixe.
"""
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select

PARTICULIER = "PARTICULIER"
ENTREPRISE = "ENTREPRISE"
CLIENT_TYPES = {PARTICULIER: "Particulier", ENTREPRISE: "Entreprise"}

# Champs que Bob peut remplir : (libellé pour le cabinet, type, longueur max pour un texte).
FIELDS = {
    "company_name": ("Entreprise", "text", 150),
    "vehicle": ("Véhicule (marque, modèle)", "text", 150),
    "vehicle_year": ("Année du véhicule", "int", None),
    "vehicle_value": ("Valeur du véhicule", "text", 80),
    "vehicles_count": ("Nombre de véhicules", "int", None),
    "usage": ("Usage", "text", 150),
    "coverage": ("Couverture souhaitée", "text", 150),
    "persons_count": ("Nombre de personnes", "int", None),
    "ages": ("Âges", "text", 150),
    "occupancy": ("Propriétaire ou locataire", "text", 60),
    "housing_type": ("Type de logement", "text", 100),
    "destination": ("Destination", "text", 120),
    "travel_dates": ("Dates ou durée du voyage", "text", 120),
    "objective": ("Objectif", "text", 150),
    "age": ("Âge", "int", None),
    "children_count": ("Nombre d'enfants", "int", None),
    "activity": ("Activité", "text", 150),
    "employees": ("Effectif", "int", None),
    "premises": ("Locaux (type, ville)", "text", 150),
    "goods": ("Marchandises", "text", 150),
    "route": ("Trajet / mode de transport", "text", 150),
    "description": ("Besoin décrit par le client", "text", 300),
    "current_insurer": ("Assureur actuel", "text", 100),
    "current_expiry": ("Échéance du contrat actuel", "text", 60),
}

# code → (libellé, type de client par défaut, champs indispensables, champs utiles, description du produit)
BRANCHES = {
    "AUTO": ("Assurance automobile", PARTICULIER, ("vehicle", "vehicle_year", "usage"), ("vehicle_value", "coverage"),
             "Responsabilité civile obligatoire, tiers, tiers plus ou tous risques, selon votre véhicule et son usage."),
    "MOTO": ("Assurance moto", PARTICULIER, ("vehicle", "usage"), ("vehicle_year", "coverage"),
             "Assurance de votre moto ou scooter : responsabilité civile obligatoire et garanties complémentaires."),
    "SANTE": ("Assurance santé", PARTICULIER, ("persons_count", "ages"), ("coverage",),
              "Prise en charge de vos frais de santé, pour vous seul ou toute votre famille."),
    "HABITATION": ("Assurance habitation", PARTICULIER, ("occupancy", "housing_type"), ("coverage",),
                   "Votre logement et vos biens protégés : incendie, dégâts des eaux, vol, responsabilité civile."),
    "VOYAGE": ("Assurance voyage", PARTICULIER, ("destination", "travel_dates", "persons_count"), (),
               "Assistance et frais médicaux à l'étranger, souvent exigés pour le visa."),
    "VIE_PREVOYANCE": ("Assurance vie et prévoyance", PARTICULIER, ("objective", "age"), (),
                       "Épargne, retraite, études des enfants, obsèques ou protection de vos proches."),
    "SCOLAIRE": ("Assurance scolaire", PARTICULIER, ("children_count",), ("ages",),
                 "Vos enfants couverts à l'école et pendant les activités scolaires."),
    "RC_PRO": ("Responsabilité civile professionnelle", ENTREPRISE, ("company_name", "activity"), ("employees",),
               "Les dommages causés à vos clients ou à des tiers dans le cadre de votre activité."),
    "FLOTTE": ("Assurance flotte automobile", ENTREPRISE, ("company_name", "vehicles_count", "usage"), ("coverage",),
               "Tous les véhicules de votre entreprise assurés dans un seul contrat."),
    "MULTIRISQUE_PRO": ("Multirisque professionnelle", ENTREPRISE, ("company_name", "activity", "premises"), ("employees",),
                        "Vos locaux, votre matériel et votre activité protégés : incendie, vol, dégâts des eaux, pertes."),
    "MARCHANDISES": ("Assurance marchandises transportées", ENTREPRISE, ("company_name", "goods", "route"), (),
                     "Vos marchandises couvertes pendant le transport, par route, mer ou air."),
    "AUTRE": ("Autre besoin d'assurance", PARTICULIER, ("description",), (), ""),
}
DEFAULT_PRODUCTS = [code for code in BRANCHES if code != "AUTRE"]  # liste complète (choix du 09/10)
SKU_PREFIX = "ASSUR-"

STATUS_DRAFT = "DRAFT"           # Bob réunit encore les informations
STATUS_SUBMITTED = "SUBMITTED"   # transmise au cabinet, à traiter
STATUS_HANDLED = "HANDLED"       # prise en charge par le cabinet
STATUS_LABELS = {STATUS_DRAFT: "En cours de qualification", STATUS_SUBMITTED: "Nouvelle", STATUS_HANDLED: "Prise en charge"}


def branch_label(code: str | None) -> str:
    return BRANCHES.get(code or "", ("Assurance",))[0]


def _clean_value(key: str, value):
    """Valeur vérifiée, ou None si elle est absente ou invalide (jamais devinée)."""
    label, kind, limit = FIELDS[key]
    if value is None:
        return None
    if kind == "int":
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if 0 <= number <= 100000 else None
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:limit]
    return cleaned or None


def missing_fields(branch: str, client_type: str | None, details: dict) -> list[str]:
    needed = list(BRANCHES[branch][2])
    if client_type == ENTREPRISE and "company_name" not in needed:
        needed.insert(0, "company_name")
    return [key for key in needed if details.get(key) in (None, "")]


def missing_labels(keys: list[str]) -> list[str]:
    return [FIELDS[key][0] for key in keys]


async def open_request(db, tenant_id, customer_id, branch: str):
    """La demande en cours pour cette branche (brouillon ou transmise, non prise en charge)."""
    from app.models.quote_request import QuoteRequest

    return (await db.execute(select(QuoteRequest).where(
        QuoteRequest.tenant_id == tenant_id, QuoteRequest.customer_id == customer_id, QuoteRequest.branch == branch,
        QuoteRequest.status.in_((STATUS_DRAFT, STATUS_SUBMITTED)),
    ).order_by(QuoteRequest.created_at.desc()).limit(1))).scalar_one_or_none()


async def update_request(db, tenant_id, customer_id, conversation_id, values: dict, now: datetime | None = None) -> dict:
    """
    Enregistre ce que le client a dit pour UNE branche. Renvoie :
    {"request": QuoteRequest, "submitted_now": bool, "missing": [libellés], "rejected": [champs], "error"?}.
    La demande passe à « transmise » une seule fois, dès que le minimum de la branche est réuni.
    """
    from app.models.quote_request import QuoteRequest

    branch = values.get("branch")
    if branch not in BRANCHES:
        return {"error": "Branche inconnue : " + ", ".join(BRANCHES)}
    client_type = values.get("client_type")
    if client_type is not None and client_type not in CLIENT_TYPES:
        return {"error": "Type de client invalide : PARTICULIER ou ENTREPRISE"}
    request = await open_request(db, tenant_id, customer_id, branch)
    if request is None:
        request = QuoteRequest(tenant_id=tenant_id, customer_id=customer_id, conversation_id=conversation_id,
                               branch=branch, client_type=client_type or BRANCHES[branch][1], details={}, status=STATUS_DRAFT)
        db.add(request)
    elif client_type is not None:
        request.client_type = client_type
    details = dict(request.details or {})
    rejected = []
    for key in FIELDS:
        if values.get(key) is None:
            continue
        cleaned = _clean_value(key, values[key])
        if cleaned is None:
            rejected.append(key)
        else:
            details[key] = cleaned
    request.details = details
    request.conversation_id = conversation_id or request.conversation_id
    missing = missing_fields(branch, request.client_type, details)
    submitted_now = False
    if not missing and request.status == STATUS_DRAFT:
        request.status = STATUS_SUBMITTED
        request.submitted_at = now or datetime.now(timezone.utc)
        submitted_now = True
    await db.flush()
    return {"request": request, "submitted_now": submitted_now, "missing": missing_labels(missing), "rejected": rejected}


def request_lines(request) -> list[str]:
    details = request.details or {}
    lines = [f"Assurance : {branch_label(request.branch)}", f"Type de client : {CLIENT_TYPES.get(request.client_type, '—')}"]
    for key, (label, _, _) in FIELDS.items():
        if details.get(key) not in (None, ""):
            lines.append(f"{label} : {details[key]}")
    return lines


def quote_email(tenant, customer, request, link: str) -> tuple[str, str]:
    """Email au cabinet : nouvelle demande de cotation (jamais de prix : c'est au cabinet de proposer)."""
    from app.services.handoff_service import customer_display_name

    who = customer_display_name(customer)
    subject = f"Nouvelle demande de cotation — {branch_label(request.branch)} — {who}"
    body = "\n".join([
        "Bonjour,",
        "",
        f"Bob a qualifié une nouvelle demande de cotation pour {tenant.name}.",
        "",
        f"Client : {who}",
        *request_lines(request),
        "",
        "Bob a proposé au client un appel ou un rendez-vous au cabinet. Il ne lui a donné aucun prix.",
        "",
        f"Voir la conversation : {link}",
        "",
        "— Bob",
    ])
    return subject, body


# --- Garde-fou : jamais de montant ----------------------------------------------------------------

_CURRENCY = r"(?:€|eur\b|euros?\b|fcfa\b|f\s?cfa\b|cfa\b|xof\b|francs?\b|\$|usd\b|dollars?\b)"
_NUMBER = r"\d[\d\s.,]*\d|\d"
_AMOUNT_PATTERNS = [
    re.compile(rf"(?:{_NUMBER})\s?(?:k|m|millions?|milliards?|mille)?\s?{_CURRENCY}"),   # « 85 000 FCFA », « 2 M FCFA »
    re.compile(rf"{_CURRENCY}\s?(?:{_NUMBER})"),                                           # « € 120 », « $ 50 »
    re.compile(r"(?:\d+(?:[.,]\d+)?)\s?%"),                                                  # « 20 % »
    re.compile(r"\b(?:\d+(?:[.,]\d+)?)\s?(?:millions?|milliards?)\b"),                       # « 2 millions »
    # « prime de 85 000 », « à partir de 50 000 », « franchise : 100 000 », « plafond 5 000 000 »
    # « 140 000 », « 1.500.000 » : un nombre écrit par milliers est un montant (sauf des kilomètres).
    re.compile(r"(?<![\d+])\d{1,3}(?:[ .]\d{3})+(?![\d.]*\s?(?:km|kilom))"),
    re.compile(r"\b(?:primes?|cotisations?|tarifs?|co[uû]ts?|prix|devis|franchises?|plafonds?|capital|indemnit\w*|"
               r"rembours\w*|montants?|[àa] partir de|environ|seulement)\b[^.!?\n]{0,25}?\d[\d\s.,]{2,}"),
]

AMOUNT_FALLBACK = ("Le montant dépend de votre situation : je ne peux pas vous donner de prix ici. Votre conseiller "
                   "vous fera une proposition personnalisée, sans engagement. Préférez-vous qu'il vous appelle, ou "
                   "un rendez-vous au cabinet ?")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return text.replace("’", "'").replace(" ", " ").replace("\xa0", " ")


def contains_amount(text: str) -> bool:
    normalized = _normalize(text)
    return any(pattern.search(normalized) for pattern in _AMOUNT_PATTERNS)


def amount_correction() -> str:
    return ("\n\nCONSIGNE (correction) : ta réponse contient un montant, un pourcentage ou un prix. Un cabinet "
            "d'assurance ne donne jamais de prime, de tarif, de franchise ni de montant de garantie sur WhatsApp : "
            "réécris ta réponse SANS AUCUN chiffre de ce type, explique que le conseiller fera une proposition "
            "personnalisée et propose un appel ou un rendez-vous au cabinet.")


# --- Produits par défaut ---------------------------------------------------------------------------

async def seed_products(db, tenant) -> int:
    """Crée les produits d'assurance par défaut d'un nouveau cabinet (jamais deux fois, jamais de prix)."""
    from sqlalchemy import func

    from app.models.product import Product

    from app.repositories.category_repository import CategoryRepository

    existing = (await db.execute(select(func.count(Product.id)).where(Product.tenant_id == tenant.id))).scalar_one()
    if existing:
        return 0
    categories = CategoryRepository(db)
    for code in DEFAULT_PRODUCTS:
        label, client_type, _, _, description = BRANCHES[code]
        category = await categories.get_or_create_by_name(tenant.id, "Particuliers" if client_type == PARTICULIER else "Entreprises")
        db.add(Product(tenant_id=tenant.id, sku=f"{SKU_PREFIX}{code}", name=label, description=description,
                       price=0, currency=tenant.currency or "XOF", stock_quantity=0, active=True, category_id=category.id))
    await db.flush()
    return len(DEFAULT_PRODUCTS)


async def quote_alert_emails(db, tenant, customer, conversation, now: datetime | None = None) -> list[dict]:
    """Demandes de cotation transmises et pas encore annoncées : email au cabinet (et au commercial), une seule fois."""
    from sqlalchemy import update

    from app.models.quote_request import QuoteRequest
    from app.services.handoff_service import alert_emails, commercial_for_customer, conversation_link

    if tenant is None or customer is None or tenant.business_type != "INSURANCE_BROKER":
        return []
    now = now or datetime.now(timezone.utc)
    pending = (await db.execute(select(QuoteRequest).where(
        QuoteRequest.tenant_id == tenant.id, QuoteRequest.customer_id == customer.id,
        QuoteRequest.status == STATUS_SUBMITTED, QuoteRequest.notified_at.is_(None),
    ))).scalars().all()
    emails = []
    for request in pending:
        claimed = await db.execute(
            update(QuoteRequest).where(QuoteRequest.id == request.id, QuoteRequest.notified_at.is_(None))
            .values(notified_at=now).execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            continue  # déjà annoncée par un autre traitement
        request.notified_at = now
        subject, body = quote_email(tenant, customer, request, conversation_link(conversation))
        emails += alert_emails(tenant.email, await commercial_for_customer(db, customer), subject, body)
    return emails
