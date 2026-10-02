"""
Lot 37b — tâches à faire et notifications sur l'appareil du commerçant.

Le NOMBRE affiché (pastille rouge sur l'icône Bob, compteurs du menu) = ce qu'il reste à faire.
Il ne baisse que quand la tâche est faite, jamais parce qu'elle a été vue. Mêmes règles que la
liste « À traiter maintenant » de l'accueil (home_service), chaque activité ne voit que ce qui la
concerne :
- conversations à reprendre (en attente d'un humain) ;
- rendez-vous à confirmer, et rendez-vous passés sans issue (concession) ;
- commandes en attente (boutique en ligne) ;
- clients à rappeler (panne de Bob, relance après visite à faire à la main).

Notification (message + pastille même Bob fermé) : seulement quand une NOUVELLE tâche apparaît,
une fois. Le texte reste général (jamais le nom ni le message d'un client : il s'affiche sur
l'écran verrouillé). L'envoi passe par le service de notification du navigateur (Google, Apple,
Mozilla, Microsoft) : seules ces adresses sont acceptées, jamais une adresse interne.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.order import Order, OrderStatus
from app.models.push_subscription import NotificationState, PushSubscription
from app.services.business_type import CAR_DEALERSHIP

logger = logging.getLogger(__name__)

KINDS = ("conversations", "appointments", "outcomes", "orders", "callbacks")
DEALERSHIP_ONLY = frozenset({"appointments", "outcomes"})
STORE_ONLY = frozenset({"orders"})

# Titre de la notification selon le type de la nouvelle tâche (ordre = priorité).
TITLES = {
    "conversations": "Un client attend votre réponse",
    "appointments": "Nouveau rendez-vous à confirmer",
    "orders": "Nouvelle commande",
    "callbacks": "Client à rappeler",
    "outcomes": "Rendez-vous passé : issue à indiquer",
}

# Services de notification des navigateurs : jamais d'envoi vers une autre adresse.
PUSH_HOSTS = (
    "fcm.googleapis.com",          # Chrome, Edge Android, Samsung…
    "push.services.mozilla.com",   # Firefox (updates.push.services.mozilla.com)
    "push.apple.com",              # Safari, iPhone (web.push.apple.com)
    "notify.windows.com",          # Edge Windows (*.notify.windows.com)
)
MAX_FAILURES = 5
MAX_DEVICES_PER_USER = 10
TTL_SECONDS = 24 * 3600


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------------------------
# Compteurs
# ---------------------------------------------------------------------------------------------

async def task_counts(db, tenant, now: datetime | None = None) -> dict:
    """Nombre de tâches à faire par type, pour CETTE boutique ; « total » = somme."""
    from app.models.appointment_request import STATUS_CONFIRMED, STATUS_REQUESTED, AppointmentRequest
    from app.services.appointment_outcome import open_tasks
    from app.services.handoff_rules import RULE_LABELS
    from app.services.home_service import CALLBACK_WINDOW_HOURS

    now = _aware(now or datetime.now(timezone.utc))
    tenant_id = tenant.id
    dealership = tenant.business_type == CAR_DEALERSHIP
    counts = dict.fromkeys(KINDS, 0)

    waiting = set((await db.execute(select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, Conversation.status == ConversationStatus.WAITING_HUMAN,
    ))).scalars().all())
    counts["conversations"] = len(waiting)

    # Clients à rappeler après une panne de Bob (48 h), sauf si un humain a répondu depuis.
    callback_label = RULE_LABELS["AI_OUTAGE_CALLBACK"]
    outages = (await db.execute(select(Message.conversation_id, func.max(Message.created_at)).where(
        Message.tenant_id == tenant_id, Message.message_type == "ai_outage",
        Message.content.contains(callback_label),
        Message.created_at >= now - timedelta(hours=CALLBACK_WINDOW_HOURS),
    ).group_by(Message.conversation_id))).all()
    for conversation_id, at in outages:
        if conversation_id in waiting:
            continue  # déjà comptée comme conversation à reprendre
        answered = (await db.execute(select(func.count(Message.id)).where(
            Message.tenant_id == tenant_id, Message.conversation_id == conversation_id,
            Message.sender == MessageSender.HUMAN, Message.created_at > at,
        ))).scalar_one()
        if not answered:
            counts["callbacks"] += 1

    if dealership:
        requested = (await db.execute(select(AppointmentRequest.conversation_id).where(
            AppointmentRequest.tenant_id == tenant_id, AppointmentRequest.status == STATUS_REQUESTED,
        ))).scalars().all()
        counts["appointments"] = sum(1 for c in requested if c not in waiting)
        past = (await db.execute(select(AppointmentRequest).where(
            AppointmentRequest.tenant_id == tenant_id, AppointmentRequest.status == STATUS_CONFIRMED,
            AppointmentRequest.scheduled_at.is_not(None), AppointmentRequest.scheduled_at <= now - timedelta(hours=1),
        ))).scalars().all()
        counts["outcomes"] = sum(1 for a in past if a.outcome is None)
        counts["callbacks"] += len(open_tasks(past, now))
    else:
        counts["orders"] = (await db.execute(select(func.count(Order.id)).where(
            Order.tenant_id == tenant_id, Order.status == OrderStatus.PENDING,
        ))).scalar_one()

    counts["total"] = sum(counts[k] for k in KINDS)
    return counts


async def remember_counts(db, tenant_id, counts: dict) -> dict:
    """Enregistre les compteurs vus ; renvoie les précédents ({} la première fois)."""
    state = await db.get(NotificationState, tenant_id)
    if state is None:
        db.add(NotificationState(tenant_id=tenant_id, counts={k: counts[k] for k in KINDS}))
        return {}
    previous = dict(state.counts or {})
    current = {k: counts[k] for k in KINDS}
    if previous != current:
        state.counts = current
    return previous


def new_tasks(previous: dict, counts: dict) -> list[str]:
    """Types de tâches dont le nombre a augmenté, par priorité."""
    return [k for k in TITLES if counts.get(k, 0) > previous.get(k, 0)]


def build_payload(kinds: list[str], counts: dict) -> dict:
    total = counts["total"]
    return {
        "title": TITLES[kinds[0]],
        "body": f"{total} tâche{'s' if total > 1 else ''} à traiter dans Bob.",
        "count": total,
        "tag": "bob-tasks",
    }


# ---------------------------------------------------------------------------------------------
# Appareils
# ---------------------------------------------------------------------------------------------

def push_enabled() -> bool:
    settings = get_settings()
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def validate_endpoint(endpoint) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        raise ValueError("Adresse de notification invalide")
    url = urlparse(endpoint)
    host = (url.hostname or "").lower()
    if url.scheme != "https" or url.port not in (None, 443) or url.username or url.password:
        raise ValueError("Adresse de notification invalide")
    if not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        raise ValueError("Service de notification non reconnu")
    return endpoint


async def save_subscription(db, user, endpoint: str, p256dh: str, auth: str, user_agent: str | None) -> PushSubscription:
    """Un appareil = une adresse ; s'il change de compte, il passe au nouvel utilisateur."""
    sub = (await db.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))).scalar_one_or_none()
    if sub is None:
        sub = PushSubscription(endpoint=endpoint, tenant_id=user.tenant_id, user_id=user.id, p256dh=p256dh, auth=auth)
        db.add(sub)
    sub.tenant_id, sub.user_id, sub.p256dh, sub.auth = user.tenant_id, user.id, p256dh, auth
    sub.user_agent = (user_agent or "")[:255] or None
    sub.failures = 0
    await db.flush()
    # Au-delà de 10 appareils pour un même utilisateur, les plus anciens sont oubliés.
    others = (await db.execute(select(PushSubscription.id).where(PushSubscription.user_id == user.id)
                               .order_by(PushSubscription.created_at.desc(), PushSubscription.id))).scalars().all()
    stale = [i for i in others if i != sub.id][MAX_DEVICES_PER_USER - 1:]
    if stale:
        await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(stale)))
    return sub


async def forget_device(db, user_id, endpoint: str) -> None:
    await db.execute(delete(PushSubscription).where(
        PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint,
    ))


async def forget_user_devices(db, user_id) -> None:
    await db.execute(delete(PushSubscription).where(PushSubscription.user_id == user_id))


# ---------------------------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------------------------

def _send_webpush(sub: PushSubscription, data: str) -> int:
    """Envoi réel (bloquant) ; renvoie le code HTTP du service de notification."""
    from pywebpush import WebPushException, webpush

    settings = get_settings()
    try:
        response = webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=data, vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject or f"mailto:{settings.smtp_from_email}"},
            ttl=TTL_SECONDS, timeout=10,
        )
        return response.status_code
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 0


async def check_tenant(db, tenant_id, now: datetime | None = None, sender=None) -> dict:
    """
    Recompte les tâches d'une boutique ; si une nouvelle est apparue, prévient chaque appareil
    abonné (utilisateurs actifs). Les appareils disparus (404/410) ou en échec répété sont oubliés.
    """
    import asyncio

    from app.models.tenant import Tenant
    from app.models.user import User

    report = {"sent": 0, "removed": 0, "failed": 0, "new": []}
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.active:
        return report
    counts = await task_counts(db, tenant, now)
    previous = await remember_counts(db, tenant_id, counts)
    kinds = new_tasks(previous, counts)
    await db.commit()
    report["new"] = kinds
    if not kinds or not push_enabled():
        return report

    subs = (await db.execute(
        select(PushSubscription).join(User, User.id == PushSubscription.user_id)
        .where(PushSubscription.tenant_id == tenant_id, User.tenant_id == tenant_id, User.active.is_(True))
    )).scalars().all()
    data = json.dumps(build_payload(kinds, counts), ensure_ascii=False)
    send = sender or _send_webpush
    for sub in subs:
        try:
            code = await asyncio.to_thread(send, sub, data)
        except Exception:  # noqa: BLE001 — un appareil injoignable ne bloque jamais les autres
            code = 0
        if code in (404, 410):
            await db.delete(sub)
            report["removed"] += 1
        elif 200 <= code < 300:
            sub.failures, sub.last_sent_at = 0, _aware(now or datetime.now(timezone.utc))
            report["sent"] += 1
        else:
            sub.failures += 1
            report["failed"] += 1
            if sub.failures >= MAX_FAILURES:
                await db.delete(sub)
                report["removed"] += 1
    await db.commit()
    return report


def queue_check(tenant_id) -> None:
    """Après un message WhatsApp reçu : vérification en tâche de fond (jamais bloquant)."""
    if not push_enabled():
        return
    try:
        from app.workers.notifications import check_tenant_task

        check_tenant_task.delay(str(tenant_id))
    except Exception:  # noqa: BLE001
        logger.warning("Vérification des notifications non programmée (boutique %s)", tenant_id)
