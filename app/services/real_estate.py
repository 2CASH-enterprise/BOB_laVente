"""
Lot 79 — agence immobilière (décisions du 17/10 : 1B 2A 3C 4A 5A).

Quatre publics, quatre parcours : l'acheteur et le locataire (biens présentés en cartes, qualification, visite),
le vendeur et le propriétaire bailleur (qualification du bien, rendez-vous d'estimation : le futur mandat).

- Fiche projet : un projet par type (vendre pour acheter est courant), valeurs vérifiées, rien de deviné.
- Score Chaud / Tiède / Froid par des RÈGLES FIXES (jamais un avis de l'IA), expliqué en une ligne :
    Conclu = le dernier rendez-vous a abouti (vente, location ou mandat signé)
    Chaud  = propriétaire (vente ou gestion) avec un rendez-vous prévu ou un projet sous 3 mois,
             OU acheteur sous 3 mois avec un accord bancaire, un achat comptant ou une visite prévue,
             OU locataire sous 3 mois avec une visite prévue
    Tiède  = rendez-vous prévu, OU à relancer, OU projet sous 3 mois, OU propriétaire (un mandat possible),
             OU budget et secteur connus
    Froid  = sinon (simple renseignement)
- Verrous dans le CODE (pas seulement des consignes) : aucune pièce interdite demandée au candidat locataire
  (loi du 6 juillet 1989, art. 22-2 ; décret 2015-1437) et aucune question discriminatoire (critères de la loi,
  art. 225-1 du code pénal) : la ligne de Bob qui les contient est retirée avant l'envoi.
- Profil de l'agence (loi Hoguet, loi ALUR) : Bob donne ces informations quand on les lui demande, sans jamais
  inventer une carte professionnelle, une garantie ou un barème.
"""
import re
import unicodedata
from datetime import datetime, timezone

from app.services import product_sheets

# --- Profil de l'agence ---------------------------------------------------------------------------------------

STATUSES = {"AGENCE": "Agence immobilière", "MANDATAIRE": "Mandataire / agent commercial immobilier"}
ACTIVITIES = {"VENTE": "Vente", "LOCATION": "Location", "GESTION": "Gestion locative"}
PROFILE_TEXT_FIELDS = {  # clé : (libellé, longueur maximale)
    "card": ("Carte professionnelle", 200),
    "network": ("Réseau ou agence mandante", 150),
    "rsac": ("Immatriculation RSAC (mandataire)", 150),
    "guarantee": ("Garantie financière", 200),
    "zones": ("Secteur couvert", 200),
    "mediator": ("Médiateur de la consommation", 200),
}
PROFILE_URL_FIELDS = {"fees_url": "Barème des honoraires (lien)"}


class ProfileError(ValueError):
    pass


def clean_profile(raw: dict | None) -> dict:
    """Profil vérifié : statut et activités dans les listes, textes bornés, liens https seulement."""
    raw = raw or {}
    profile: dict = {}
    status = raw.get("status")
    if status not in (None, ""):
        if status not in STATUSES:
            raise ProfileError("Statut inconnu : agence ou mandataire.")
        profile["status"] = status
    activities = raw.get("activities")
    if activities is not None:
        if not isinstance(activities, list) or any(a not in ACTIVITIES for a in activities):
            raise ProfileError("Activités possibles : vente, location, gestion locative.")
        profile["activities"] = [a for a in ACTIVITIES if a in activities]
    for key, (_, limit) in PROFILE_TEXT_FIELDS.items():
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            profile[key] = " ".join(value.split())[:limit]
    from app.services.insurance import clean_url

    for key in PROFILE_URL_FIELDS:
        try:
            value = clean_url(raw.get(key))
        except ValueError:
            raise ProfileError("Lien du barème des honoraires invalide : indiquez une adresse web complète (https://…)") from None
        if value:
            profile[key] = value
    return profile


def activities_of(tenant) -> list[str]:
    """Activités déclarées ; rien de déclaré = les trois (l'agence n'a pas encore rempli son profil)."""
    return (getattr(tenant, "real_estate_profile", None) or {}).get("activities") or list(ACTIVITIES)


def office_info(tenant) -> str:
    """Bloc « INFORMATIONS DE L'AGENCE » des consignes de Bob (seulement ce qui est rempli)."""
    profile = getattr(tenant, "real_estate_profile", None) or {}
    lines = []
    if profile.get("status"):
        lines.append(f"Statut : {STATUSES[profile['status']]}")
    lines.append("Activités : " + ", ".join(ACTIVITIES[a].lower() for a in activities_of(tenant)))
    for key, (label, _) in PROFILE_TEXT_FIELDS.items():
        if profile.get(key):
            lines.append(f"{label} : {profile[key]}")
    if profile.get("fees_url"):
        lines.append(f"Barème des honoraires : {profile['fees_url']}")
    if getattr(tenant, "privacy_policy_url", None):
        lines.append(f"Politique de confidentialité : {tenant.privacy_policy_url}")
    return "\n".join(lines)


# --- Fiche projet du client -----------------------------------------------------------------------------------

PROJECTS = {"ACHAT": "Achat", "LOCATION": "Location", "VENTE": "Vente de son bien",
            "GESTION": "Mise en location / gestion de son bien"}
OWNER_PROJECTS = ("VENTE", "GESTION")
PROPERTY_TYPES = {code: cat["label"] for code, cat in product_sheets.PROPERTY_CATEGORIES.items()}
TIMELINES = {"MOINS_1_MOIS": "sous 1 mois", "UN_A_3_MOIS": "dans 1 à 3 mois", "PLUS_3_MOIS": "dans plus de 3 mois",
             "NE_SAIT_PAS": "pas encore de date"}
SOON = ("MOINS_1_MOIS", "UN_A_3_MOIS")
FINANCING = {"ACCORD_BANCAIRE": "accord bancaire obtenu", "EN_COURS": "financement en cours", "COMPTANT": "comptant",
             "A_DEFINIR": "financement à définir"}
FURNISHED = {"MEUBLE": "meublé", "VIDE": "vide", "INDIFFERENT": "meublé ou vide"}
GUARANTORS = {"GARANT": "avec garant", "VISALE": "garantie Visale", "AUCUN": "sans garant"}
OCCUPANCY = {"LIBRE": "libre", "LOUE": "loué", "OCCUPE_PROPRIETAIRE": "occupé par le propriétaire"}
DPE_CLASSES = {c: f"DPE {c}" for c in "ABCDEFG"}  # lot 80 : classe énergie du bien du propriétaire

# champ : (projets concernés, type, contrainte) — type : text (longueur), int, enum (dict), bool
FIELDS = {
    "property_type": (("ACHAT", "LOCATION", "VENTE", "GESTION"), "enum", PROPERTY_TYPES),
    "zone": (("ACHAT", "LOCATION"), "text", 200),
    "budget": (("ACHAT", "LOCATION"), "text", 100),
    "timeline": (("ACHAT", "LOCATION", "VENTE", "GESTION"), "enum", TIMELINES),
    "rooms_min": (("ACHAT", "LOCATION"), "int", 50),
    "surface_min": (("ACHAT", "LOCATION"), "int", 100000),
    "financing": (("ACHAT",), "enum", FINANCING),
    "must_sell": (("ACHAT",), "bool", None),
    "furnished": (("LOCATION",), "enum", FURNISHED),
    "guarantor": (("LOCATION",), "enum", GUARANTORS),
    "move_in": (("LOCATION",), "text", 100),
    "address": (("VENTE", "GESTION"), "text", 200),
    "surface": (("VENTE", "GESTION"), "int", 100000),
    "rooms": (("VENTE", "GESTION"), "int", 50),
    "occupancy": (("VENTE", "GESTION"), "enum", OCCUPANCY),
    "dpe": (("VENTE", "GESTION"), "enum", DPE_CLASSES),
    "criteria": (("ACHAT", "LOCATION", "VENTE", "GESTION"), "text", 300),
}
# Ordre des questions utiles (une à la fois), par projet.
NEXT_QUESTIONS = {
    "ACHAT": ("property_type", "zone", "budget", "financing", "timeline", "rooms_min"),
    "LOCATION": ("zone", "budget", "move_in", "rooms_min", "furnished"),
    "VENTE": ("address", "property_type", "surface", "timeline"),
    "GESTION": ("address", "property_type", "occupancy"),
}
FIELD_LABELS = {
    "property_type": "type de bien", "zone": "ville ou quartier", "budget": "budget", "timeline": "délai",
    "rooms_min": "nombre de pièces", "surface_min": "surface minimale", "financing": "financement (accord bancaire ?)",
    "must_sell": "bien à vendre avant d'acheter", "furnished": "meublé ou vide", "guarantor": "garant",
    "move_in": "date d'entrée souhaitée", "address": "adresse ou secteur du bien", "surface": "surface du bien",
    "rooms": "nombre de pièces du bien", "occupancy": "bien libre ou loué", "criteria": "critères",
    "dpe": "classe DPE du bien",
}


def _value(kind: str, rule, value):
    """Valeur vérifiée, ou None si elle n'est pas valable."""
    if kind == "text":
        return " ".join(value.split())[:rule] or None if isinstance(value, str) else None
    if kind == "enum":
        return value if value in rule else None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if isinstance(value, bool):
        return None
    try:
        number = int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= rule else None


async def update_project(db, tenant_id, customer_id, values: dict) -> tuple[dict, list[str]]:
    """Enregistre ce que le client a dit pour UN projet ; renvoie (le projet à jour, les champs refusés)."""
    from app.services import prospect as prospect_service
    from app.models.prospect_profile import ProspectProfile

    project = values.get("project")
    if project not in PROJECTS:
        raise ValueError("project")
    profile = await prospect_service.load(db, tenant_id, customer_id)
    if profile is None:
        profile = ProspectProfile(tenant_id=tenant_id, customer_id=customer_id)
        db.add(profile)
    data = dict(profile.real_estate or {})
    projects = dict(data.get("projects") or {})
    current = dict(projects.get(project) or {})
    rejected = []
    for key, (scope, kind, rule) in FIELDS.items():
        if values.get(key) is None:
            continue
        cleaned = _value(kind, rule, values[key]) if project in scope else None
        if cleaned is None:
            rejected.append(key)
        else:
            current[key] = cleaned
    # Lot 80 : date de la dernière information, pour ne plus utiliser un projet resté sans nouvelle depuis 90 jours.
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    projects[project] = current
    data["projects"] = projects
    profile.real_estate = data  # nouvel objet : SQLAlchemy voit le changement du JSON
    if current.get("budget") and not profile.budget:
        profile.budget = current["budget"]
    await db.flush()
    return current, rejected


def missing(project: str, current: dict) -> list[str]:
    return [FIELD_LABELS[k] for k in NEXT_QUESTIONS[project] if current.get(k) in (None, "")]


def project_instruction(project: str, current: dict) -> str:
    """Ce que Bob fait ensuite, selon le projet (jamais un prix ni une fourchette d'estimation)."""
    if project in OWNER_PROJECTS:
        what = "l'estimer" if project == "VENTE" else "l'estimer et parler de sa mise en location"
        return (f"Propose au client qu'un conseiller vienne voir son bien pour {what} : get_available_slots puis "
                "request_appointment (kind ESTIMATION). Ne donne JAMAIS de prix, de loyer ni de fourchette.")
    text = ("Quand un bien l'intéresse, propose une visite : get_available_slots puis request_appointment (kind VISITE, "
            "product_id du bien).")
    if project == "ACHAT" and current.get("must_sell"):
        text += " Il doit vendre son bien actuel : propose aussi une estimation (kind ESTIMATION)."
    return text


def _projects(profile) -> dict:
    return ((getattr(profile, "real_estate", None) or {}).get("projects") or {}) if profile is not None else {}


def score(projects: dict, has_appointment: bool, outcome: str | None) -> tuple[str, list[str]]:
    """(VENDU | CHAUD | TIEDE | FROID, raisons lisibles). Règles fixes, voir l'en-tête du module."""
    if outcome == "SOLD":
        return "VENDU", ["affaire conclue au rendez-vous"]
    reasons = []
    from app.services.prospect import OUTCOME_REASONS

    if outcome in OUTCOME_REASONS:
        reasons.append(OUTCOME_REASONS[outcome])
    if has_appointment:
        reasons.append("rendez-vous prévu")
    owner = any(p in projects for p in OWNER_PROJECTS)
    if owner:
        reasons.append("propriétaire : " + " et ".join(PROJECTS[p].lower() for p in OWNER_PROJECTS if p in projects))
    soon = [p for p, d in projects.items() if d.get("timeline") in SOON]
    for p in soon:
        reasons.append(f"{PROJECTS[p].lower()} {TIMELINES[projects[p]['timeline']]}")
    achat = projects.get("ACHAT") or {}
    if achat.get("financing") in ("ACCORD_BANCAIRE", "COMPTANT"):
        reasons.append(FINANCING[achat["financing"]])
    located = any(d.get("budget") and d.get("zone") for d in projects.values())
    if located:
        reasons.append("budget et secteur connus")
    hot = (owner and (has_appointment or any(p in OWNER_PROJECTS for p in soon))) \
        or ("ACHAT" in soon and (achat.get("financing") in ("ACCORD_BANCAIRE", "COMPTANT") or has_appointment)) \
        or ("LOCATION" in soon and has_appointment)
    if hot:
        return "CHAUD", reasons
    if has_appointment or outcome == "FOLLOW_UP" or soon or owner or located:
        return "TIEDE", reasons
    return "FROID", reasons or ["simple renseignement pour l'instant"]


SCORES = {"VENDU": "Conclu", "CHAUD": "Chaud", "TIEDE": "Tiède", "FROID": "Froid"}


def _project_line(project: str, data: dict) -> str:
    parts = []
    if data.get("property_type"):
        parts.append(PROPERTY_TYPES[data["property_type"]].lower())
    for key in ("zone", "address"):
        if data.get(key):
            parts.append(data[key])
    if data.get("rooms_min"):
        parts.append(f"{data['rooms_min']} pièces ou plus")
    if data.get("rooms"):
        parts.append(f"{data['rooms']} pièces")
    if data.get("surface_min"):
        parts.append(f"{data['surface_min']} m² ou plus")
    if data.get("surface"):
        parts.append(f"{data['surface']} m²")
    if data.get("budget"):
        parts.append(f"budget {data['budget']}")
    for key, labels in (("financing", FINANCING), ("furnished", FURNISHED), ("guarantor", GUARANTORS),
                        ("occupancy", OCCUPANCY), ("dpe", DPE_CLASSES)):
        if data.get(key):
            parts.append(labels[data[key]])
    if data.get("move_in"):
        parts.append(f"entrée {data['move_in']}")
    if data.get("must_sell"):
        parts.append("doit vendre avant d'acheter")
    if data.get("timeline"):
        parts.append(TIMELINES[data["timeline"]])
    if data.get("criteria"):
        parts.append(data["criteria"])
    return f"{PROJECTS[project]} : " + (", ".join(parts) if parts else "à préciser")


async def build_fiche(db, tenant, customer, now: datetime | None = None) -> dict | None:
    """Fiche du prospect d'une agence ; None si Bob n'a encore rien appris."""
    from app.services import prospect as prospect_service
    from app.services.handoff_service import customer_display_name

    now = now or datetime.now(timezone.utc)
    profile = await prospect_service.load(db, tenant.id, customer.id)
    appointment = await prospect_service._latest_appointment(db, tenant.id, customer.id)
    projects = _projects(profile)
    if not projects and appointment is None:
        return None
    fiche = {
        "prospect": " ".join(p for p in (customer.first_name, customer.last_name) if p) or customer_display_name(customer),
        "phone": f"+{customer.whatsapp_number}" if customer.whatsapp_number and customer.whatsapp_number.isdigit() else None,
        "city": customer.city,
        "projects": projects,
        "property": appointment.vehicle_label if appointment else None,
        "has_appointment": prospect_service._has_upcoming(appointment, now),
        "outcome": appointment.outcome if appointment is not None else None,
    }
    fiche["score"], fiche["reasons"] = score(projects, fiche["has_appointment"], fiche["outcome"])
    fiche["score_label"] = SCORES[fiche["score"]]
    rows = [("Prospect", fiche["prospect"]), ("Téléphone", fiche["phone"]), ("Ville", fiche["city"])]
    rows += [("Projet", _project_line(p, projects[p])) for p in PROJECTS if p in projects]
    rows += [("Bien visé", fiche["property"]),
             ("Score commercial", f"{SCORES[fiche['score']]} ({', '.join(fiche['reasons'])})")]
    fiche["lines"] = [f"{label} : {' '.join(str(value).split())}" for label, value in rows if value]
    return fiche


# --- Recherche de biens ---------------------------------------------------------------------------------------

def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.lower()).strip()


_TYPE_WORDS = [
    (r"\b(appartements?|apparts?|studios?|duplex|lofts?|[tf][1-9])\b", "APPARTEMENT"),
    (r"\b(maisons?|villas?|pavillons?|longeres?|chalets?)\b", "MAISON"),
    (r"\b(terrains?|parcelles?)\b", "TERRAIN"),
    (r"\b(locaux|local|bureaux?|boutiques?|entrepots?)\b", "LOCAL"),
    (r"\b(parkings?|box|garages?)\b", "PARKING"),
]
_STOP = {"a", "au", "aux", "de", "du", "des", "d", "l", "la", "le", "les", "un", "une", "en", "dans", "pour", "sur",
         "avec", "et", "ou", "je", "cherche", "recherche", "veux", "voudrais", "souhaite", "louer", "location", "acheter",
         "achat", "vendre", "vente", "bien", "biens", "pieces", "piece", "chambres", "chambre", "m2", "m", "vous", "avez",
         "quoi", "disponible", "disponibles", "pres", "proche", "secteur", "quartier", "ville", "centre"}


def criteria_from_query(query: str | None) -> tuple[str | None, dict]:
    """« T3 à louer à Lyon » → (mots restants, {transaction, property_type, min_rooms, min_surface})."""
    if not query:
        return query, {}
    plain = _plain(query)
    criteria: dict = {}
    if re.search(r"\b(louer|location|locations|loyer|a louer)\b", plain):
        criteria["transaction"] = "LOCATION"
    elif re.search(r"\b(acheter|achat|a vendre|vente|acquerir)\b", plain):
        criteria["transaction"] = "VENTE"
    for pattern, code in _TYPE_WORDS:
        if re.search(pattern, plain):
            criteria["property_type"] = code
            break
    if (m := re.search(r"\b[tf]([1-9])\b", plain)) or (m := re.search(r"\b(\d{1,2}) ?pieces?\b", plain)):
        criteria["min_rooms"] = int(m.group(1))
    elif re.search(r"\bstudios?\b", plain):
        criteria["min_rooms"] = 1
    elif m := re.search(r"\b(\d{1,2}) ?chambres?\b", plain):
        criteria["min_rooms"] = int(m.group(1)) + 1
    if m := re.search(r"\b(\d{2,4}) ?m(?:2|²)?\b", plain.replace("m²", "m2")):
        criteria["min_surface"] = int(m.group(1))
    rest = re.sub(r"\b[tf][1-9]\b|\d+", " ", plain)
    for pattern, _ in _TYPE_WORDS:
        rest = re.sub(pattern, " ", rest)
    words = [w for w in re.split(r"[^a-z0-9-]+", rest) if w and w not in _STOP and len(w) > 1]
    return (" ".join(words) or None), criteria


def matches(product, transaction=None, property_type=None, city=None, min_rooms=None, min_surface=None,
            words: str | None = None) -> bool:
    """Un critère connu qui ne correspond pas exclut le bien ; une information absente de la fiche ne l'exclut pas."""
    sheet = getattr(product, "sheet", None) or {}
    if transaction and sheet.get("transaction") and _plain(sheet["transaction"]) != _plain(transaction):
        return False
    if property_type and getattr(product, "bob_category", None) and product.bob_category != property_type:
        return False
    if min_rooms is not None and product_sheets.to_number(sheet.get("rooms")) is not None \
            and product_sheets.to_number(sheet["rooms"]) < float(min_rooms):
        return False
    if min_surface is not None and product_sheets.to_number(sheet.get("surface")) is not None \
            and product_sheets.to_number(sheet["surface"]) < float(min_surface):
        return False
    haystack = _plain(" ".join(str(v) for v in (product.name, getattr(product, "description", None),
                                                    *sheet.values()) if v))
    if city and _plain(city) not in haystack:
        return False
    for word in (words or "").split():
        if _plain(word) not in haystack:
            return False
    return True


def card_detail(product) -> str | None:
    """« À louer · 3 pièces · 68 m² · Lyon 3e » — jamais le prix (lot 76)."""
    sheet = getattr(product, "sheet", None) or {}
    parts = []
    if sheet.get("transaction"):
        parts.append("À louer" if sheet["transaction"] == "Location" else "À vendre")
    if sheet.get("rooms"):
        parts.append(f"{sheet['rooms']} pièce{'s' if product_sheets.to_number(sheet['rooms']) not in (None, 1) else ''}")
    surface = sheet.get("surface") or sheet.get("land_surface")
    if surface:
        parts.append(f"{surface} m²")
    if sheet.get("city"):
        parts.append(str(sheet["city"]))
    if sheet.get("dpe"):
        parts.append(f"DPE {sheet['dpe']}" if len(str(sheet["dpe"])) == 1 else str(sheet["dpe"]))
    return " · ".join(parts) or None


# --- Verrous légaux sur la réponse de Bob ---------------------------------------------------------------------

# Pièces que le bailleur ou l'agence ne peut PAS demander au candidat locataire (loi du 6 juillet 1989, art. 22-2).
FORBIDDEN_DOCUMENTS = re.compile(
    r"(relev[ée]s? (de|du|des) comptes?|relev[ée]s? bancaires?|attestation de bonne tenue|carte vitale|"
    r"carte d'assur[ée] social|casier judiciaire|photo d'identit[ée]|photos d'identit[ée]|dossier m[ée]dical|"
    r"contrat de mariage|jugement de divorce|ch[èe]que de r[ée]servation|autorisation de pr[ée]l[èe]vement|"
    r"absence de cr[ée]dit|certificat de concubinage|carte d'[ée]lecteur)",
    re.IGNORECASE,
)
# Questions sur des critères de discrimination (art. 225-1 du code pénal).
DISCRIMINATORY = re.compile(
    r"(nationalit|origine|religio|ethni|couleur de (votre )?peau|enceinte|grossesse|handicap|[ée]tat de sant[ée]|"
    r"orientation sexuelle|opinions? politiques?|syndica|situation (de famille|familiale)|[êe]tes[- ]vous mari[ée]|"
    r"avez[- ]vous des enfants|lieu de naissance|patronyme)",
    re.IGNORECASE,
)
SAFE_FALLBACK = "Un conseiller de l'agence vous indiquera la suite."


def legal_guard(text: str) -> tuple[str, list[str]]:
    """Retire de la réponse de Bob toute ligne qui demande une pièce interdite ou pose une question discriminatoire.
    Renvoie (texte nettoyé, motifs) ; le texte n'est jamais vide."""
    kept, reasons = [], []
    for line in (text or "").splitlines():
        if FORBIDDEN_DOCUMENTS.search(line):
            reasons.append("pièce interdite au dossier locataire")
            continue
        if DISCRIMINATORY.search(line) and ("?" in line or re.search(r"\b(indiquer|pr[ée]ciser|dire)\b", line, re.I)):
            reasons.append("question discriminatoire")
            continue
        kept.append(line)
    if not reasons:
        return text, []
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return (cleaned or SAFE_FALLBACK), reasons


RENTAL_DOCUMENTS = (
    "une pièce d'identité ; un justificatif de domicile (3 dernières quittances de loyer, attestation d'hébergement "
    "ou dernier avis de taxe foncière) ; un justificatif de situation professionnelle (contrat de travail, attestation "
    "de l'employeur, carte d'étudiant ou extrait Kbis…) ; des justificatifs de ressources (3 derniers bulletins de "
    "salaire, dernier avis d'imposition, justificatifs de prestations ou de bourse…) ; et, s'il y a un garant, les "
    "mêmes pièces pour lui, ou son visa Visale"
)


# --- Estimation et crédit : jamais un chiffre annoncé par Bob -----------------------------------------------------

_MONEY = r"\d[\d\s.,  ]*\s?(?:€|eur\b|euros?\b|fcfa\b|xof\b|k€)"
_LOAN = [
    re.compile(r"mensualit\w*[^.!?\n]{0,40}\d"),
    re.compile(r"\b(?:taux|taeg)\b[^.!?\n]{0,40}\d"),
    re.compile(r"\bapports?\b[^.!?\n]{0,40}\d"),
    re.compile(rf"(?:capacit[ée] d'emprunt|emprunter|pr[êe]t immobilier|cr[ée]dit)[^.!?\n]{{0,60}}{_MONEY}"),
]
_OWNER_PROPERTY = re.compile(r"\b(?:votre|ton|ta) (?:bien|maison|appartement|appart|terrain|local|studio|villa|logement)\b")
_VALUATION = re.compile(r"(?:\bvaut\b|vaudrait|se vendrait|se louerait|valeur|estim\w*|pourriez (?:le|la) (?:vendre|louer))")
_NOT_VALUATION = re.compile(r"(?:[ée]nergie|[ée]nerg[ée]tique|\bdpe\b|honoraires|affich)")

VALUATION_MESSAGE = (
    "Pour l'estimation de votre bien comme pour le financement, un conseiller vous fera une proposition "
    "personnalisée. Je lui transmets votre demande : il reviendra vers vous directement."
)
VALUATION_MESSAGE_NO_TRANSFER = (
    "Pour l'estimation de votre bien comme pour le financement, un conseiller vous fera une proposition "
    "personnalisée, sans engagement."
)


def contains_valuation_or_loan_figure(text: str) -> bool:
    """Lot 79 — un prix d'estimation du bien du client, ou un chiffre de crédit, annoncé par Bob. Le prix affiché
    d'un bien de l'agence, son loyer, ses honoraires et ses dépenses d'énergie restent autorisés."""
    normalized = (text or "").lower().replace("’", "'")
    if any(p.search(normalized) for p in _LOAN):
        return True
    for sentence in re.split(r"(?<=[.!?\n])", normalized):
        if not re.search(_MONEY, sentence) or _NOT_VALUATION.search(sentence):
            continue
        if _OWNER_PROPERTY.search(sentence) or _VALUATION.search(sentence):
            return True
    return False


# --- Import CSV des biens (tableau de bord et démo) -------------------------------------------------------------

CSV_HEADERS = {  # clé de la fiche (ou "type") : en-têtes acceptés, sans accents ni majuscules
    "type": {"type", "type de bien", "bien", "nature", "categorie de bien"},
    "transaction": {"transaction", "vente ou location", "vente/location", "offre", "mandat type"},
    "city": {"ville", "commune", "localite", "localisation", "city"},
    "district": {"quartier", "secteur", "arrondissement"},
    "surface": {"surface", "surface habitable", "superficie", "m2", "surface (m2)"},
    "land_surface": {"terrain", "surface terrain", "surface du terrain"},
    "rooms": {"pieces", "nb pieces", "nombre de pieces", "nb de pieces"},
    "bedrooms": {"chambres", "nb chambres", "nombre de chambres"},
    "dpe": {"dpe", "classe energie", "classe energetique"},
    "ges": {"ges", "classe climat"},
    "fees": {"honoraires", "frais d'agence", "frais agence"},
    "charges": {"charges"},
    "deposit": {"depot de garantie", "caution"},
    "floor": {"etage"},
    "furnished": {"meuble"},
    "reference": {"reference mandat", "mandat", "numero de mandat"},
    "available_from": {"disponibilite", "disponible le", "disponible a partir du"},
}


def csv_property_headers(fieldnames) -> dict[str, str]:
    """En-tête du fichier → clé de la fiche (« Pièces » → rooms)."""
    found: dict[str, str] = {}
    for header in fieldnames or []:
        plain = _plain(header)
        for key, aliases in CSV_HEADERS.items():
            if plain in aliases and key not in found.values():
                found[header] = key
                break
    return found


def property_from_csv_row(row: dict, name: str, description: str | None) -> tuple[str, dict | None, bool]:
    """(type de bien, fiche, le fichier avait-il des colonnes de bien). Le type et la transaction sont devinés d'après
    le nom et la description quand le fichier ne les donne pas ; le commerçant corrige dans Biens."""
    headers = csv_property_headers(row.keys())
    raw = {key: row.get(header) for header, key in headers.items() if (row.get(header) or "").strip()}
    type_text = raw.pop("type", None)
    code = product_sheets.guess_property_type(type_text, name) if type_text else product_sheets.guess_property_type(
        name, description)
    if "transaction" in raw:
        plain = _plain(raw["transaction"])
        raw["transaction"] = "Location" if re.search(r"loc|louer|bail", plain) else "Vente" if re.search(
            r"vent|vendre|achat", plain) else raw["transaction"]
    else:
        plain = _plain(f"{name} {description or ''}")
        if re.search(r"\b(a louer|location|loyer)\b", plain):
            raw["transaction"] = "Location"
        elif re.search(r"\b(a vendre|vente)\b", plain):
            raw["transaction"] = "Vente"
    if "furnished" in raw:
        raw["furnished"] = "Meublé" if re.search(r"^(oui|o|yes|1|meuble)", _plain(raw["furnished"])) else "Vide"
    return code, product_sheets.normalize_sheet(code, raw), bool(headers)


# Lot 78 B — colonnes du modèle Excel (et de l'export) des biens : reconnues par csv_property_headers.
TEMPLATE_COLUMNS = [("TYPE", "type"), ("TRANSACTION", "transaction"), ("VILLE", "city"), ("QUARTIER", "district"),
                    ("SURFACE", "surface"), ("TERRAIN", "land_surface"), ("PIECES", "rooms"), ("CHAMBRES", "bedrooms"),
                    ("DPE", "dpe"), ("GES", "ges"), ("HONORAIRES", "fees"), ("CHARGES", "charges"),
                    ("DEPOT DE GARANTIE", "deposit"), ("ETAGE", "floor"), ("MEUBLE", "furnished"),
                    ("REFERENCE MANDAT", "reference"), ("DISPONIBILITE", "available_from")]
