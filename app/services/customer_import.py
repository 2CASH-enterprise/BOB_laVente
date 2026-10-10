"""
Lot 59 — la liste de clients que le commerçant tient dans Excel, importée dans Bob (les trois secteurs), et
ressortie en Excel.

Import (choix du 09/10 : 1A 2A 3A) :
- Aperçu d'abord : Bob propose la correspondance des colonnes (« Tél. » → Téléphone…) et montre les premières
  lignes ; le commerçant corrige, puis valide. Le fichier est renvoyé à la validation : rien n'est gardé entre-temps.
- Le client est retrouvé par son numéro WhatsApp (normalisé avec l'indicatif du pays) ; un client déjà connu
  n'est jamais créé deux fois et ce qui est déjà connu n'est JAMAIS remplacé : on complète seulement ce qui manque.
- L'accord pour les offres ne s'importe pas : il se recueille sur WhatsApp ou par email (lot 34).
- Importer n'envoie aucun message : Bob reconnaît simplement le client quand il écrit.
"""
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.services.spreadsheet import SpreadsheetError, read_table

FIELDS = {
    "phone": "Téléphone",
    "first_name": "Prénom",
    "last_name": "Nom",
    "full_name": "Nom complet",
    "email": "Email",
    "city": "Ville",
    "note": "Note",
    "tags": "Étiquettes",
}
SYNONYMS = {
    "phone": ("telephone", "tel", "tél", "phone", "mobile", "portable", "whatsapp", "numero", "numero de telephone",
              "num", "contact", "cellulaire", "gsm", "n tel", "no tel", "numero whatsapp"),
    "first_name": ("prenom", "prenoms", "first name", "firstname"),
    "last_name": ("nom", "nom de famille", "last name", "lastname", "surname"),
    "full_name": ("nom complet", "client", "nom du client", "nom et prenom", "nom et prenoms", "nom prenom",
                  "nom prenoms", "full name", "name", "raison sociale", "societe", "entreprise"),
    "email": ("email", "e mail", "mail", "courriel", "adresse email", "adresse mail"),
    "city": ("ville", "commune", "localite", "quartier", "city", "localisation"),
    "note": ("note", "notes", "remarque", "remarques", "commentaire", "commentaires", "observation", "observations"),
    "tags": ("etiquette", "etiquettes", "tag", "tags", "categorie", "segment", "groupe", "type de client"),
}
PREVIEW_ROWS = 5
NOTE_MAX = 2000
TAG_MAX = 30


class ImportError_(ValueError):
    """Refus : message montré tel quel."""


def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


_LOOKUP = {_plain(word): field for field, words in SYNONYMS.items() for word in words}


def detect_mapping(header: list[str]) -> list[str | None]:
    """Un champ par colonne reconnue (chaque champ une seule fois : la première colonne l'emporte)."""
    used, mapping = set(), []
    for name in header:
        field = _LOOKUP.get(_plain(name))
        if field is None:
            plain = _plain(name)
            field = next((f for f, words in SYNONYMS.items() if f == "phone" and any(w in plain for w in ("tel", "phone", "whatsapp", "mobile"))), None)
        if field in used:
            field = None
        if field:
            used.add(field)
        mapping.append(field)
    return mapping


def preview(raw: bytes, filename: str, sheet: str | None = None) -> dict:
    try:
        table = read_table(raw, filename, sheet)
    except SpreadsheetError as exc:
        raise ImportError_(str(exc)) from None
    mapping = detect_mapping(table["header"])
    return {
        "sheets": table["sheets"], "sheet": table["sheet"], "header": table["header"], "mapping": mapping,
        "rows": table["rows"][:PREVIEW_ROWS], "total": len(table["rows"]),
        "fields": [{"value": k, "label": v} for k, v in FIELDS.items()],
        "missing_phone": "phone" not in mapping,
    }


def _check_mapping(header: list[str], mapping: list) -> list[str | None]:
    if not isinstance(mapping, list) or len(mapping) != len(header):
        raise ImportError_("La correspondance des colonnes ne correspond pas au fichier : recommencez l'aperçu")
    clean = [m if m in FIELDS else None for m in mapping]
    chosen = [m for m in clean if m]
    if chosen.count("phone") != 1:
        raise ImportError_("Indiquez une (et une seule) colonne Téléphone")
    duplicates = {m for m in chosen if chosen.count(m) > 1}
    if duplicates:
        raise ImportError_("Chaque information ne peut venir que d'une colonne : " + ", ".join(FIELDS[d] for d in duplicates))
    return clean


def _tags(value: str) -> list[str]:
    return [t[:TAG_MAX] for t in (" ".join(part.split()) for part in re.split(r"[,;/|]", value or "")) if t]


async def import_customers(db, tenant, raw: bytes, filename: str, sheet: str | None, mapping: list,
                           now: datetime | None = None) -> dict:
    from app.models.customer import Customer
    from app.services.insurance_contracts import _email, normalize_phone

    try:
        table = read_table(raw, filename, sheet)
    except SpreadsheetError as exc:
        raise ImportError_(str(exc)) from None
    mapping = _check_mapping(table["header"], mapping)
    now = now or datetime.now(timezone.utc)
    known = {c.whatsapp_number: c for c in (await db.execute(
        select(Customer).where(Customer.tenant_id == tenant.id))).scalars().all()}
    report = {"created": 0, "completed": 0, "unchanged": 0, "errors": [], "warnings": [],
              "ignored_columns": [h for h, m in zip(table["header"], mapping) if not m and h], "total": len(table["rows"])}
    detail = f"Fichier {filename}"[:255]
    for offset, row in enumerate(table["rows"]):
        line = table["first_line"] + offset
        values = {field: row[i].strip() for i, field in enumerate(mapping) if field and row[i].strip()}
        number = normalize_phone(values.get("phone"), tenant.country)
        if number is None:
            report["errors"].append({"line": line, "message": "téléphone manquant ou invalide" if values.get("phone")
                                     else "téléphone manquant"})
            continue
        email = None
        if values.get("email"):
            try:
                email = _email(values["email"])
            except ValueError:
                report["warnings"].append({"line": line, "message": f"email ignoré (invalide) : {values['email'][:80]}"})
        first, last = values.get("first_name"), values.get("last_name")
        if not first and not last and values.get("full_name"):
            first = values["full_name"]
        customer = known.get(number)
        created = customer is None
        if created:
            customer = Customer(tenant_id=tenant.id, whatsapp_number=number, acquisition_source="IMPORT",
                                acquisition_detail=detail, tags=[], detected_preferences={})
            db.add(customer)
            known[number] = customer
        changed = False
        # On complète seulement ce qui manque : un nom, un email, une ville ou une note déjà connus restent.
        if not customer.first_name and not customer.last_name and (first or last):
            customer.first_name, customer.last_name = (first or None) and first[:255], (last or None) and last[:255]
            changed = True
        if email and not customer.email:
            customer.email, customer.email_source, customer.email_collected_at = email, "MANUAL", now
            changed = True
        if values.get("city") and not customer.city:
            customer.city = values["city"][:255]
            changed = True
        if values.get("note") and not customer.notes:
            customer.notes = values["note"][:NOTE_MAX]
            changed = True
        new_tags = [t for t in _tags(values.get("tags", "")) if t not in (customer.tags or [])]
        if new_tags:
            customer.tags = list(customer.tags or []) + new_tags
            changed = True
        if created:
            report["created"] += 1
        elif changed:
            report["completed"] += 1
        else:
            report["unchanged"] += 1
    await db.commit()
    report["errors"] = report["errors"][:100]
    report["warnings"] = report["warnings"][:100]
    return report


# --- Export -------------------------------------------------------------------------------------------------

async def export_rows(db, tenant) -> tuple[list[str], list[list]]:
    """Tous les clients de la boutique : identité, source, accord pour les offres, dernier échange, score, notes."""
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.services.acquisition import channel_label, channel_of
    from app.services.business_type import is_dealership, is_insurance
    from app.services.local_time import tenant_zone

    zone = tenant_zone(tenant)

    def when(moment):
        if moment is None:
            return ""
        aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        return aware.astimezone(zone).strftime("%d/%m/%Y %H:%M")

    customers = (await db.execute(select(Customer).where(Customer.tenant_id == tenant.id)
                                  .order_by(Customer.created_at))).scalars().all()
    last = dict((await db.execute(select(Conversation.customer_id, func.max(Conversation.last_message_at)).where(
        Conversation.tenant_id == tenant.id).group_by(Conversation.customer_id))).all())
    scores: dict = {}
    if is_insurance(tenant):
        from app.services import insurance_prospect

        scores = {cid: p["score_label"] for cid, p in (await insurance_prospect.for_customers(db, tenant)).items()}
    elif is_dealership(tenant):
        from app.models.appointment_request import AppointmentRequest
        from app.models.prospect_profile import ProspectProfile
        from app.services.prospect import build_fiche

        ids = set((await db.execute(select(ProspectProfile.customer_id).where(ProspectProfile.tenant_id == tenant.id))).scalars())
        ids |= set((await db.execute(select(AppointmentRequest.customer_id).where(AppointmentRequest.tenant_id == tenant.id))).scalars())
        by_id = {c.id: c for c in customers}
        for cid in ids:
            fiche = await build_fiche(db, tenant, by_id[cid]) if cid in by_id else None
            if fiche:
                scores[cid] = fiche.get("score_label") or fiche.get("score") or ""
    header = ["Prénom", "Nom", "Téléphone", "Email", "Ville", "Source", "Détail de la source", "Accepte les offres",
              "Accord donné le", "Dernier échange", "Client depuis", "Étiquettes", "Note"]
    if scores or is_insurance(tenant) or is_dealership(tenant):
        header.insert(10, "Score")
    rows = []
    for c in customers:
        row = [c.first_name or "", c.last_name or "", f"+{c.whatsapp_number}", c.email or "", c.city or "",
               channel_label(channel_of(c.acquisition_source)), c.acquisition_detail or "",
               "oui" if c.marketing_consent else "non", when(c.marketing_consent_given_at) if c.marketing_consent else "",
               when(last.get(c.id)), when(c.created_at), ", ".join(c.tags or []), c.notes or ""]
        if "Score" in header:
            row.insert(10, scores.get(c.id, ""))
        rows.append(row)
    return header, rows
