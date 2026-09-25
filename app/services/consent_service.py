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


# Sources de RETRAIT — tracées comme les sources de consentement : en cas de litige, il faut
# pouvoir montrer non seulement QUAND mais COMMENT le client s'est désinscrit.
WITHDRAWN_VIA_EMAIL_LINK = "EMAIL_LINK"
WITHDRAWN_VIA_WHATSAPP_KEYWORD = "WHATSAPP_KEYWORD"
WITHDRAWN_VIA_AI = "AI_DETECTED"
WITHDRAWN_MANUALLY = "MANUAL"


def grant_marketing_consent(customer: Customer, source: str, now: datetime | None = None) -> bool:
    """source : "AI_ASKED" (Bob a demandé et le client a répondu oui) ou "MANUAL" (un humain coche).

    Idempotent : un client DÉJÀ consentant n'est pas modifié — sinon la preuve d'origine
    (date + source) serait écrasée à chaque réaffirmation. Renvoie True si l'état a changé.
    """
    if customer.marketing_consent:
        return False
    now = now or datetime.now(timezone.utc)
    customer.marketing_consent = True
    customer.marketing_consent_given_at = now
    customer.marketing_consent_source = source
    customer.marketing_consent_withdrawn_at = None  # un nouveau consentement supplante un retrait précédent
    customer.marketing_consent_withdrawn_source = None
    return True


def withdraw_marketing_consent(customer: Customer, source: str, now: datetime | None = None) -> bool:
    """given_at/source sont conservés : trace historique de quand/comment il avait été donné.

    Un client qui n'a jamais consenti peut quand même exprimer un refus (STOP) : il est
    enregistré. En revanche, un retrait DÉJÀ enregistré n'est jamais écrasé (la première
    date et la première source font foi). Renvoie True si quelque chose a été enregistré.
    """
    if not customer.marketing_consent and customer.marketing_consent_withdrawn_at is not None:
        return False
    now = now or datetime.now(timezone.utc)
    customer.marketing_consent = False
    customer.marketing_consent_withdrawn_at = now
    customer.marketing_consent_withdrawn_source = source
    return True
