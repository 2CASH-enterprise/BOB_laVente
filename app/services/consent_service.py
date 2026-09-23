"""
Consentement marketing (section CRM). Un booléen seul ne prouve rien : on trace
systématiquement QUAND, COMMENT (source), et si retiré, QUAND. Toute la logique passe
ici — jamais un `customer.marketing_consent = True` écrit ailleurs dans le code.
"""
import re
from datetime import datetime, timezone

from app.models.customer import Customer

# Mots-clés de désinscription reconnus indépendamment de la casse/accents — la
# reconnaissance doit être fiable et déterministe, jamais laissée à l'appréciation du LLM.
_OPT_OUT_PATTERN = re.compile(
    r"^\s*(stop|arret|arrêt|desinscrire|désinscrire|unsubscribe|stop\s*pub)\s*[.!]?\s*$", re.IGNORECASE
)


def is_opt_out_message(text: str) -> bool:
    return bool(_OPT_OUT_PATTERN.match(text or ""))


def grant_marketing_consent(customer: Customer, source: str, now: datetime | None = None) -> None:
    """source : "AI_ASKED" (Bob a demandé et le client a répondu oui) ou "MANUAL" (un humain coche)."""
    now = now or datetime.now(timezone.utc)
    customer.marketing_consent = True
    customer.marketing_consent_given_at = now
    customer.marketing_consent_source = source
    customer.marketing_consent_withdrawn_at = None  # un nouveau consentement supplante un retrait précédent


def withdraw_marketing_consent(customer: Customer, now: datetime | None = None) -> None:
    """given_at/source sont conservés : trace historique de quand/comment il avait été donné."""
    now = now or datetime.now(timezone.utc)
    customer.marketing_consent = False
    customer.marketing_consent_withdrawn_at = now
