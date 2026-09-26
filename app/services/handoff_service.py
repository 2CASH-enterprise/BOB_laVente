"""
Fiabilité de la prise en main humaine.

1. Mémoire de conversation lue par l'IA : elle commence au DERNIER transfert vers un humain.
   Les messages système de transfert ne sont jamais montrés au LLM ; sans ce découpage, Bob
   relisait une demande déjà transmise (ex. une négociation) et la retransmettait en boucle,
   même en réponse à un simple « Bonjour ». Règle déterministe, indépendante du LLM.
   Le dashboard, lui, affiche toujours l'historique complet.

2. Alerte email au commerçant quand Bob transmet une conversation (le client attend), et
   rappel si le client relance pendant l'attente — au plus une fois par heure et par
   conversation. Jamais d'alerte pour une prise de contrôle manuelle (l'humain est là).
"""
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer

# Événements qui font passer une conversation à un humain.
TRANSFER_MESSAGE_TYPES = frozenset({"handoff", "takeover", "negotiation_escalated"})

ALERT_REMINDER_INTERVAL = timedelta(hours=1)
_EXCERPT_LENGTH = 200


def is_transfer_event(message: Message) -> bool:
    return message.sender == MessageSender.SYSTEM and message.message_type in TRANSFER_MESSAGE_TYPES


def history_since_last_transfer(history: list[Message]) -> list[Message]:
    """`history` est chronologique. Renvoie uniquement ce qui suit le dernier transfert."""
    for index in range(len(history) - 1, -1, -1):
        if is_transfer_event(history[index]):
            return history[index + 1:]
    return history


def customer_display_name(customer: Customer) -> str:
    full_name = " ".join(part for part in (customer.first_name, customer.last_name) if part)
    number = f"+{customer.whatsapp_number}" if customer.whatsapp_number else ""
    if full_name and number:
        return f"{full_name} ({number})"
    return full_name or number or "Client"


def conversation_link(conversation: Conversation) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/dashboard/?conversation={conversation.id}"


def build_handoff_alert(customer: Customer, conversation: Conversation, reason: str | None) -> tuple[str, str]:
    name = customer_display_name(customer)
    subject = f"Un client attend votre réponse : {name}"
    body = (
        "Bonjour,\n\n"
        "Bob vient de transmettre une conversation à un humain : le client attend votre réponse.\n\n"
        f"Client : {name}\n"
        f"Raison : {reason or 'Non précisée'}\n\n"
        "Bob ne répondra plus à ce client tant que vous ne lui aurez pas rendu la conversation "
        "(bouton « Rendre à l'IA »).\n\n"
        f"Ouvrir la conversation : {conversation_link(conversation)}"
    )
    return subject, body


def build_reminder_alert(customer: Customer, conversation: Conversation, last_message: str) -> tuple[str, str]:
    name = customer_display_name(customer)
    excerpt = last_message.strip()
    if len(excerpt) > _EXCERPT_LENGTH:
        excerpt = excerpt[:_EXCERPT_LENGTH].rstrip() + "…"
    subject = f"Relance : {name} attend toujours votre réponse"
    body = (
        "Bonjour,\n\n"
        f"{name} vous a de nouveau écrit et attend toujours une réponse humaine.\n\n"
        f"Son dernier message : « {excerpt} »\n\n"
        f"Ouvrir la conversation : {conversation_link(conversation)}"
    )
    return subject, body


def reminder_due(conversation: Conversation, now: datetime | None = None) -> bool:
    """
    Rappel uniquement pour une conversation transmise PAR BOB (aucun agent assigné : une prise
    de contrôle manuelle a toujours un agent), et au plus une fois par heure.
    """
    if conversation.status != ConversationStatus.WAITING_HUMAN or conversation.assigned_agent is not None:
        return False
    last = conversation.human_alert_sent_at
    if last is None:
        return True
    if last.tzinfo is None:  # SQLite (tests) renvoie parfois une valeur naïve
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last >= ALERT_REMINDER_INTERVAL


def build_outage_alert(tenant_name: str) -> tuple[str, str]:
    """Panne du service d'IA, option « renvoyez votre message » : au plus un email par heure."""
    subject = "Bob a rencontré une panne technique"
    body = (
        "Bonjour,\n\n"
        f"Le service d'intelligence artificielle de Bob est momentanément indisponible pour {tenant_name}.\n"
        "Les clients qui écrivent reçoivent un message d'excuse les invitant à renvoyer leur message "
        "dans quelques minutes. Bob reprendra automatiquement dès que le service sera rétabli.\n\n"
        "Vous recevez au plus un email de ce type par heure. Si vous préférez rappeler vous-même les "
        "clients pendant une panne, choisissez « Un conseiller rappelle le client » dans Paramètres → "
        "Transmission à un humain."
    )
    return subject, body


def build_callback_alert(customer: Customer, conversation: Conversation, last_message: str) -> tuple[str, str]:
    """Panne du service d'IA, option « rappel » : un email PAR client, avec son numéro."""
    name = customer_display_name(customer)
    excerpt = (last_message or "").strip()
    if len(excerpt) > _EXCERPT_LENGTH:
        excerpt = excerpt[:_EXCERPT_LENGTH].rstrip() + "…"
    subject = f"À rappeler : {name}"
    body = (
        "Bonjour,\n\n"
        "Bob est momentanément en panne. Ce client a reçu le message « un conseiller va vous rappeler » : "
        "merci de l'appeler au plus vite.\n\n"
        f"Client : {name}\n"
        f"Numéro à appeler : +{customer.whatsapp_number}\n"
        f"Son dernier message : « {excerpt} »\n\n"
        f"Voir la conversation : {conversation_link(conversation)}"
    )
    return subject, body
