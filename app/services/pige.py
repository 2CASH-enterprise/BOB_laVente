"""
Lot 81 — assistant de pige (agence immobilière ; décisions du 17/10).

L'agent colle une annonce de particulier (texte et lien). Bob en tire une fiche, un prix de comparaison (ventes DVF),
un score de priorité, un argumentaire d'appel et un rappel. Bob ne contacte JAMAIS le particulier lui-même.

Règles légales (CNIL, démarchage) rappelées et appliquées :
  - appel manuel seulement : jamais de SMS, d'email ni de WhatsApp au particulier sans son accord ;
  - vérifier la liste Bloctel avant d'appeler (rappel affiché ; la consultation est l'obligation de l'agence) ;
  - dire la source du numéro dès le début de l'appel, et noter toute opposition (« Ne plus contacter ») : ses
    données sont effacées, seule une empreinte du numéro reste pour ne jamais le rappeler ;
  - « agences s'abstenir » est respecté : l'annonce est marquée « Ne pas appeler » ;
  - aucun robot ne lit les sites d'annonces : l'agent colle lui-même l'annonce qu'il a consultée.

Plans : Indépendant 30 annonces par mois, Starter 50 ; Pro et Entreprise sans plafond, avec l'extraction par IA
(quelques dixièmes de centime par annonce, à notre charge) et le rapprochement avec les acheteurs de l'agence.
"""
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import func, select

from app.models.pige import PigeListing

logger = logging.getLogger(__name__)

MONTHLY_QUOTA = {"INDEPENDANT": 30, "STARTER": 50, "PRO": None, "ENTERPRISE": None}
PRO_PLANS = ("PRO", "ENTERPRISE")
TEXT_MAX = 6000
PURGE_AFTER = timedelta(days=180)
NOT_AVAILABLE = "La pige est disponible à partir du plan Indépendant."
STATUSES = {
    "A_APPELER": "À appeler", "PAS_JOINT": "Pas joint", "A_RAPPELER": "À rappeler",
    "RDV": "Rendez-vous d'estimation", "MANDAT": "Mandat signé", "PAS_INTERESSE": "Pas intéressé",
    "OPPOSITION": "Ne plus contacter",
}
TRANSACTIONS = {"VENTE": "Vente", "LOCATION": "Location"}
SITES = {"leboncoin": "Leboncoin", "pap.fr": "PAP", "seloger": "SeLoger", "bienici": "Bien'ici",
         "logic-immo": "Logic-Immo", "facebook": "Facebook", "paruvendu": "ParuVendu", "ouestfrance-immo": "Ouest-France Immo"}
PRIORITIES = {"HAUTE": "Haute", "MOYENNE": "Moyenne", "BASSE": "Basse", "NE_PAS_APPELER": "Ne pas appeler"}


def _plan(tenant) -> str:
    plan = getattr(tenant, "plan", None)
    return getattr(plan, "value", plan) or "FREE"


def available(tenant) -> bool:
    from app.services.business_type import is_real_estate

    return is_real_estate(tenant) and _plan(tenant) in MONTHLY_QUOTA


def is_pro(tenant) -> bool:
    return _plan(tenant) in PRO_PLANS


def monthly_quota(tenant) -> int | None:
    return MONTHLY_QUOTA.get(_plan(tenant), 0)


def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.lower()).strip()


def phone_hash(tenant_id, phone: str | None) -> str | None:
    if not phone:
        return None
    return hashlib.sha256(f"{tenant_id}:{phone}".encode()).hexdigest()


# --- Lecture de l'annonce (sans IA) -----------------------------------------------------------------------------

_MONEY = re.compile(r"(\d{1,3}(?:[ .  ]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s?(?:€|euros?\b|eur\b)(\s?(?:/|par)\s?(?:mois|m2|m²))?",
                    re.IGNORECASE)
_SURFACE = re.compile(r"(\d+(?:[.,]\d+)?)\s?m(?:2|²)(?![a-z])", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+33\s?|0033\s?|\b0)([1-9](?:[\s.-]?\d{2}){4})\b")
_POSTAL_CITY = re.compile(r"\b((?:0[1-9]|[1-8]\d|9[0-5]|97)\d{3})\s+([A-ZÉÈÀÂÎÔÛÇ][\wÀ-ÿ'’ -]{1,40})")
_CITY_POSTAL = re.compile(r"([A-ZÉÈÀÂÎÔÛÇ][\wÀ-ÿ'’ -]{1,40}?)\s*\(\s*((?:0[1-9]|[1-8]\d|9[0-5]|97)\d{3})\s*\)")
_CITY_THEN_POSTAL = re.compile(r"\b([A-ZÉÈÀÂÎÔÛÇ][\wÀ-ÿ'’-]{2,40})\s+((?:0[1-9]|[1-8]\d|9[0-5]|97)\d{3})\b(?!\s?(?:€|euros?))")
_NO_AGENCY = re.compile(r"(agences?\s+s.?abstenir|pas\s+d.?agences?|non\s+aux\s+agences|agences?\s+non\s+merci|"
                        r"sans\s+interm[ée]diaire|ni\s+agences?)", re.IGNORECASE)
_DPE = re.compile(r"(?:dpe|classe\s+[ée]nerg[ée]tique|[ée]tiquette\s+[ée]nergie|classe\s+[ée]nergie)\s*[:\-–]?\s*\(?([A-G])\b",
                  re.IGNORECASE)


def _money(match) -> Decimal:
    whole = re.sub(r"[ .  ]", "", match.group(1))
    return Decimal(f"{whole}.{match.group(2) or 0}")


def site_of(url: str | None) -> str | None:
    host = _plain(url)
    return next((label for key, label in SITES.items() if key in host), "Autre site" if host else None)


def extract(text: str, url: str | None = None) -> dict:
    """Fiche tirée de l'annonce par des règles fixes ; ce qui n'est pas trouvé reste vide (l'agent complète)."""
    from app.services.product_sheets import guess_property_type

    text = (text or "")[:TEXT_MAX]
    plain = _plain(text)
    out: dict = {"site": site_of(url)}
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    out["title"] = first[:200] or None
    # « Charges 120 €/mois » ou « charges de copropriété » ne font pas d'une vente une location.
    plain = re.sub(r"charges?[^.\n]{0,25}?(/ ?mois|par mois|mensuelles?)", " ", plain)
    rent = re.search(r"\b(a louer|location|loyer|par mois|charges comprises)\b|/ ?mois", plain)
    sale = re.search(r"\b(a vendre|vente|vends|prix de vente)\b", plain)
    monthly_words = re.search(r"\b(loyer|par mois)\b|/ ?mois", plain)
    if rent and (not sale or monthly_words):
        out["transaction"] = "LOCATION"
    elif sale or _MONEY.search(text):
        out["transaction"] = "VENTE"
    else:
        out["transaction"] = None
    prices = [(_money(m), (m.group(3) or "").lower()) for m in _MONEY.finditer(text)
              if not re.search(r"charges?\W{0,3}$", _plain(text[max(0, m.start() - 12):m.start()]))]
    prices = [(value, unit) for value, unit in prices if "m" not in unit.replace("mois", "")]  # « 4 500 €/m² » n'est pas un prix
    if out["transaction"] == "LOCATION":
        monthly = [v for v, unit in prices if "mois" in unit] or [v for v, _ in prices if v < 20000]
        out["price"] = max(monthly) if monthly else None
    else:
        out["price"] = max((v for v, _ in prices), default=None)
    surfaces = []
    for m in _SURFACE.finditer(text):
        before = _plain(text[max(0, m.start() - 25):m.start()])
        if "terrain" not in before and "jardin" not in before and "€" not in text[m.end():m.end() + 3]:
            surfaces.append(Decimal(m.group(1).replace(",", ".")))
    out["surface"] = surfaces[0] if surfaces else None
    rooms = re.search(r"\b[tf]([1-9])\b", plain) or re.search(r"\b(\d{1,2})\s?pieces?\b", plain)
    out["rooms"] = int(rooms.group(1)) if rooms else (1 if re.search(r"\bstudio\b", plain) else None)
    dpe = _DPE.search(text)
    out["dpe"] = dpe.group(1).upper() if dpe else None
    place = _POSTAL_CITY.search(text)
    if place and "€" not in text[place.end(1):place.end(1) + 3]:
        out["postal_code"], out["city"] = place.group(1), place.group(2).strip(" -")[:100]
    elif place := _CITY_POSTAL.search(text):
        out["city"], out["postal_code"] = place.group(1).strip(" -")[:100], place.group(2)
    elif place := _CITY_THEN_POSTAL.search(text):
        out["city"], out["postal_code"] = place.group(1)[:100], place.group(2)
    phone = _PHONE.search(text)
    out["owner_phone"] = "+33" + re.sub(r"\D", "", phone.group(1)) if phone else None
    out["no_agency"] = bool(_NO_AGENCY.search(text))
    out["property_type"] = guess_property_type(first, text)
    return out


# --- Lecture par IA (Pro) --------------------------------------------------------------------------------------

AI_PROMPT = (
    "Tu lis UNE annonce immobilière de particulier. Réponds UNIQUEMENT par un objet JSON avec ces clés (null si "
    "l'annonce ne le dit pas, n'invente rien) : transaction (\"VENTE\" ou \"LOCATION\"), property_type (\"APPARTEMENT\", "
    "\"MAISON\", \"TERRAIN\", \"LOCAL\", \"PARKING\" ou \"AUTRE\"), city, postal_code (5 chiffres), surface (m² habitables, "
    "nombre), rooms (nombre de pièces), price (prix de vente, ou loyer mensuel, nombre), dpe (lettre A à G), "
    "no_agency (true si le vendeur refuse les agences), points (liste de 3 points forts au plus, courts)."
)


def mask_personal(text: str) -> str:
    """Numéros et emails retirés avant d'envoyer l'annonce à l'IA : elle n'en a pas besoin."""
    text = _PHONE.sub("[téléphone]", text or "")
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)


async def ai_extract(text: str) -> tuple[dict | None, dict | None]:
    """(champs lus par l'IA, consommation) ; (None, None) si l'IA n'est pas configurée ou ne répond pas."""
    from app.core.config import get_settings

    settings = get_settings()
    if settings.llm_provider != "mistral" or not settings.mistral_api_key:
        return None, None
    try:
        from mistralai.client.sdk import Mistral

        from app.services.llm_costs import usage_from_mistral

        response = await Mistral(api_key=settings.mistral_api_key).chat.complete_async(
            model=settings.mistral_classifier_model,
            messages=[{"role": "system", "content": AI_PROMPT}, {"role": "user", "content": mask_personal(text)[:TEXT_MAX]}],
            response_format={"type": "json_object"}, temperature=0, max_tokens=300)
        content = response.choices[0].message.content
        if isinstance(content, list):
            content = "".join(getattr(chunk, "text", "") for chunk in content)
        raw = json.loads(content or "{}")
        return (raw if isinstance(raw, dict) else None), usage_from_mistral(getattr(response, "usage", None))
    except Exception:  # noqa: BLE001 — jamais bloquant : la lecture par règles reste
        logger.warning("Lecture de l'annonce par l'IA impossible : règles fixes seulement")
        return None, None


def clean_fields(raw: dict) -> dict:
    """Valeurs vérifiées (une valeur douteuse est ignorée, jamais enregistrée)."""
    from app.services.product_sheets import PROPERTY_CATEGORIES

    out = {}
    if raw.get("transaction") in TRANSACTIONS:
        out["transaction"] = raw["transaction"]
    if raw.get("property_type") in PROPERTY_CATEGORIES:
        out["property_type"] = raw["property_type"]
    for key, limit in (("city", 100), ("title", 200), ("owner_name", 100)):
        if isinstance(raw.get(key), str) and raw[key].strip():
            out[key] = " ".join(raw[key].split())[:limit]
    if isinstance(raw.get("postal_code"), (str, int)) and re.fullmatch(r"\d{5}", str(raw["postal_code"]).strip()):
        out["postal_code"] = str(raw["postal_code"]).strip()
    for key, low, high in (("surface", 5, 100000), ("price", 50, 100_000_000)):
        try:
            value = Decimal(str(raw.get(key)).replace(",", ".").replace(" ", "")) if raw.get(key) not in (None, "") else None
        except InvalidOperation:
            value = None
        if value is not None and low <= value <= high:
            out[key] = value
    try:
        rooms = int(raw.get("rooms")) if raw.get("rooms") not in (None, "") else None
    except (TypeError, ValueError):
        rooms = None
    if rooms is not None and 0 < rooms <= 50:
        out["rooms"] = rooms
    if isinstance(raw.get("dpe"), str) and raw["dpe"].strip().upper() in tuple("ABCDEFG"):
        out["dpe"] = raw["dpe"].strip().upper()
    if isinstance(raw.get("no_agency"), bool):
        out["no_agency"] = raw["no_agency"]
    if isinstance(raw.get("owner_phone"), str):
        digits = re.sub(r"\D", "", raw["owner_phone"])
        if digits.startswith("33"):
            digits = digits[2:]
        digits = digits.lstrip("0")
        if len(digits) == 9:
            out["owner_phone"] = "+33" + digits
    return out


# --- Analyse : prix DVF, acheteurs (Pro), score -----------------------------------------------------------------

async def matching_buyers(db, tenant, listing) -> list[str]:
    """Pro — contacts de l'agence dont le projet d'achat (ou de location) correspond à l'annonce (règles du lot 80)."""
    from app.models.customer import Customer
    from app.models.prospect_profile import ProspectProfile
    from app.services import property_alerts

    if not is_pro(tenant) or listing.transaction not in TRANSACTIONS or not listing.price:
        return []
    product = SimpleNamespace(
        name=listing.title or "", price=listing.price, bob_category=listing.property_type,
        sheet={"transaction": TRANSACTIONS[listing.transaction], "city": " ".join(p for p in (listing.city, listing.postal_code) if p),
               **({"rooms": listing.rooms} if listing.rooms else {}), **({"surface": float(listing.surface)} if listing.surface else {})})
    kind = "ACHAT" if listing.transaction == "VENTE" else "LOCATION"
    now = datetime.now(timezone.utc)
    names = []
    rows = (await db.execute(select(ProspectProfile, Customer).join(Customer, Customer.id == ProspectProfile.customer_id)
                             .where(ProspectProfile.tenant_id == tenant.id, ProspectProfile.real_estate.is_not(None)))).all()
    for profile, customer in rows:
        data = ((profile.real_estate or {}).get("projects") or {}).get(kind)
        if data and property_alerts.project_active(data, profile.updated_at, now) and property_alerts.matches(product, kind, data):
            names.append(property_alerts._name(customer))
    return sorted(names)


async def suppressed(db, tenant_id, hashed: str | None, exclude_id=None) -> bool:
    if not hashed:
        return False
    query = select(func.count(PigeListing.id)).where(
        PigeListing.tenant_id == tenant_id, PigeListing.phone_hash == hashed, PigeListing.status == "OPPOSITION")
    if exclude_id is not None:
        query = query.where(PigeListing.id != exclude_id)
    return (await db.execute(query)).scalar_one() > 0


def score(listing, dvf: dict | None, buyers: list[str], opposed: bool) -> tuple[str, int, list[str]]:
    """(priorité, points, raisons) — règles fixes, expliquées à l'agent."""
    if opposed or listing.status == "OPPOSITION":
        return "NE_PAS_APPELER", 0, ["ce vendeur a demandé à ne plus être contacté"]
    if listing.no_agency:
        return "NE_PAS_APPELER", 0, ["l'annonce refuse les agences : à respecter"]
    points, reasons = 0, []
    if listing.owner_phone:
        points += 20
        reasons.append("numéro dans l'annonce")
    else:
        reasons.append("pas de numéro dans l'annonce")
    if listing.online_days and listing.online_days >= 60:
        points += 25
        reasons.append(f"en ligne depuis {listing.online_days} jours")
    elif listing.online_days and listing.online_days >= 30:
        points += 15
        reasons.append(f"en ligne depuis {listing.online_days} jours")
    if listing.price_dropped:
        points += 15
        reasons.append("prix déjà baissé")
    if dvf and dvf.get("status") == "OK" and dvf["delta_pct"] >= 10:
        points += 20
        reasons.append(f"prix {dvf['delta_pct']} % au-dessus des ventes du secteur")
    if listing.dpe in ("F", "G"):
        points += 10
        reasons.append(f"DPE {listing.dpe}")
    if buyers:
        points += 25
        reasons.append(f"{len(buyers)} acheteur{'s' if len(buyers) > 1 else ''} de votre base")
    priority = "HAUTE" if points >= 60 else "MOYENNE" if points >= 30 else "BASSE"
    return priority, points, reasons


async def analyze(db, tenant, listing) -> dict:
    from app.services import dvf

    comparison = await dvf.compare(db, listing.property_type, listing.postal_code, listing.surface, listing.price) \
        if listing.transaction == "VENTE" else None
    buyers = await matching_buyers(db, tenant, listing)
    opposed = await suppressed(db, tenant.id, listing.phone_hash, exclude_id=listing.id)
    priority, points, reasons = score(listing, comparison, buyers, opposed)
    listing.analysis = {"priority": priority, "points": points, "reasons": reasons, "dvf": comparison,
                        "buyers": buyers, "opposed": opposed}
    return listing.analysis


# --- Argumentaire d'appel ---------------------------------------------------------------------------------------

def _euros(value) -> str:
    return f"{Decimal(str(value)):,.0f} €".replace(",", " ")


def script(tenant, listing, agent_name: str | None = None) -> dict:
    """Argumentaire fixe rempli avec la fiche : rappels légaux, présentation, accroches, proposition, objections."""
    analysis = listing.analysis or {}
    who = agent_name or "[votre prénom]"
    what = listing.title or "votre bien"
    where = f" sur {listing.site}" if listing.site and listing.site != "Autre site" else ""
    before = ["Appel manuel seulement : pas de SMS, d'email ni de WhatsApp sans l'accord de la personne.",
              "Vérifiez que ce numéro n'est pas inscrit sur Bloctel avant d'appeler."]
    if analysis.get("priority") == "NE_PAS_APPELER":
        return {"before": before, "blocked": analysis["reasons"][0], "steps": []}
    hooks = []
    if analysis.get("buyers"):
        n = len(analysis["buyers"])
        hooks.append(f"J'accompagne en ce moment {n} acheteur{'s' if n > 1 else ''} qui cherche{'nt' if n > 1 else ''} "
                     f"un bien comme le vôtre{f' à {listing.city}' if listing.city else ''}.")
    comparison = analysis.get("dvf") or {}
    if comparison.get("status") == "OK":
        hooks.append(f"D'après les ventes réelles publiées par l'État au code postal {comparison['postal_code']}, les biens "
                     f"comparables se sont vendus autour de {_euros(comparison['median_m2'])} le m² "
                     f"({comparison['period']}) : on peut regarder ensemble ce que ça donne pour le vôtre.")
    if listing.online_days and listing.online_days >= 30:
        hooks.append(f"Votre annonce est en ligne depuis {listing.online_days} jours : je peux vous dire ce qui freine les "
                     "acheteurs en ce moment, sans engagement.")
    if listing.dpe in ("F", "G"):
        hooks.append(f"Avec un DPE {listing.dpe}, les acheteurs posent beaucoup de questions sur les travaux : nous vous "
                     "aidons à y répondre.")
    if not hooks:
        hooks.append("Je connais bien le secteur et je peux vous donner un avis de valeur sans engagement.")
    steps = [
        {"title": "Présentation (obligatoire)",
         "text": f"Bonjour, {who}, de {tenant.name}. Je vous appelle au sujet de votre annonce{where} pour {what}. "
                 "J'ai trouvé votre numéro sur cette annonce ; si vous préférez ne pas être recontacté, dites-le-moi et je "
                 "le note tout de suite."},
        {"title": "Accroche", "text": " ".join(hooks)},
        {"title": "Proposition",
         "text": "Je vous propose de passer voir votre bien pour vous donner un avis de valeur, sans engagement. "
                 "Quel moment vous arrangerait cette semaine ?"},
        {"title": "S'il vend seul",
         "text": "Je comprends tout à fait. Beaucoup de vendeurs commencent seuls ; si dans quelques semaines vous voulez "
                 "un regard extérieur sur le prix ou les visites, je reste disponible."},
        {"title": "S'il refuse d'être recontacté",
         "text": "C'est noté, je ne vous rappellerai plus. Bonne vente ! (Cochez « Ne plus contacter » après l'appel.)"},
    ]
    return {"before": before, "blocked": None, "steps": steps}


# --- Plafond, doublons, effacement -----------------------------------------------------------------------------

async def used_this_month(db, tenant, now: datetime | None = None) -> int:
    from app.services.local_time import tenant_zone

    now = now or datetime.now(timezone.utc)
    start = now.astimezone(tenant_zone(tenant)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (await db.execute(select(func.count(PigeListing.id)).where(
        PigeListing.tenant_id == tenant.id, PigeListing.created_at >= start.astimezone(timezone.utc)))).scalar_one()


async def quota_left(db, tenant, now: datetime | None = None) -> int | None:
    quota = monthly_quota(tenant)
    return None if quota is None else max(0, quota - await used_this_month(db, tenant, now))


async def duplicate_of(db, tenant_id, url: str | None, hashed: str | None):
    conditions = []
    if url:
        conditions.append(PigeListing.source_url == url)
    if hashed:
        conditions.append(PigeListing.phone_hash == hashed)
    if not conditions:
        return None
    from sqlalchemy import or_

    return (await db.execute(select(PigeListing).where(
        PigeListing.tenant_id == tenant_id, PigeListing.status != "OPPOSITION", or_(*conditions)).limit(1))).scalar_one_or_none()


def forget(listing) -> None:
    """« Ne plus contacter » : tout ce qui identifie le vendeur est effacé ; l'empreinte du numéro reste."""
    listing.text = None
    listing.owner_name = None
    listing.owner_phone = None
    listing.source_url = None
    listing.notes = None
    listing.next_call_at = None


async def purge(db, now: datetime | None = None) -> int:
    """Annonces sans activité depuis 6 mois (hors mandat signé et oppositions) : effacées."""
    from sqlalchemy import delete

    now = now or datetime.now(timezone.utc)
    result = await db.execute(delete(PigeListing).where(
        PigeListing.last_activity_at < now - PURGE_AFTER, PigeListing.status.notin_(("MANDAT", "OPPOSITION"))))
    await db.commit()
    return result.rowcount or 0
