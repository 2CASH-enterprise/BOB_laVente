"""
Lot 44 — cohérence des dates dans les rendez-vous (concession).

Incident réel du 02/10/2026 : un conseiller écrit « Samedi 11 à 10h », Bob répète deux fois
« samedi 11 octobre à 10h »… or le 11 octobre 2026 est un DIMANCHE. Bob ne connaissait pas le
calendrier, et rien ne vérifiait l'accord entre le jour et la date.

- calendar_for_ai : la date du jour et les 14 prochains jours, donnés à Bob (heure de la boutique).
- mismatches : repère « samedi 11 octobre » (ou « samedi 11 ») dont le jour ne correspond pas à la
  date ; l'année et le mois sous-entendus sont ceux de la prochaine occurrence.
- clarification : la question fixe à poser au client (jamais une date choisie à sa place).
"""
import re
import unicodedata
from datetime import date, timedelta

DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
          "septembre", "octobre", "novembre", "décembre"]
CALENDAR_DAYS = 14


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


_MONTH_INDEX = {_plain(m): i + 1 for i, m in enumerate(MONTHS)}
_DAY = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
# « samedi 11 octobre (2026) » ; « samedi 11 » seul, mais jamais « samedi 10 h » (= samedi à 10 h)
_WITH_MONTH = re.compile(_DAY + r"\s+(\d{1,2})(?:er)?\s+(" + "|".join(_MONTH_INDEX) + r")(?:\s+(\d{4}))?\b")
_NO_MONTH = re.compile(_DAY + r"\s+(\d{1,2})(?:er)?\b(?!\s*(?:h\b|h\d|heures?|:|\d|" + "|".join(_MONTH_INDEX) + r"))")


def label(day: date) -> str:
    return f"{DAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def calendar_for_ai(today: date) -> str:
    following = ", ".join(label(today + timedelta(days=i)) for i in range(1, CALENDAR_DAYS + 1))
    return (f"Aujourd'hui : {label(today)} {today.year}.\n"
            f"Les {CALENDAR_DAYS} prochains jours : {following}.")


def _resolve(day_num: int, month: int | None, year: int | None, today: date) -> date | None:
    """La date évoquée : année donnée, sinon la prochaine occurrence (une semaine de tolérance en arrière)."""
    candidates = []
    if month is not None:
        for y in ([year] if year else [today.year, today.year + 1]):
            try:
                candidates.append(date(y, month, day_num))
            except ValueError:
                continue
    else:
        for shift in range(0, 3):
            m = (today.month - 1 + shift) % 12 + 1
            y = today.year + (today.month - 1 + shift) // 12
            try:
                candidates.append(date(y, m, day_num))
            except ValueError:
                continue
    for candidate in candidates:
        if year or candidate >= today - timedelta(days=7):
            return candidate
    return None


def mismatches(text: str | None, today: date) -> list[dict]:
    """Jours qui ne correspondent pas à la date qui les suit : [{"written", "date", "real_day"}]."""
    if not text:
        return []
    plain = _plain(text)
    found, seen = [], set()
    for match in _WITH_MONTH.finditer(plain):
        written, num, month, year = match.group(1), int(match.group(2)), _MONTH_INDEX[match.group(3)], match.group(4)
        when = _resolve(num, month, int(year) if year else None, today)
        if when and DAYS[when.weekday()] != written and (written, when) not in seen:
            seen.add((written, when))
            found.append({"written": written, "date": when, "real_day": DAYS[when.weekday()]})
    for match in _NO_MONTH.finditer(plain):
        written, num = match.group(1), int(match.group(2))
        if not 1 <= num <= 31:
            continue
        when = _resolve(num, None, None, today)
        if when and DAYS[when.weekday()] != written and not any(f["written"] == written and f["date"].day == num for f in found):
            seen.add((written, when))
            found.append({"written": written, "date": when, "real_day": DAYS[when.weekday()]})
    return found


def nearest(written_day: str, around: date) -> date:
    """Le jour nommé le plus proche de la date (ex. samedi le plus proche du dimanche 11 → samedi 10)."""
    shift = (DAYS.index(written_day) - around.weekday() + 3) % 7 - 3  # entre -3 et +3 : un seul candidat
    return around + timedelta(days=shift)


def clarification(mismatch: dict) -> str:
    when, written = mismatch["date"], mismatch["written"]
    other = nearest(written, when)
    return (f"Petite précision : le {when.day} {MONTHS[when.month - 1]} est un {mismatch['real_day']}. "
            f"Vous pensiez au {label(other)} ou au {label(when)} ?")
