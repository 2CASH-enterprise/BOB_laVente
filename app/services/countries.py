"""
Lot 41 — pays et devises proposés à l'inscription (une seule source pour le serveur et la page).

Les pays sont ceux dont l'heure locale est connue (app/services/local_time.py) : un pays inconnu
mettrait la boutique à l'heure UTC et décalerait rendez-vous et rappels. La devise est proposée
selon le pays, et reste modifiable parmi la liste.
"""
from app.services.local_time import COUNTRY_TIMEZONES

COUNTRIES = [  # (code, nom, devise habituelle) — ordre d'affichage
    ("SN", "Sénégal", "XOF"), ("CI", "Côte d'Ivoire", "XOF"), ("ML", "Mali", "XOF"), ("BF", "Burkina Faso", "XOF"),
    ("BJ", "Bénin", "XOF"), ("TG", "Togo", "XOF"), ("NE", "Niger", "XOF"), ("GW", "Guinée-Bissau", "XOF"),
    ("GN", "Guinée", "GNF"), ("MR", "Mauritanie", "MRU"), ("GM", "Gambie", "GMD"), ("SL", "Sierra Leone", "SLE"),
    ("LR", "Liberia", "LRD"), ("GH", "Ghana", "GHS"), ("NG", "Nigeria", "NGN"),
    ("CM", "Cameroun", "XAF"), ("GA", "Gabon", "XAF"), ("CG", "Congo", "XAF"), ("TD", "Tchad", "XAF"),
    ("CF", "Centrafrique", "XAF"), ("GQ", "Guinée équatoriale", "XAF"), ("CD", "RD Congo", "CDF"),
    ("MA", "Maroc", "MAD"), ("DZ", "Algérie", "DZD"), ("TN", "Tunisie", "TND"),
    ("FR", "France", "EUR"), ("BE", "Belgique", "EUR"), ("LU", "Luxembourg", "EUR"), ("CH", "Suisse", "CHF"),
    ("CA", "Canada", "CAD"),
]

CURRENCIES = {
    "XOF": "Franc CFA (XOF)", "XAF": "Franc CFA (XAF)", "EUR": "Euro (EUR)", "USD": "Dollar américain (USD)",
    "GNF": "Franc guinéen (GNF)", "MRU": "Ouguiya (MRU)", "GMD": "Dalasi (GMD)", "SLE": "Leone (SLE)",
    "LRD": "Dollar libérien (LRD)", "GHS": "Cedi (GHS)", "NGN": "Naira (NGN)", "CDF": "Franc congolais (CDF)",
    "MAD": "Dirham (MAD)", "DZD": "Dinar algérien (DZD)", "TND": "Dinar tunisien (TND)", "CHF": "Franc suisse (CHF)",
    "CAD": "Dollar canadien (CAD)",
}

COUNTRY_CODES = frozenset(code for code, _, _ in COUNTRIES)

assert COUNTRY_CODES <= set(COUNTRY_TIMEZONES), "chaque pays proposé doit avoir son heure locale"
assert all(currency in CURRENCIES for _, _, currency in COUNTRIES)


def signup_options() -> dict:
    return {
        "countries": [{"code": c, "name": n, "currency": cur} for c, n, cur in COUNTRIES],
        "currencies": [{"code": c, "label": label} for c, label in CURRENCIES.items()],
        "default_country": "SN",
    }
