"""
Lot 61 — prospection d'AgenC'AI (Super Admin → Prospection), choix du 10/10 : 1A (suivi et canaux manuels ;
emails automatiques au lot 62), 2A (le lien mène à la page de présentation), 3A (Super Admins seulement).

Parcours suivi, étape par étape :
  ajouté → contacté → a cliqué → démo essayée → compte créé → activé → payant
- « contacté » : noté à la main (appel, WhatsApp depuis son téléphone, visite, salon, email…) ;
- « a cliqué » : le lien personnel /p/CODE (les robots d'aperçu de lien — WhatsApp, Facebook, Slack… — ne
  comptent pas) ; il dépose le code dans le navigateur pendant 30 jours ;
- « démo » et « compte créé » : la démo et l'inscription faites avec ce code (ou, pour l'inscription, avec
  l'email du prospect, ou depuis sa démo) ;
- « activé » : la boutique a connecté son numéro WhatsApp et reçu un premier vrai message client ;
- « payant » : le Super Admin a renseigné « payé jusqu'au ».
Rien n'est jamais envoyé au prospect par Bob dans ce lot (pas de WhatsApp automatique : interdit par Meta).
"""
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

COOKIE = "bob_p"
COOKIE_DAYS = 30
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # sans 0/o, 1/l/i : lisible à voix haute
CODE_LENGTH = 6
FOLLOW_UP_DAYS = 4        # contacté sans clic depuis 4 jours : à relancer
RETENTION_DAYS = 365      # un prospect resté sans réaction est effacé au bout d'un an

STAGES = [
    ("ADDED", "Ajouté"), ("CONTACTED", "Contacté"), ("CLICKED", "A cliqué"), ("DEMO", "Démo essayée"),
    ("SIGNED_UP", "Compte créé"), ("ACTIVATED", "Activé"), ("PAID", "Payant"),
]
STAGE_INDEX = {key: i for i, (key, _) in enumerate(STAGES)}
CHANNELS = {"WHATSAPP": "WhatsApp", "PHONE": "Appel", "EMAIL": "Email", "SMS": "SMS", "VISIT": "Visite",
            "EVENT": "Salon, événement", "OTHER": "Autre"}
STATUSES = {"ACTIVE": "En cours", "NOT_INTERESTED": "Pas intéressé", "UNSUBSCRIBED": "Ne plus contacter",
            "INVALID": "Coordonnées invalides"}
SECTORS = {"ONLINE_STORE": "Commerce", "CAR_DEALERSHIP": "Concession automobile",
           "INSURANCE_BROKER": "Courtier / agent d'assurance"}
EVENT_LABELS = {"ADDED": "Ajouté", "CONTACT": "Contacté", "CLICK": "A cliqué sur son lien", "DEMO": "A essayé la démo",
                "SIGNUP": "A créé son compte", "STATUS": "Statut", "NOTE": "Note"}
# Robots qui ouvrent les liens pour en faire un aperçu : un aperçu n'est pas un clic du prospect.
BOT_AGENTS = re.compile(r"bot|crawl|spider|preview|facebookexternalhit|whatsapp|telegram|slack|discord|skype|"
                        r"embedly|vkshare|curl|wget|python-|go-http|okhttp|headless|lighthouse", re.I)


class ProspectError(ValueError):
    """Refus : message montré tel quel."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def is_bot(user_agent: str | None) -> bool:
    return not user_agent or bool(BOT_AGENTS.search(user_agent))


async def new_code(db) -> str:
    from app.models.prospect import Prospect

    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if (await db.execute(select(Prospect.id).where(Prospect.code == code))).first() is None:
            return code
    raise ProspectError("Impossible de créer un code unique : réessayez")


def clean_email(value: str | None) -> str | None:
    from app.services.insurance_contracts import _email

    value = (value or "").strip()
    if not value:
        return None
    try:
        return _email(value)
    except ValueError:
        raise ProspectError(f"Email invalide : {value[:80]}") from None


def clean_phone(value: str | None, country: str) -> str | None:
    from app.services.insurance_contracts import normalize_phone

    if not (value or "").strip():
        return None
    number = normalize_phone(value, country)
    if number is None:
        raise ProspectError(f"Téléphone invalide : {str(value)[:40]}")
    return number


def clean_fields(data: dict, current=None) -> dict:
    """Champs modifiables d'un prospect, nettoyés ; une valeur absente du dictionnaire ne change rien."""
    from app.services.countries import COUNTRY_CODES

    out = {}
    country = (data.get("country") or getattr(current, "country", None) or "CI").strip().upper()
    if country not in COUNTRY_CODES:
        raise ProspectError("Pays non proposé : choisissez-le dans la liste")
    if "country" in data or current is None:
        out["country"] = country
    if "company" in data or current is None:
        company = " ".join(str(data.get("company") or "").split())
        if not company:
            raise ProspectError("Indiquez le nom de l'entreprise")
        out["company"] = company[:200]
    for key, size in (("contact_name", 200), ("city", 120), ("source", 120)):
        if key in data:
            out[key] = " ".join(str(data.get(key) or "").split())[:size] or None
    if "notes" in data:
        out["notes"] = str(data.get("notes") or "").strip()[:2000] or None
    if "sector" in data or current is None:
        sector = data.get("sector") or "ONLINE_STORE"
        if sector not in SECTORS:
            raise ProspectError("Secteur inconnu")
        out["sector"] = sector
    if "email" in data:
        out["email"] = clean_email(data.get("email"))
    if "phone" in data or ("country" in data and current is not None and current.phone):
        out["phone"] = clean_phone(data.get("phone") if "phone" in data else current.phone, country)
    if "next_action_on" in data:
        value = data.get("next_action_on")
        out["next_action_on"] = date.fromisoformat(value) if value else None
    return out


async def find_duplicate(db, email: str | None, phone: str | None, exclude=None):
    from app.models.prospect import Prospect

    conditions = []
    if email:
        conditions.append(func.lower(Prospect.email) == email.lower())
    if phone:
        conditions.append(Prospect.phone == phone)
    if not conditions:
        return None
    from sqlalchemy import or_

    stmt = select(Prospect).where(or_(*conditions))
    if exclude is not None:
        stmt = stmt.where(Prospect.id != exclude)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


def add_event(db, prospect, kind: str, detail: str | None = None, channel: str | None = None, actor: str | None = None,
              at: datetime | None = None) -> None:
    from app.models.prospect import ProspectEvent

    db.add(ProspectEvent(prospect_id=prospect.id, kind=kind, channel=channel, detail=(detail or None) and detail[:500],
                         actor=actor, at=at or now_utc()))


# --- Étapes -----------------------------------------------------------------------------------------------

async def tenant_facts(db, tenant_ids: set) -> dict:
    """Boutique → (activée, payante). Activée = numéro WhatsApp connecté et au moins un vrai message client."""
    from app.models.conversation import Message, MessageSender
    from app.models.tenant import Tenant
    from app.models.whatsapp_account import WhatsAppAccount

    ids = {t for t in tenant_ids if t is not None}
    if not ids:
        return {}
    tenants = {t.id: t for t in (await db.execute(select(Tenant).where(Tenant.id.in_(ids)))).scalars().all()}
    connected = set((await db.execute(select(WhatsAppAccount.tenant_id).where(WhatsAppAccount.tenant_id.in_(ids)))).scalars())
    talking = set((await db.execute(select(Message.tenant_id).where(
        Message.tenant_id.in_(ids), Message.sender == MessageSender.CUSTOMER).distinct())).scalars())
    return {tid: (tid in connected and tid in talking and not t.is_demo, t.paid_until is not None and not t.is_demo)
            for tid, t in tenants.items()}


def stage_of(prospect, facts: dict) -> str:
    """Étape la plus avancée atteinte (une étape atteinte compte aussi toutes les précédentes)."""
    activated, paid = facts.get(prospect.tenant_id, (False, False))
    if paid:
        return "PAID"
    if activated:
        return "ACTIVATED"
    if prospect.signed_up_at:
        return "SIGNED_UP"
    if prospect.demo_at:
        return "DEMO"
    if prospect.first_click_at:
        return "CLICKED"
    if prospect.contacted_at:
        return "CONTACTED"
    return "ADDED"


def to_follow_up(prospect, stage: str, today: date, now: datetime | None = None) -> bool:
    """À relancer : date prévue arrivée, ou contacté sans clic depuis 4 jours (jamais un perdu ni un inscrit)."""
    if prospect.status != "ACTIVE" or STAGE_INDEX[stage] >= STAGE_INDEX["SIGNED_UP"]:
        return False
    if prospect.next_action_on is not None:
        return prospect.next_action_on <= today
    last = aware(prospect.last_contact_at)
    return stage == "CONTACTED" and last is not None and last <= (now or now_utc()) - timedelta(days=FOLLOW_UP_DAYS)


def funnel(prospects: list, stages: dict) -> list[dict]:
    counts = [0] * len(STAGES)
    for p in prospects:
        for i in range(STAGE_INDEX[stages[p.id]] + 1):
            counts[i] += 1
    out = []
    for i, (key, label) in enumerate(STAGES):
        previous = counts[i - 1] if i else None
        out.append({"key": key, "label": label, "count": counts[i],
                    "rate_pct": round(counts[i] * 100 / previous) if previous else None})
    return out


def group_funnel(prospects: list, stages: dict, key_of) -> list[dict]:
    groups: dict = {}
    for p in prospects:
        groups.setdefault(key_of(p) or "—", []).append(p)
    rows = []
    for name, items in groups.items():
        counts = {key: sum(1 for p in items if STAGE_INDEX[stages[p.id]] >= i) for key, i in STAGE_INDEX.items()}
        rows.append({"name": name, "total": len(items), **{k.lower(): v for k, v in counts.items()},
                     "signup_rate_pct": round(counts["SIGNED_UP"] * 100 / len(items)) if items else None})
    return sorted(rows, key=lambda r: (-r["signed_up"], -r["total"], r["name"]))


# --- Lien personnel et rattachement de la démo / de l'inscription ------------------------------------------

def link(code: str) -> str:
    from app.core.config import get_settings

    return f"{get_settings().public_base_url.rstrip('/')}/p/{code}"


async def record_click(db, code: str, user_agent: str | None):
    """Le prospect du code (ou None) ; le clic n'est compté que pour un vrai navigateur."""
    from app.models.prospect import Prospect

    code = (code or "").strip().lower()[:16]
    prospect = (await db.execute(select(Prospect).where(Prospect.code == code))).scalar_one_or_none()
    if prospect is None or is_bot(user_agent):
        return prospect
    moment = now_utc()
    prospect.click_count = (prospect.click_count or 0) + 1
    prospect.last_click_at = moment
    if prospect.first_click_at is None:
        prospect.first_click_at = moment
        add_event(db, prospect, "CLICK", actor="prospect", at=moment)
    await db.commit()
    return prospect


async def attach(db, request, tenant_id, kind: str, email: str | None = None) -> None:
    """Démo (DEMO) ou compte (SIGNUP) rattaché au prospect du lien ; ne fait jamais échouer l'inscription."""
    from app.models.prospect import Prospect

    try:
        code = (request.cookies.get(COOKIE) or "").strip().lower() if request is not None else ""
        prospect = None
        if code:
            prospect = (await db.execute(select(Prospect).where(Prospect.code == code))).scalar_one_or_none()
        if prospect is None and kind == "SIGNUP":
            prospect = (await db.execute(select(Prospect).where(Prospect.demo_tenant_id == tenant_id))).scalars().first()
            if prospect is None and email:
                prospect = (await db.execute(select(Prospect).where(
                    func.lower(Prospect.email) == email.strip().lower()))).scalars().first()
        if prospect is None:
            return
        moment = now_utc()
        if kind == "DEMO" and prospect.demo_at is None:
            prospect.demo_at, prospect.demo_tenant_id = moment, tenant_id
            add_event(db, prospect, "DEMO", actor="prospect", at=moment)
        elif kind == "SIGNUP" and prospect.tenant_id is None:
            prospect.signed_up_at, prospect.tenant_id = moment, tenant_id
            add_event(db, prospect, "SIGNUP", actor="prospect", at=moment)
        await db.commit()
    except Exception:  # noqa: BLE001 — le suivi ne bloque jamais une démo ni une inscription
        logger.exception("Rattachement au prospect impossible (%s)", kind)
        await db.rollback()


# --- Message à envoyer soi-même ------------------------------------------------------------------------------

_PITCH = {
    "ONLINE_STORE": "répond à vos clients sur WhatsApp 24 h / 24, prend leurs commandes et relance ceux qui hésitent",
    "CAR_DEALERSHIP": "répond à vos prospects sur WhatsApp 24 h / 24, qualifie leur projet et vous obtient des rendez-vous d'essai",
    "INSURANCE_BROKER": "répond à vos clients sur WhatsApp 24 h / 24, prépare les demandes de cotation et suit les échéances de leurs contrats",
}


def message(prospect, sender: str | None) -> str:
    """Texte prêt à envoyer depuis son propre WhatsApp (ou par SMS, email), avec le lien personnel."""
    first = (prospect.contact_name or "").split()[0] if (prospect.contact_name or "").strip() else None
    who = f"{sender} d'AgenC'AI" if sender else "AgenC'AI"
    pitch = _PITCH.get(prospect.sector, _PITCH["ONLINE_STORE"])
    return (f"Bonjour{' ' + first if first else ''}, je suis {who}. Bob, notre vendeur virtuel, {pitch}. "
            f"Vous pouvez l'essayer en 2 minutes pour {prospect.company} : {link(prospect.code)}")


def whatsapp_url(prospect, sender: str | None) -> str | None:
    if not prospect.phone:
        return None
    from urllib.parse import quote

    return f"https://wa.me/{prospect.phone}?text={quote(message(prospect, sender))}"


# --- Import Excel ----------------------------------------------------------------------------------------------

FIELDS = {"company": "Entreprise", "contact_name": "Contact", "email": "Email", "phone": "Téléphone",
          "city": "Ville", "country": "Pays", "sector": "Secteur", "source": "Source", "notes": "Note"}
SYNONYMS = {
    "company": ("entreprise", "societe", "raison sociale", "nom de l entreprise", "boutique", "commerce", "company",
                "enseigne", "concession", "cabinet", "nom commercial"),
    "contact_name": ("contact", "nom", "nom du contact", "responsable", "gerant", "nom et prenom", "nom et prenoms",
                     "interlocuteur", "dirigeant", "name"),
    "email": ("email", "e mail", "mail", "courriel", "adresse email"),
    "phone": ("telephone", "tel", "tél", "phone", "mobile", "portable", "whatsapp", "numero", "contact telephonique"),
    "city": ("ville", "commune", "localite", "quartier", "city"),
    "country": ("pays", "country"),
    "sector": ("secteur", "activite", "type", "categorie", "secteur d activite"),
    "source": ("source", "origine", "provenance", "canal"),
    "notes": ("note", "notes", "remarque", "remarques", "commentaire", "commentaires", "observation"),
}
SECTOR_WORDS = {"CAR_DEALERSHIP": ("auto", "concession", "vehicule", "voiture", "garage", "moto"),
                "INSURANCE_BROKER": ("assur", "courtier", "courtage", "agent general", "agence generale")}


def _plain(text) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


_LOOKUP = {_plain(word): field for field, words in SYNONYMS.items() for word in words}


def detect_mapping(header: list[str]) -> list[str | None]:
    used, mapping = set(), []
    for name in header:
        field = _LOOKUP.get(_plain(name))
        if field in used:
            field = None
        if field:
            used.add(field)
        mapping.append(field)
    return mapping


def guess_sector(value: str | None, default: str) -> str:
    plain = _plain(value)
    if not plain:
        return default
    if value in SECTORS:
        return value
    for sector, words in SECTOR_WORDS.items():
        if any(w in plain for w in words):
            return sector
    return "ONLINE_STORE"


def guess_country(value: str | None, default: str) -> str:
    from app.services.countries import COUNTRIES, COUNTRY_CODES

    raw = (value or "").strip()
    if not raw:
        return default
    if raw.upper() in COUNTRY_CODES:
        return raw.upper()
    plain = _plain(raw)
    for code, name, _ in COUNTRIES:
        if _plain(name) == plain:
            return code
    return default


def preview(raw: bytes, filename: str, sheet: str | None = None) -> dict:
    from app.services.spreadsheet import SpreadsheetError, read_table

    try:
        table = read_table(raw, filename, sheet)
    except SpreadsheetError as exc:
        raise ProspectError(str(exc)) from None
    mapping = detect_mapping(table["header"])
    return {"sheets": table["sheets"], "sheet": table["sheet"], "header": table["header"], "mapping": mapping,
            "rows": table["rows"][:5], "total": len(table["rows"]),
            "fields": [{"value": k, "label": v} for k, v in FIELDS.items()], "missing_company": "company" not in mapping}


async def import_rows(db, raw: bytes, filename: str, sheet: str | None, mapping: list, defaults: dict, owner_id,
                      actor: str) -> dict:
    """Ajoute les prospects du fichier ; un prospect déjà connu (même email ou téléphone) n'est pas recréé."""
    from app.models.prospect import Prospect
    from app.services.spreadsheet import SpreadsheetError, read_table

    try:
        table = read_table(raw, filename, sheet)
    except SpreadsheetError as exc:
        raise ProspectError(str(exc)) from None
    if not isinstance(mapping, list) or len(mapping) != len(table["header"]):
        raise ProspectError("La correspondance des colonnes ne correspond pas au fichier : recommencez l'aperçu")
    mapping = [m if m in FIELDS else None for m in mapping]
    chosen = [m for m in mapping if m]
    if chosen.count("company") != 1:
        raise ProspectError("Indiquez une (et une seule) colonne Entreprise")
    if len(set(chosen)) != len(chosen):
        raise ProspectError("Chaque information ne peut venir que d'une colonne")
    country_default = (defaults.get("country") or "CI").upper()
    sector_default = defaults.get("sector") if defaults.get("sector") in SECTORS else "ONLINE_STORE"
    source_default = " ".join(str(defaults.get("source") or "").split())[:120] or None
    report = {"created": 0, "duplicates": 0, "errors": [], "total": len(table["rows"])}
    for offset, row in enumerate(table["rows"]):
        line = table["first_line"] + offset
        values = {field: row[i].strip() for i, field in enumerate(mapping) if field and row[i].strip()}
        try:
            country = guess_country(values.get("country"), country_default)
            data = {"company": values.get("company"), "contact_name": values.get("contact_name"),
                    "email": values.get("email"), "phone": values.get("phone"), "city": values.get("city"),
                    "country": country, "sector": guess_sector(values.get("sector"), sector_default),
                    "source": values.get("source") or source_default, "notes": values.get("notes")}
            fields = clean_fields(data)
        except ProspectError as exc:
            report["errors"].append({"line": line, "message": str(exc)})
            continue
        email, phone = fields.get("email"), fields.get("phone")
        if await find_duplicate(db, email, phone) is not None:  # aussi les lignes déjà ajoutées de ce fichier
            report["duplicates"] += 1
            continue
        prospect = Prospect(code=await new_code(db), owner_id=owner_id, status="ACTIVE", **fields)
        db.add(prospect)
        await db.flush()
        add_event(db, prospect, "ADDED", detail=f"Import {filename}"[:500], actor=actor)
        report["created"] += 1
    await db.commit()
    report["errors"] = report["errors"][:100]
    return report


async def purge_stale(db, now: datetime | None = None) -> int:
    """Efface les prospects ajoutés il y a plus d'un an qui n'ont jamais réagi (ni clic, ni démo, ni compte)."""
    from sqlalchemy import delete

    from app.models.prospect import Prospect

    limit = (now or now_utc()) - timedelta(days=RETENTION_DAYS)
    result = await db.execute(delete(Prospect).where(
        Prospect.created_at < limit, Prospect.first_click_at.is_(None), Prospect.demo_at.is_(None),
        Prospect.tenant_id.is_(None)))
    await db.commit()
    return result.rowcount or 0
