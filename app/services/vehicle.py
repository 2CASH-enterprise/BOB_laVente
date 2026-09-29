"""
Fiches véhicules (lot 26, concession automobile).

Un véhicule reste un produit (prix, stock, photo, SKU) ; ses caractéristiques sont rangées dans
`products.vehicle` (JSON) sous des clés fixes et des valeurs normalisées. Bob les reçoit telles
quelles par ses outils : il ne les invente jamais et peut filtrer sur elles.
"""
import re
from datetime import date

FIELDS = {
    "brand": "Marque",
    "model": "Modèle",
    "trim": "Finition",
    "year": "Année",
    "mileage_km": "Kilométrage",
    "fuel": "Carburant",
    "gearbox": "Boîte",
    "body_type": "Carrosserie",
    "color": "Couleur",
}

FUELS = {"ESSENCE": "Essence", "DIESEL": "Diesel", "HYBRIDE": "Hybride", "ELECTRIQUE": "Électrique", "GPL": "GPL"}
GEARBOXES = {"MANUELLE": "Manuelle", "AUTOMATIQUE": "Automatique"}
# Lot 26b — incident du 29/09 : « un SUV ? » → « je n'en ai pas », la 5008 n'avait pas « SUV » dans son nom.
BODY_TYPES = {
    "SUV": "SUV", "BERLINE": "Berline", "CITADINE": "Citadine", "BREAK": "Break", "MONOSPACE": "Monospace",
    "COUPE": "Coupé", "CABRIOLET": "Cabriolet", "PICK_UP": "Pick-up", "UTILITAIRE": "Utilitaire",
}

_FUEL_ALIASES = {
    "essence": "ESSENCE", "petrol": "ESSENCE", "gasoline": "ESSENCE", "gas": "ESSENCE", "sp95": "ESSENCE", "sp98": "ESSENCE",
    "diesel": "DIESEL", "gazole": "DIESEL", "gasoil": "DIESEL",
    "hybride": "HYBRIDE", "hybrid": "HYBRIDE", "hybride rechargeable": "HYBRIDE", "plug-in hybrid": "HYBRIDE",
    "electrique": "ELECTRIQUE", "electric": "ELECTRIQUE", "ev": "ELECTRIQUE",
    "gpl": "GPL", "lpg": "GPL",
}
_GEARBOX_ALIASES = {
    "manuelle": "MANUELLE", "manuel": "MANUELLE", "manual": "MANUELLE", "bvm": "MANUELLE",
    "automatique": "AUTOMATIQUE", "auto": "AUTOMATIQUE", "automatic": "AUTOMATIQUE", "bva": "AUTOMATIQUE",
}

_BODY_ALIASES = {
    "suv": "SUV", "4x4": "SUV", "crossover": "SUV", "tout-terrain": "SUV", "tout terrain": "SUV",
    "berline": "BERLINE", "sedan": "BERLINE", "citadine": "CITADINE", "city car": "CITADINE", "compacte": "CITADINE",
    "break": "BREAK", "estate": "BREAK", "wagon": "BREAK", "sw": "BREAK",
    "monospace": "MONOSPACE", "minivan": "MONOSPACE", "mpv": "MONOSPACE",
    "coupe": "COUPE", "cabriolet": "CABRIOLET", "convertible": "CABRIOLET",
    "pick-up": "PICK_UP", "pickup": "PICK_UP", "pick up": "PICK_UP", "utilitaire": "UTILITAIRE", "van": "UTILITAIRE",
    "fourgon": "UTILITAIRE", "fourgonnette": "UTILITAIRE",
}

# En-têtes CSV acceptés (français ou anglais, sans tenir compte de la casse ni des accents).
CSV_HEADERS = {
    "brand": {"brand", "marque", "make"},
    "model": {"model", "modele"},
    "trim": {"trim", "finition", "version"},
    "year": {"year", "annee"},
    "mileage_km": {"mileage", "mileage_km", "kilometrage", "km"},
    "fuel": {"fuel", "carburant", "energie"},
    "gearbox": {"gearbox", "boite", "transmission"},
    "body_type": {"body_type", "body", "carrosserie", "segment"},
    "color": {"color", "colour", "couleur"},
}

MAX_MILEAGE_KM = 2_000_000
MIN_YEAR = 1950


def _strip_accents(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _to_int(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float)):
        return int(value)
    if "-" in str(value):
        raise ValueError  # jamais de valeur négative silencieusement retournée en positive
    digits = re.sub(r"[^\d]", "", str(value))  # « 48 000 km », « 48.000 »
    if not digits:
        raise ValueError
    return int(digits)


def normalize_vehicle(data: dict | None) -> tuple[dict | None, list[str]]:
    """Renvoie (caractéristiques normalisées ou None si rien, erreurs lisibles)."""
    if not data:
        return None, []
    result: dict = {}
    errors: list[str] = []
    unknown = set(data) - set(FIELDS)
    if unknown:
        errors.append(f"Caractéristique inconnue : {', '.join(sorted(unknown))}")

    for key in ("brand", "model", "trim", "color"):
        value = data.get(key)
        if value is not None and str(value).strip():
            result[key] = str(value).strip()[:80]

    try:
        year = _to_int(data.get("year"))
        if year is not None:
            if not MIN_YEAR <= year <= date.today().year + 1:
                raise ValueError
            result["year"] = year
    except ValueError:
        errors.append(f"Année invalide : {data.get('year')}")

    try:
        mileage = _to_int(data.get("mileage_km"))
        if mileage is not None:
            if not 0 <= mileage <= MAX_MILEAGE_KM:
                raise ValueError
            result["mileage_km"] = mileage
    except ValueError:
        errors.append(f"Kilométrage invalide : {data.get('mileage_km')}")

    fuel = data.get("fuel")
    if fuel is not None and str(fuel).strip():
        raw = str(fuel).strip()
        code = raw.upper() if raw.upper() in FUELS else _FUEL_ALIASES.get(_strip_accents(raw.lower()))
        if code is None:
            errors.append(f"Carburant inconnu : {fuel} (essence, diesel, hybride, électrique, GPL)")
        else:
            result["fuel"] = code

    gearbox = data.get("gearbox")
    if gearbox is not None and str(gearbox).strip():
        raw = str(gearbox).strip()
        code = raw.upper() if raw.upper() in GEARBOXES else _GEARBOX_ALIASES.get(_strip_accents(raw.lower()))
        if code is None:
            errors.append(f"Boîte inconnue : {gearbox} (manuelle ou automatique)")
        else:
            result["gearbox"] = code

    body = data.get("body_type")
    if body is not None and str(body).strip():
        code = body_type_code(body)
        if code is None:
            errors.append(f"Carrosserie inconnue : {body} ({', '.join(BODY_TYPES.values()).lower()})")
        else:
            result["body_type"] = code

    return (result or None), errors


def body_type_code(value) -> str | None:
    raw = str(value).strip()
    if raw.upper() in BODY_TYPES:
        return raw.upper()
    return _BODY_ALIASES.get(_strip_accents(raw.lower()))


def criteria_from_query(query: str | None) -> tuple[str | None, dict]:
    """
    « SUV », « un diesel », « automatique » : des mots du client qui sont des critères, pas des
    noms de véhicule. Renvoie (texte restant à chercher dans le nom, critères reconnus).
    """
    if not query or not query.strip():
        return query, {}
    criteria: dict = {}
    remaining = []
    for word in re.split(r"[\s,;/]+", query.strip()):
        key = _strip_accents(word.lower())
        if key in _BODY_ALIASES and "body_type" not in criteria:
            criteria["body_type"] = _BODY_ALIASES[key]
        elif key in _FUEL_ALIASES and "fuel" not in criteria:
            criteria["fuel"] = _FUEL_ALIASES[key]
        elif key in _GEARBOX_ALIASES and "gearbox" not in criteria:
            criteria["gearbox"] = _GEARBOX_ALIASES[key]
        elif key and key not in {"un", "une", "des", "de", "voiture", "voitures", "vehicule", "vehicules", "car"}:
            remaining.append(word)
    return (" ".join(remaining) or None), criteria


def vehicle_from_csv_row(row: dict) -> dict:
    """Extrait les colonnes véhicule d'une ligne CSV (en-têtes français ou anglais)."""
    normalized_headers = {_strip_accents((h or "").strip().lower()): h for h in row}
    found = {}
    for key, aliases in CSV_HEADERS.items():
        for alias in aliases:
            header = normalized_headers.get(alias)
            if header is not None and (row.get(header) or "").strip():
                found[key] = row[header]
                break
    return found


def summary(vehicle: dict | None) -> str:
    """« 2021 · 48 000 km · Diesel · Automatique »."""
    if not vehicle:
        return ""
    parts = []
    if vehicle.get("year"):
        parts.append(str(vehicle["year"]))
    if vehicle.get("mileage_km") is not None:
        parts.append(f"{vehicle['mileage_km']:,} km".replace(",", " "))
    if vehicle.get("fuel"):
        parts.append(FUELS.get(vehicle["fuel"], vehicle["fuel"]))
    if vehicle.get("gearbox"):
        parts.append(GEARBOXES.get(vehicle["gearbox"], vehicle["gearbox"]))
    if vehicle.get("body_type"):
        parts.insert(0, BODY_TYPES.get(vehicle["body_type"], vehicle["body_type"]))
    return " · ".join(parts)


def for_ai(vehicle: dict | None) -> dict | None:
    """Caractéristiques lisibles transmises à Bob (libellés français, jamais de valeur inventée)."""
    if not vehicle:
        return None
    readable = {}
    for key, label in FIELDS.items():
        if key not in vehicle:
            continue
        value = vehicle[key]
        if key == "fuel":
            value = FUELS.get(value, value)
        elif key == "gearbox":
            value = GEARBOXES.get(value, value)
        elif key == "body_type":
            value = BODY_TYPES.get(value, value)
        elif key == "mileage_km":
            value = f"{value:,} km".replace(",", " ")
        readable[label] = value
    return readable


def matches(vehicle: dict | None, *, fuel=None, gearbox=None, min_year=None, max_mileage_km=None, body_type=None) -> bool:
    """Filtres de recherche de Bob. Un critère demandé sur un véhicule sans cette info = exclu."""
    if not any(v is not None for v in (fuel, gearbox, min_year, max_mileage_km, body_type)):
        return True
    vehicle = vehicle or {}
    if body_type is not None and vehicle.get("body_type") != body_type_code(body_type):
        return False
    if fuel is not None:
        wanted, _ = normalize_vehicle({"fuel": fuel})
        if not wanted or vehicle.get("fuel") != wanted["fuel"]:
            return False
    if gearbox is not None:
        wanted, _ = normalize_vehicle({"gearbox": gearbox})
        if not wanted or vehicle.get("gearbox") != wanted["gearbox"]:
            return False
    if min_year is not None and (vehicle.get("year") is None or vehicle["year"] < int(min_year)):
        return False
    if max_mileage_km is not None and (vehicle.get("mileage_km") is None or vehicle["mileage_km"] > int(max_mileage_km)):
        return False
    return True
