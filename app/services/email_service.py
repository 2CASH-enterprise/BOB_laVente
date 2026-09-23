"""
Envoi d'email (2FA notamment). Si aucun SMTP n'est configuré, le contenu est journalisé
côté serveur plutôt que silencieusement perdu — permet de tester la fonctionnalité avant
d'avoir configuré un vrai prestataire email en production.
"""
import logging
import smtplib
from email.mime.text import MIMEText

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> bool:
    settings = get_settings()

    if not settings.smtp_host:
        logger.warning(
            "[SMTP non configuré] Email non envoyé à %s — sujet: %s — corps: %s", to, subject, body
        )
        return False

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_email
    msg["To"] = to

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.sendmail(settings.smtp_from_email, [to], msg.as_string())
        return True
    except Exception:  # noqa: BLE001 — un échec d'envoi ne doit jamais faire planter l'appelant
        logger.exception("Échec de l'envoi d'email à %s", to)
        return False
