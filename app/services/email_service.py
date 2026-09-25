"""
Envoi d'email (2FA, mot de passe oublié, campagnes). Si aucun SMTP n'est configuré, le
contenu est journalisé côté serveur plutôt que silencieusement perdu — permet de tester
la fonctionnalité avant d'avoir configuré un vrai prestataire email en production.

Expéditeur :
- l'ADRESSE d'envoi est toujours `smtp_from_email` (seul domaine authentifié DKIM/DMARC) ;
- le NOM affiché est `smtp_from_name` par défaut (« Bob AI »), ou celui passé par l'appelant
  (ex. le nom du commerce pour une campagne) ;
- `reply_to` permet de diriger les réponses ailleurs (ex. vers le commerçant).
"""
import logging
import re
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_MAX_DISPLAY_NAME_LENGTH = 100
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_display_name(name: str | None) -> str:
    """Le nom peut venir d'une saisie utilisateur (nom du commerce) : aucun caractère de
    contrôle (dont CR/LF, vecteur d'injection d'en-têtes), espaces normalisés, longueur bornée."""
    if not name:
        return ""
    cleaned = _CONTROL_CHARS.sub(" ", name)
    # Pas de chevrons : un nom comme « Boutique <support@banque.com> » tromperait visuellement le destinataire.
    cleaned = cleaned.replace("<", " ").replace(">", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned[:_MAX_DISPLAY_NAME_LENGTH].strip()


def _sanitize_reply_to(address: str | None) -> str | None:
    """Accepte uniquement une adresse simple et plausible ; sinon l'en-tête est omis (jamais bloquant)."""
    if not address or _CONTROL_CHARS.search(address):
        return None
    _, parsed = parseaddr(address)
    if not parsed or "@" not in parsed or parsed != address.strip():
        return None
    return parsed


def build_message(to: str, subject: str, body: str, from_name: str | None = None, reply_to: str | None = None) -> MIMEText:
    settings = get_settings()
    display_name = _sanitize_display_name(from_name if from_name is not None else settings.smtp_from_name)

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((display_name, settings.smtp_from_email), charset="utf-8") if display_name else settings.smtp_from_email
    msg["To"] = to

    clean_reply_to = _sanitize_reply_to(reply_to)
    if clean_reply_to:
        msg["Reply-To"] = clean_reply_to
    elif reply_to:
        logger.warning("Reply-To ignoré (adresse invalide) pour l'email à %s", to)
    return msg


def send_email(to: str, subject: str, body: str, from_name: str | None = None, reply_to: str | None = None) -> bool:
    settings = get_settings()

    if not settings.smtp_host:
        logger.warning(
            "[SMTP non configuré] Email non envoyé à %s — sujet: %s — corps: %s", to, subject, body
        )
        return False

    msg = build_message(to, subject, body, from_name=from_name, reply_to=reply_to)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            # L'enveloppe SMTP utilise toujours l'adresse brute (jamais le nom affiché).
            server.sendmail(settings.smtp_from_email, [to], msg.as_string())
        return True
    except Exception:  # noqa: BLE001 — un échec d'envoi ne doit jamais faire planter l'appelant
        logger.exception("Échec de l'envoi d'email à %s", to)
        return False
