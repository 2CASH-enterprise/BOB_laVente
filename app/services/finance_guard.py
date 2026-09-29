"""
Garde-fou financement et reprise (lot 24, concession automobile).

Un chiffre de financement (mensualité, taux, apport, durée de crédit) ou une valeur de reprise
annoncés par Bob engageraient la concession — et en France le crédit est très encadré. La
consigne ne suffit pas : le code relit chaque réponse de Bob et retire la réponse entière si elle
contient un tel chiffre (même principe que promise_guard, lot 16).

Le prix affiché d'un véhicule reste autorisé : seuls les chiffres liés au financement ou à la
reprise sont visés.
"""
import re

_CURRENCY = r"(?:€|eur\b|euros?\b|fcfa\b|f\s?cfa\b|cfa\b|xof\b|francs?\b)"
_AMOUNT = r"\d[\d\s.,]*"

_PATTERNS = [
    # « 350 € par mois », « 45 000 FCFA/mois », « 199 €/mois »
    re.compile(rf"{_AMOUNT}\s?{_CURRENCY}?\s*(?:par\s+mois|/\s?mois|mensuel)"),
    # « mensualités de 320 € », « une mensualité à partir de 199 »
    re.compile(r"mensualit\w*[^.!?\n]{0,40}\d"),
    # « taux de 4,9 % », « TAEG 5,2 % »
    re.compile(r"\b(?:taux|taeg|tan)\b[^.!?\n]{0,40}\d"),
    # « 3,9 % d'intérêt »
    re.compile(r"\d+(?:[.,]\d+)?\s?%[^.!?\n]{0,30}(?:intér[eê]t|taux|cr[eé]dit|financement)"),
    # « un apport de 2 000 € »
    re.compile(r"\bapports?\b[^.!?\n]{0,40}\d"),
    # « sur 48 mois » quand on parle de crédit, LOA ou LLD
    re.compile(r"(?:cr[eé]dit|financ\w*|\bloa\b|\blld\b|leasing|location avec option)[^.!?\n]{0,60}\b\d{2,3}\s?mois\b"),
    re.compile(r"\b\d{2,3}\s?mois\b[^.!?\n]{0,60}(?:cr[eé]dit|financ\w*|\bloa\b|\blld\b|leasing)"),
    # Valeur de reprise chiffrée : le mot vient AVANT le montant (« on reprend votre Clio 6 000 € »).
    # « La 3008 est à 24 900 €, on estimera votre Clio sur place » n'est pas visé.
    re.compile(
        rf"(?:reprise|rachat|rach[eè]t\w*|racheter|reprend\w*|reprenons|estim\w*|\bcote\b)[^.!?\n]{{0,60}}?{_AMOUNT}\s?{_CURRENCY}"
    ),
]

FINANCE_MESSAGE = (
    "Pour le financement comme pour la reprise, un conseiller vous fera une proposition personnalisée, "
    "sans engagement. Je lui transmets votre demande : il reviendra vers vous directement."
)
FINANCE_MESSAGE_NO_TRANSFER = (
    "Pour le financement comme pour la reprise, votre conseiller vous fera une proposition personnalisée "
    "lors de votre visite, sans engagement."
)


def _normalize(text: str) -> str:
    return (text or "").lower().replace("’", "'").replace(" ", " ").replace(" ", " ")


def contains_financing_figure(text: str) -> bool:
    normalized = _normalize(text)
    return any(pattern.search(normalized) for pattern in _PATTERNS)
