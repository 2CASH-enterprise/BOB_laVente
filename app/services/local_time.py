"""
Heure locale de la boutique (lot 25) : un rendez-vous se saisit et s'affiche dans le fuseau du
pays de la boutique, jamais dans celui du serveur (UTC).

Le fuseau est déduit du pays (tenants.country). Pays non listé : UTC, affiché comme tel.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

COUNTRY_TIMEZONES = {
    # Afrique de l'Ouest (GMT toute l'année)
    "SN": "Africa/Dakar", "CI": "Africa/Abidjan", "ML": "Africa/Bamako", "BF": "Africa/Ouagadougou",
    "GN": "Africa/Conakry", "TG": "Africa/Lome", "GH": "Africa/Accra", "MR": "Africa/Nouakchott",
    "GM": "Africa/Banjul", "SL": "Africa/Freetown", "LR": "Africa/Monrovia", "GW": "Africa/Bissau",
    # Afrique centrale et de l'Ouest (GMT+1)
    "BJ": "Africa/Porto-Novo", "NE": "Africa/Niamey", "NG": "Africa/Lagos", "CM": "Africa/Douala",
    "GA": "Africa/Libreville", "CG": "Africa/Brazzaville", "CD": "Africa/Kinshasa", "TD": "Africa/Ndjamena",
    "CF": "Africa/Bangui", "GQ": "Africa/Malabo",
    # Maghreb
    "MA": "Africa/Casablanca", "DZ": "Africa/Algiers", "TN": "Africa/Tunis",
    # Europe francophone
    "FR": "Europe/Paris", "BE": "Europe/Brussels", "CH": "Europe/Zurich", "LU": "Europe/Luxembourg",
    "CA": "America/Toronto",
}

_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]


def tenant_zone(tenant) -> ZoneInfo:
    return ZoneInfo(COUNTRY_TIMEZONES.get((getattr(tenant, "country", None) or "").upper(), "UTC"))


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def local_to_utc(naive_local: datetime, zone: ZoneInfo) -> datetime:
    """Heure saisie par le conseiller (sans fuseau, heure de la boutique) → UTC."""
    return naive_local.replace(tzinfo=zone).astimezone(timezone.utc)


def format_local(value: datetime, zone: ZoneInfo) -> str:
    """« samedi 4 octobre à 10 h » / « samedi 4 octobre à 10 h 30 »."""
    local = as_utc(value).astimezone(zone)
    minutes = f" {local.minute:02d}" if local.minute else ""
    day = "1er" if local.day == 1 else str(local.day)
    return f"{_DAYS[local.weekday()]} {day} {_MONTHS[local.month - 1]} à {local.hour} h{minutes}"
