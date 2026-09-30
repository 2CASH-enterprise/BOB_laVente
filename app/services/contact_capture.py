"""
Lot 34 — email et accord pour les offres du client final.

Principes :
- c'est le CODE qui décide du moment où Bob demande l'email (juste après un rendez-vous ou une
  commande : le client y gagne un rappel ou un reçu), jamais l'IA d'elle-même ;
- la question n'est posée qu'UNE fois par client (date enregistrée) : un refus ou un silence
  n'entraîne jamais de relance ;
- un email n'est enregistré que s'il est valide ; s'il est écrit par le client dans un message
  alors que sa fiche n'en a pas, le code l'enregistre directement, sans dépendre de l'IA ;
- l'accord pour les offres reste distinct de l'email (avoir donné son email pour un rappel ne
  vaut jamais accord pour recevoir des offres).
"""
import re
from datetime import datetime, timezone

from pydantic import EmailStr, TypeAdapter, ValidationError

SOURCE_BOB = "BOB"  # donné en réponse à Bob, enregistré par son outil
SOURCE_MESSAGE = "WHATSAPP_MESSAGE"  # écrit par le client, repéré par le code
SOURCE_MANUAL = "MANUAL"

_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL = TypeAdapter(EmailStr)

REASON_APPOINTMENT = "APPOINTMENT"
REASON_ORDER = "ORDER"
CONSENT_SOURCE_KEYWORD = "WHATSAPP_OFFRES"  # le client a répondu « OFFRES »

# Lot 34b — la demande part en MESSAGE FIXE envoyé par le code (test réel du 30/09 : l'IA recevait
# la consigne mais ne posait pas la question).
_ASK_TEXT = {
    REASON_APPOINTMENT: "Souhaitez-vous recevoir un rappel par email la veille de votre rendez-vous ? "
                        "Si oui, répondez simplement avec votre adresse email.",
    REASON_ORDER: "Souhaitez-vous recevoir le récapitulatif de votre commande par email ? "
                  "Si oui, répondez simplement avec votre adresse email.",
}
_OFFERS_TEXT = "Pour recevoir aussi nos offres et nouveautés par email, ajoutez le mot OFFRES à votre réponse."
NOTE_FOR_BOB = ("Un message automatique propose au client, juste après ta réponse, de laisser son email : "
                "ne lui demande pas toi-même son email ni son accord pour les offres.")


def valid_email(value) -> str | None:
    if not isinstance(value, str) or len(value) > 254:
        return None
    try:
        return str(_EMAIL.validate_python(value.strip())).lower()
    except ValidationError:
        return None


def extract_email(text: str | None) -> str | None:
    """Un seul email valide dans le message ; plusieurs ou aucun : rien (pas de devinette)."""
    found = {m.group(0).rstrip(".").lower() for m in _EMAIL_IN_TEXT.finditer(text or "")}
    if len(found) != 1:
        return None
    return valid_email(found.pop())


def record_email(customer, email: str, source: str, now: datetime | None = None) -> None:
    customer.email = email
    customer.email_source = source
    customer.email_collected_at = now or datetime.now(timezone.utc)


def consent_state(customer) -> str:
    if customer.marketing_consent:
        return "ACCEPTED"
    if customer.marketing_consent_withdrawn_at is not None or customer.marketing_consent_given_at is not None:
        return "REFUSED"
    return "NEVER_ASKED"


def request_email(customer, reason: str, now: datetime | None = None) -> str | None:
    """
    Message fixe à envoyer au client après la réponse de Bob, ou None si rien n'est à demander
    (email connu, ou déjà demandé une fois). Marque la demande comme faite.
    """
    if customer is None or customer.email or customer.email_requested_at is not None:
        return None
    customer.email_requested_at = now or datetime.now(timezone.utc)
    text = _ASK_TEXT[reason]
    if consent_state(customer) == "NEVER_ASKED":
        text += "\n\n" + _OFFERS_TEXT
    return text


_OPT_IN_WORDS = {"offres", "offre", "oui", "ok", "okay", "merci", "et", "daccord", "pour", "les", "svp", "stp", "bien", "sur", "volontiers"}


def is_offers_opt_in(text: str | None) -> bool:
    """
    « OFFRES », « oui OFFRES », « awa@example.com OFFRES » : un accord explicite. « Vous avez des
    offres ? » n'en est pas un (d'autres mots que ceux d'un simple oui).
    """
    import unicodedata

    cleaned = _EMAIL_IN_TEXT.sub(" ", text or "")
    cleaned = unicodedata.normalize("NFKD", cleaned).encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z]+", cleaned.replace("'", ""))
    return bool(words) and ("offres" in words or "offre" in words) and set(words) <= _OPT_IN_WORDS \
        and "?" not in (text or "")


def opt_in_reply(email_known: bool) -> str:
    if email_known:
        return ("C'est noté ✅ Vous recevrez nos offres par email. Vous pouvez vous désinscrire à tout moment "
                "en répondant STOP.")
    return ("C'est noté ✅ Pour recevoir nos offres, indiquez-nous votre adresse email. Vous pouvez vous "
            "désinscrire à tout moment en répondant STOP.")


def contact_lines(customer) -> list[str]:
    """Ce que Bob doit savoir pour ne jamais reposer une question déjà posée."""
    if customer is None:
        return []
    lines = []
    if customer.email:
        lines.append("Email du client : déjà connu (ne le redemande pas, ne l'écris jamais en entier).")
    elif customer.email_requested_at is not None:
        lines.append("Email du client : déjà demandé une fois (ne le redemande pas).")
    state = consent_state(customer)
    if state == "ACCEPTED":
        lines.append("Offres par email : le client a accepté.")
    elif state == "REFUSED":
        lines.append("Offres par email : le client a refusé ou s'est désinscrit (ne repose jamais la question).")
    return lines
