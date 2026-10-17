"""
Lot 65 — pré-inscription : recruter les premiers utilisateurs avant le lancement.

- Un interrupteur de la plateforme (Super Admin) : tant qu'il est allumé, toute nouvelle inscription (inscription
  guidée, ou démo gardée) crée un compte PRÉ-INSCRIT. Les comptes existants ne changent pas.
- Un compte pré-inscrit est verrouillé côté serveur (app/core/security.get_current_user) : il ne voit que l'accueil
  d'exemple de son secteur, sa fenêtre de pré-inscription (« Mes informations », fichiers à préparer) et peut se
  déconnecter. Rien d'autre ne répond, et Bob ne travaille pas pour lui.
- L'offre de lancement est figée sur le compte à la pré-inscription (installation offerte, abonnement à −50 %
  pendant 3 mois à partir de l'ouverture de l'accès) : elle sera appliquée sans oubli au lancement.
- Le Super Admin ouvre l'accès complet compte par compte (bêta-testeurs) ou à tous les pré-inscrits (lancement).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE, REAL_ESTATE, normalize

logger = logging.getLogger(__name__)

OFFER_CODE = "LANCEMENT_2026"
OFFERS = {OFFER_CODE: {"setup_free": True, "discount_pct": 50, "months": 3}}
OFFER_TEXT = ("Frais d'installation offerts, et vos 3 premiers mois d'abonnement à moitié prix (−50\u00a0%), "
              "à partir de l'ouverture de votre accès.")
LOCKED_MESSAGE = "Votre compte est pré-inscrit : l'accès complet à Bob ouvrira au lancement."

CATALOG_SIZES = ["1 à 20", "21 à 100", "101 à 500", "Plus de 500"]
FILE_KINDS = ("PRODUCTS", "CUSTOMERS")
FILE_MAX_BYTES = 5 * 1024 * 1024
FILE_EXTENSIONS = (".xlsx", ".xls", ".csv")

# Ce qu'un compte pré-inscrit peut encore appeler (tout le reste répond 403).
ALLOWED_PREFIXES = ("/api/v1/auth/", "/api/v1/preregistration/")
ALLOWED_GET = {"/api/v1/tenants/me", "/api/v1/tenants/me/business-type", "/api/v1/analytics/home",
               "/api/v1/notifications/counts"}
ALLOWED_PUT = {"/api/v1/tenants/me/business-type"}  # choix du secteur s'il n'a pas été fait à l'inscription


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_locked(tenant) -> bool:
    return tenant is not None and tenant.prereg_at is not None and tenant.access_opened_at is None


def api_path(path: str) -> str:
    """Chemin de l'API sans le préfixe éventuel du serveur (…/bob/api/v1/… → /api/v1/…)."""
    index = path.find("/api/v1/")
    return path[index:] if index >= 0 else path


def allowed(method: str, path: str) -> bool:
    path = api_path(path).rstrip("/") or "/"
    if any((path + "/").startswith(prefix) for prefix in ALLOWED_PREFIXES):
        return True
    if method in ("GET", "HEAD") and path in ALLOWED_GET:
        return True
    return method == "PUT" and path in ALLOWED_PUT


async def settings_row(db):
    from app.models.preregistration import PlatformSettings

    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1, prereg_mode=False)
        db.add(row)
        await db.flush()
    return row


async def mode_on(db) -> bool:
    from app.models.preregistration import PlatformSettings

    row = await db.get(PlatformSettings, 1)
    return bool(row and row.prereg_mode)


DEFAULT_SLOTS = 100


async def slots(db) -> dict:
    """Lot 69 — places de l'offre de lancement : les N premiers comptes qui l'ont reçue (bêta-testeurs compris)."""
    from sqlalchemy import func, select

    from app.models.preregistration import PlatformSettings
    from app.models.tenant import Tenant

    row = await db.get(PlatformSettings, 1)
    total = row.launch_slots if row is not None and row.launch_slots is not None else DEFAULT_SLOTS
    used = (await db.execute(select(func.count(Tenant.id)).where(Tenant.launch_offer.is_not(None)))).scalar_one()
    return {"total": total, "used": used, "left": max(total - used, 0)}


async def on_signup(db, tenant) -> bool:
    """
    À la création d'un compte (avant le commit) : pré-inscrit si le mode est allumé. Retourne True si c'est le cas.
    Lot 69 — l'offre de lancement seulement tant qu'il reste des places ; après, pré-inscrit sans l'offre.
    """
    if not await mode_on(db):
        return False
    tenant.prereg_at = now_utc()
    if (await slots(db))["left"] > 0:
        tenant.launch_offer = OFFER_CODE
    return True


# --- Contenu de la fenêtre (selon le secteur) ------------------------------------------------------

CONTENT = {
    ONLINE_STORE: {
        "does": ["Il répond à vos clients sur WhatsApp, tout de suite, de jour comme de nuit.",
                 "Il présente vos produits avec leurs prix et leur stock, et conseille le bon article.",
                 "Il prend la commande, envoie le lien de paiement et relance les commandes non payées.",
                 "Il vous passe la main dès qu'un client a besoin de vous."],
        "products": "Le fichier de vos produits (Excel ou CSV) : nom, prix, stock, description, et une photo si possible.",
    },
    CAR_DEALERSHIP: {
        "does": ["Il répond sur WhatsApp à chaque demande, même le soir et le week-end.",
                 "Il renseigne sur vos véhicules : prix, disponibilité, options.",
                 "Il qualifie le projet du client (budget, reprise, financement) et fixe les essais.",
                 "Il rappelle le rendez-vous au client et vous passe la main au bon moment."],
        "products": "La liste de vos véhicules (Excel ou CSV) : marque, modèle, année, kilométrage, prix, photo si possible.",
    },
    INSURANCE_BROKER: {
        "does": ["Il répond sur WhatsApp à chaque demande, même en dehors des heures du cabinet.",
                 "Il qualifie le besoin et prépare la demande de cotation, sans jamais donner de prix.",
                 "Il propose un appel ou un rendez-vous avec votre conseiller.",
                 "Il rappelle les échéances des contrats et enregistre les réclamations (CIMA)."],
        "products": "La liste de vos contrats en cours (Excel ou CSV) : client, téléphone, branche, assureur, échéance.",
    },
    REAL_ESTATE: {  # lot 79
        "does": ["Il répond sur WhatsApp à chaque demande, même le soir et le week-end.",
                 "Il présente vos biens en photos, avec leurs informations clés et vos honoraires.",
                 "Il qualifie acheteurs, locataires et propriétaires, et fixe visites et estimations.",
                 "Il ne demande jamais de pièce interdite au dossier locataire et vous passe la main au bon moment."],
        "products": "La liste de vos biens (Excel ou CSV) : type, vente ou location, ville, surface, pièces, prix, photo si possible.",
    },
}
GAINS = ["Plus aucun client sans réponse : Bob est là 24 h / 24, 7 jours sur 7.",
         "Du temps rendu à votre équipe : les questions répétitives sont traitées par Bob.",
         "Plus de ventes : chaque demande est suivie et relancée au bon moment.",
         "Une vision claire chaque mois : un rapport de ce que Bob a fait pour vous."]


def content(sector: str | None) -> dict:
    sector = normalize(sector)
    entry = CONTENT[sector]
    return {
        "does": entry["does"],
        "gains": GAINS,
        "prepare": [
            "Un numéro de téléphone pour le WhatsApp de votre entreprise.",
            entry["products"],
            "Le fichier de vos clients si vous en avez un (Excel ou CSV) : nom et téléphone.",
        ],
        "support": "Dès le lancement, nous vous accompagnons pour l'installation : vous n'aurez rien à faire seul.",
        "offer": OFFER_TEXT,
        "signature": "L'équipe de Bob",
        "products_label": {ONLINE_STORE: "Fichier de vos produits", CAR_DEALERSHIP: "Liste de vos véhicules",
                           INSURANCE_BROKER: "Liste de vos contrats", REAL_ESTATE: "Liste de vos biens"}[sector],
        "size_label": {ONLINE_STORE: "Nombre de produits", CAR_DEALERSHIP: "Nombre de véhicules",
                       INSURANCE_BROKER: "Nombre de contrats", REAL_ESTATE: "Nombre de biens"}[sector],
    }


# --- Accueil d'exemple (chiffres fictifs, marqués « Exemple ») ----------------------------------------

def _days(now: datetime, counts: list[int], sales: set, appointments: set) -> list[dict]:
    start = (now - timedelta(days=len(counts) - 1)).date()
    return [{"date": (start + timedelta(days=i)).isoformat(), "conversations": n, "sale": i in sales,
             "appointment": i in appointments} for i, n in enumerate(counts)]


_COUNTS = [9, 12, 8, 14, 11, 6, 5, 13, 15, 12, 10, 16, 7, 6, 14, 17, 13, 12, 18, 9, 8, 15, 19, 16, 14, 20, 11, 10, 17, 21]


def example_home(sector: str | None, first_name: str | None, currency: str | None, now: datetime | None = None) -> dict:
    """Même forme que /api/v1/analytics/home, avec des chiffres d'exemple : l'accueil réel, en vitrine."""
    now = now or now_utc()
    sector = normalize(sector)
    ago = lambda **kw: (now - timedelta(**kw)).isoformat()  # noqa: E731
    base = {"business_type": sector, "first_name": first_name, "period_days": 30, "currency": currency or "XOF",
            "generated_at": now.isoformat(), "ai_messages": 1184, "waiting_humans": 1, "example": True}
    if sector == ONLINE_STORE:
        return {**base,
                "kpis": {"paid": {"value": 2_485_000.0, "delta_pct": 18.0},
                         "pending": {"value": 312_000.0, "count": 6},
                         "conversion": {"value": 34.5, "delta_points": 4.2, "paid": 48, "terminated": 139},
                         "conversations": {"value": 386, "delta_pct": 22.0, "bob_only": 351}},
                "todo": [
                    {"kind": "CONVERSATION", "conversation_id": None, "customer": "Awa Koné", "reason": "Demande un humain",
                     "tone": "danger", "detail": "Je voudrais passer récupérer la robe ce soir, c'est possible ?",
                     "since": ago(minutes=12), "action": "Répondre", "hot": True},
                    {"kind": "ORDER", "customer": "Fatou Diallo", "reason": "Paiement à confirmer", "tone": "warning",
                     "detail": "Commande de 45 000 — capture d'écran du paiement reçue", "since": ago(minutes=40),
                     "action": "Voir la commande"}],
                "funnel": [{"key": "conversations", "label": "Conversations", "value": 386},
                           {"key": "opportunities", "label": "Opportunités de vente", "value": 162},
                           {"key": "orders", "label": "Commandes", "value": 71},
                           {"key": "paid", "label": "Payées", "value": 48}],
                "daily": _days(now, _COUNTS, {2, 5, 9, 12, 15, 18, 20, 23, 25, 27, 29}, set()),
                "activity": [
                    {"kind": "PAYMENT", "title": "Paiement confirmé", "detail": "Fatou Diallo · 45 000", "at": ago(minutes=38)},
                    {"kind": "ORDER", "title": "Nouvelle commande", "detail": "Yao Kouassi · 2 articles · 28 500", "at": ago(hours=2)},
                    {"kind": "CUSTOMER", "title": "Nouveau client", "detail": "Mariam Traoré · venue d'une publicité Facebook", "at": ago(hours=3)},
                    {"kind": "ORDER", "title": "Commande relancée par Bob, puis payée", "detail": "Koffi N'Guessan · 19 000", "at": ago(hours=6)},
                ]}
    if sector == CAR_DEALERSHIP:
        return {**base,
                "kpis": {"appointments": {"value": 37, "delta_pct": 23.0, "to_confirm": 3},
                         "sold": {"value": 9, "delta_pct": 12.5},
                         "appointment_rate": {"value": 15.4, "delta_points": 2.1, "with_appointment": 37, "conversations": 241},
                         "conversations": {"value": 241, "delta_pct": 19.0, "bob_only": 214}},
                "todo": [
                    {"kind": "APPOINTMENT", "customer": "Serge Kouadio", "reason": "Essai à confirmer", "tone": "warning",
                     "detail": "Toyota RAV4 2021 · samedi matin · reprise d'une Corolla", "since": ago(minutes=25),
                     "action": "Confirmer"},
                    {"kind": "CONVERSATION", "conversation_id": None, "customer": "Aïcha Bamba", "reason": "Demande un humain",
                     "tone": "danger", "detail": "Quel taux pour un financement sur 36 mois ?", "since": ago(hours=1), "action": "Répondre",
                     "hot": True}],
                "funnel": [{"key": "conversations", "label": "Conversations", "value": 241},
                           {"key": "appointments", "label": "Rendez-vous demandés", "value": 37},
                           {"key": "visited", "label": "Venus au rendez-vous", "value": 26},
                           {"key": "sold", "label": "Vendus", "value": 9}],
                "daily": _days(now, [round(n * 0.65) for n in _COUNTS], {6, 13, 19, 24, 28}, {1, 3, 4, 8, 10, 11, 15, 17, 20, 22, 25, 26, 29}),
                "activity": [
                    {"kind": "APPOINTMENT", "title": "Rendez-vous demandé", "detail": "Serge Kouadio · Essai · Toyota RAV4 2021", "at": ago(minutes=25)},
                    {"kind": "SALE", "title": "Vente conclue", "detail": "Jean-Marc Yao · Hyundai Tucson 2022", "at": ago(hours=5)},
                    {"kind": "CUSTOMER", "title": "Nouveau client", "detail": "Aïcha Bamba · venue d'une publicité Instagram", "at": ago(hours=7)},
                ]}
    if sector == REAL_ESTATE:  # lot 79
        return {**base,
                "kpis": {"appointments": {"value": 41, "delta_pct": 18.0, "to_confirm": 4},
                         "sold": {"value": 7, "delta_pct": 16.7},
                         "appointment_rate": {"value": 17.2, "delta_points": 2.4, "with_appointment": 41, "conversations": 238},
                         "conversations": {"value": 238, "delta_pct": 21.0, "bob_only": 209}},
                "todo": [
                    {"kind": "APPOINTMENT", "customer": "Claire Martin", "reason": "Estimation à confirmer", "tone": "warning",
                     "detail": "Maison 5 pièces à Villeurbanne · vente sous 3 mois", "since": ago(minutes=20),
                     "action": "Confirmer"},
                    {"kind": "CONVERSATION", "conversation_id": None, "customer": "Karim Benali", "reason": "Demande un humain",
                     "tone": "danger", "detail": "Je voudrais faire une offre sur le T3 de la rue Garibaldi",
                     "since": ago(hours=1), "action": "Répondre", "hot": True}],
                "funnel": [{"key": "conversations", "label": "Conversations", "value": 238},
                           {"key": "appointments", "label": "Rendez-vous demandés", "value": 41},
                           {"key": "visited", "label": "Rendez-vous honorés", "value": 29},
                           {"key": "sold", "label": "Affaires conclues", "value": 7}],
                "daily": _days(now, [round(n * 0.65) for n in _COUNTS], {5, 12, 19, 26}, {1, 2, 4, 7, 9, 11, 14, 16, 18, 21, 23, 25, 28}),
                "activity": [
                    {"kind": "APPOINTMENT", "title": "Rendez-vous demandé", "detail": "Claire Martin · Estimation de votre bien", "at": ago(minutes=20)},
                    {"kind": "SALE", "title": "Affaire conclue", "detail": "Julien Petit · Appartement T2 Lyon 7e", "at": ago(hours=6)},
                    {"kind": "APPOINTMENT", "title": "Rendez-vous demandé", "detail": "Sophie Bernard · Visite · Appartement T3 Lyon 3e", "at": ago(hours=9)},
                ],
                "prospect_scores": {"CHAUD": 9, "TIEDE": 17, "FROID": 12, "VENDU": 7}}
    return {**base,
            "kpis": {"appointments": {"value": 22, "delta_pct": 15.0, "to_confirm": 2},
                     "sold": {"value": 11, "delta_pct": 22.0},
                     "appointment_rate": {"value": 12.6, "delta_points": 1.8, "with_appointment": 22, "conversations": 175},
                     "conversations": {"value": 175, "delta_pct": 17.0, "bob_only": 158},
                     "quotes": {"value": 34, "delta_pct": 26.0, "to_handle": 4}},
            "todo": [
                {"kind": "QUOTE", "customer": "Mariam Traoré", "reason": "Cotation à traiter", "tone": "warning",
                 "detail": "Assurance automobile · Toyota Corolla 2019 · tous risques", "since": ago(minutes=30),
                 "action": "Voir la demande"},
                {"kind": "RENEWAL", "customer": "Koffi N'Guessan", "reason": "Échéance dans 21 jours", "tone": "info",
                 "detail": "Assurance habitation · SUNU Assurances", "since": (now + timedelta(days=21)).isoformat(),
                 "action": "Voir le contrat"}],
            "funnel": [{"key": "conversations", "label": "Conversations", "value": 175},
                       {"key": "quotes", "label": "Demandes de cotation", "value": 34},
                       {"key": "appointments", "label": "Rendez-vous et appels", "value": 22},
                       {"key": "sold", "label": "Contrats souscrits", "value": 11}],
            "daily": _days(now, [round(n * 0.5) for n in _COUNTS], {7, 14, 21, 27}, {2, 5, 9, 12, 16, 19, 23, 26, 28}),
            "activity": [
                {"kind": "QUOTE", "title": "Demande de cotation transmise", "detail": "Mariam Traoré · Assurance automobile", "at": ago(minutes=30)},
                {"kind": "SALE", "title": "Contrat souscrit", "detail": "Adjoua Kouamé · Assurance santé", "at": ago(hours=4)},
                {"kind": "APPOINTMENT", "title": "Rendez-vous demandé", "detail": "Ibrahim Cissé · Appel", "at": ago(hours=8)},
            ],
            "prospect_scores": {"CHAUD": 6, "TIEDE": 11, "FROID": 9, "VENDU": 11},
            "expiries": [], "expiries_total": 0}


# --- Vue du compte, informations préparées, fichiers -------------------------------------------------

async def files_of(db, tenant_id) -> list:
    from app.models.preregistration import PreregistrationFile

    return (await db.execute(select(PreregistrationFile).where(PreregistrationFile.tenant_id == tenant_id)
                             .order_by(PreregistrationFile.uploaded_at))).scalars().all()


def file_view(f) -> dict:
    return {"id": str(f.id), "kind": f.kind, "filename": f.filename, "size": f.size,
            "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None}


def prep_view(tenant, files) -> dict:
    return {"whatsapp": tenant.prep_whatsapp, "catalog_size": tenant.prep_catalog_size, "note": tenant.prep_note,
            "updated_at": tenant.prep_updated_at.isoformat() if tenant.prep_updated_at else None,
            "files": [file_view(f) for f in files]}


def clean_phone(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 8 or len(digits) > 15:
        raise ValueError("Numéro WhatsApp invalide : indiquez-le avec l'indicatif du pays (ex. +225 07 00 00 00 00).")
    return ("+" if value.startswith("+") or value.startswith("00") else "") + digits.removeprefix("00")


async def save_file(db, tenant, kind: str, filename: str, content_type: str | None, data: bytes):
    from app.models.preregistration import PreregistrationFile

    if kind not in FILE_KINDS:
        raise ValueError("Type de fichier inconnu.")
    name = (filename or "fichier").replace("\\", "/").rsplit("/", 1)[-1][:200]
    if not name.lower().endswith(FILE_EXTENSIONS):
        raise ValueError("Fichier Excel (.xlsx, .xls) ou CSV seulement.")
    if not data:
        raise ValueError("Le fichier est vide.")
    if len(data) > FILE_MAX_BYTES:
        raise ValueError("Fichier trop lourd : 5 Mo au plus.")
    for old in await files_of(db, tenant.id):  # un seul fichier par type : le nouveau remplace l'ancien
        if old.kind == kind:
            await db.delete(old)
    record = PreregistrationFile(tenant_id=tenant.id, kind=kind, filename=name,
                                 content_type=(content_type or "application/octet-stream")[:120], size=len(data), data=data)
    db.add(record)
    tenant.prep_updated_at = now_utc()
    return record


# --- Emails ------------------------------------------------------------------------------------------

def _dashboard_url() -> str:
    from app.core.config import get_settings

    return get_settings().public_base_url.rstrip("/") + "/dashboard/"


def welcome_email(tenant, first_name: str | None) -> tuple[str, str]:
    c = content(tenant.business_type)
    lines = [f"Bonjour {first_name}," if first_name else "Bonjour,", "",
             f"Merci ! La pré-inscription de {tenant.name} à Bob est confirmée.", "",
             *(["Votre avantage de pré-inscrit :", f"- {OFFER_TEXT}", ""] if tenant.launch_offer else []),
             "À préparer avant le lancement :", *[f"- {line}" for line in c["prepare"]], "",
             c["support"], "",
             f"Vous pouvez dès maintenant découvrir votre tableau de bord et nous transmettre ces informations : {_dashboard_url()}",
             "", "À très vite,", "L'équipe de Bob"]
    return f"Pré-inscription confirmée — {tenant.name}", "\n".join(lines)


def opened_email(tenant, first_name: str | None) -> tuple[str, str]:
    offer = f"\n\nVotre avantage de pré-inscrit reste acquis : {OFFER_TEXT}" if tenant.launch_offer else ""
    body = ((f"Bonjour {first_name}," if first_name else "Bonjour,") + "\n\n"
            f"Bonne nouvelle : l'accès complet de {tenant.name} à Bob est ouvert. Connectez-vous pour installer Bob : "
            f"{_dashboard_url()}{offer}\n\nNous vous accompagnons pour l'installation : répondez simplement à cet email.\n\n"
            "À très vite,\nL'équipe de Bob")
    return f"Votre accès à Bob est ouvert — {tenant.name}", body


def alert_email(tenant, owner_email: str | None) -> tuple[str, str]:
    from app.core.config import get_settings

    base = get_settings().public_base_url.rstrip("/")
    from app.services.business_type import BUSINESS_TYPES

    sector = BUSINESS_TYPES[normalize(tenant.business_type)]["label"]
    body = (f"Nouvelle pré-inscription : {tenant.name} ({sector}, {tenant.country}).\n"
            f"Contact : {owner_email or tenant.email}" + (f" · {tenant.phone}" if tenant.phone else "") +
            f"\n\nVoir les pré-inscrits : {base}/superadmin/\n\n— Bob")
    return f"Nouvelle pré-inscription — {tenant.name}", body


async def send_signup_emails(db, tenant, owner, send=None) -> None:
    """Confirmation au client et alerte aux Super Admins ; un échec d'envoi ne bloque jamais l'inscription."""
    from app.models.superadmin_user import SuperAdminUser

    if send is None:
        from app.services.email_service import send_email as send
    first = (owner.full_name or "").split()[0] if owner and (owner.full_name or "").strip() else None
    try:
        subject, body = welcome_email(tenant, first)
        send(to=owner.email if owner else tenant.email, subject=subject, body=body)
    except Exception:  # noqa: BLE001
        logger.warning("Email de pré-inscription impossible (boutique %s)", tenant.id)
    try:
        subject, body = alert_email(tenant, owner.email if owner else None)
        for email in (await db.execute(select(SuperAdminUser.email).where(SuperAdminUser.active.is_(True)))).scalars().all():
            send(to=email, subject=subject, body=body)
    except Exception:  # noqa: BLE001
        logger.warning("Alerte de pré-inscription impossible (boutique %s)", tenant.id)


async def owner_of(db, tenant):
    from app.models.user import Role, User

    return (await db.execute(select(User).where(User.tenant_id == tenant.id, User.role == Role.OWNER)
                             .order_by(User.created_at).limit(1))).scalar_one_or_none()


async def open_access(db, tenant, send=None) -> bool:
    """Ouvre l'accès complet (bêta ou lancement) ; un email prévient le propriétaire. False si déjà ouvert."""
    if not is_locked(tenant):
        return False
    tenant.access_opened_at = now_utc()
    if send is None:
        from app.services.email_service import send_email as send
    owner = await owner_of(db, tenant)
    first = (owner.full_name or "").split()[0] if owner and (owner.full_name or "").strip() else None
    try:
        subject, body = opened_email(tenant, first)
        send(to=owner.email if owner else tenant.email, subject=subject, body=body)
    except Exception:  # noqa: BLE001
        logger.warning("Email d'ouverture d'accès impossible (boutique %s)", tenant.id)
    return True
