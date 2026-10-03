"""
Lot 51 — Bob en pause pour TOUTE la boutique. Un seul endroit pour la règle, utilisée par le webhook,
les relances, les campagnes, les rappels de rendez-vous, le tableau de bord et le Super Admin.

Deux raisons :
- SUSPENDED : le Super Admin a suspendu la boutique (bouton « Suspendre »), effet immédiat ;
- UNPAID : abonnement non renouvelé. `paid_until` est le dernier jour payé ; le lendemain est un jour
  de grâce (Bob répond encore, rappel par email) ; Bob se met en pause le surlendemain. Les dates
  sont celles du pays de la boutique. Sans date (plan gratuit, démo), jamais de pause pour impayé.

Bob en pause : plus de réponse automatique ni d'analyse des messages (aucun coût d'IA), plus de relance,
de campagne ni de rappel de rendez-vous. Les messages des clients sont toujours enregistrés, et le
commerçant peut toujours leur répondre lui-même. Rien n'est envoyé au client.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.services.local_time import _MONTHS, as_utc, tenant_zone

SUSPENDED = "SUSPENDED"
UNPAID = "UNPAID"

GRACE_DAYS = 1    # jours où Bob répond encore après le dernier jour payé
WARNING_DAYS = 7  # premier rappel, 7 jours avant la fin de l'abonnement

# Étapes de l'échéance, dans l'ordre : un seul email par étape et par date d'échéance.
WARNING = "WARNING"
GRACE = "GRACE"
PAUSED = "PAUSED"
STAGE_ORDER = {WARNING: 1, GRACE: 2, PAUSED: 3}


def local_today(tenant, now: datetime | None = None) -> date:
    return as_utc(now or datetime.now(timezone.utc)).astimezone(tenant_zone(tenant)).date()


def pause_starts_on(paid_until: date) -> date:
    return paid_until + timedelta(days=GRACE_DAYS + 1)


def billing_stage(tenant, now: datetime | None = None) -> str | None:
    paid_until = getattr(tenant, "paid_until", None)
    if paid_until is None:
        return None
    today = local_today(tenant, now)
    if today >= pause_starts_on(paid_until):
        return PAUSED
    if today > paid_until:
        return GRACE
    if (paid_until - today).days <= WARNING_DAYS:
        return WARNING
    return None


def pause_reason(tenant, now: datetime | None = None) -> str | None:
    if tenant is None:
        return None
    if not tenant.active:
        return SUSPENDED
    if billing_stage(tenant, now) == PAUSED:
        return UNPAID
    return None


def is_paused(tenant, now: datetime | None = None) -> bool:
    return pause_reason(tenant, now) is not None


def french_date(value: date) -> str:
    day = "1er" if value.day == 1 else str(value.day)
    return f"{day} {_MONTHS[value.month - 1]} {value.year}"


PAUSED_ACTION_MESSAGE = {
    SUSPENDED: "Bob est en pause : votre compte est suspendu. Contactez-nous pour le réactiver.",
    UNPAID: "Bob est en pause : votre abonnement est à renouveler. Tout reprendra dès le renouvellement.",
}


@dataclass(frozen=True)
class BobStatus:
    paused: bool
    reason: str | None
    stage: str | None
    paid_until: date | None
    pause_on: date | None
    message: str | None

    def as_dict(self) -> dict:
        return {
            "paused": self.paused, "reason": self.reason, "stage": self.stage,
            "paid_until": self.paid_until.isoformat() if self.paid_until else None,
            "pause_on": self.pause_on.isoformat() if self.pause_on else None,
            "message": self.message,
        }


def status(tenant, now: datetime | None = None) -> BobStatus:
    """Ce que le tableau de bord (bandeau) et le Super Admin affichent."""
    reason = pause_reason(tenant, now)
    stage = billing_stage(tenant, now)
    paid_until = getattr(tenant, "paid_until", None)
    pause_on = pause_starts_on(paid_until) if paid_until else None
    if reason == SUSPENDED:
        message = ("Bob est en pause : votre compte est suspendu. Les messages de vos clients arrivent toujours "
                   "dans Conversations et vous pouvez leur répondre vous-même. Contactez-nous pour le réactiver.")
    elif reason == UNPAID:
        message = (f"Bob est en pause depuis le {french_date(pause_on)} : votre abonnement est à renouveler. "
                   "Les messages de vos clients arrivent toujours dans Conversations et vous pouvez leur répondre "
                   "vous-même. Bob reprendra automatiquement dès le renouvellement.")
    elif stage == GRACE:
        message = (f"Votre abonnement est arrivé à échéance le {french_date(paid_until)}. Bob répond encore "
                   f"aujourd'hui, mais il se mettra en pause le {french_date(pause_on)} si le paiement n'est pas reçu.")
    elif stage == WARNING:
        message = (f"Votre abonnement se termine le {french_date(paid_until)}. Pensez à le renouveler pour que Bob "
                   "continue de répondre à vos clients sans interruption.")
    else:
        message = None
    return BobStatus(paused=reason is not None, reason=reason, stage=stage, paid_until=paid_until,
                     pause_on=pause_on, message=message)


def billing_email(tenant, stage: str) -> tuple[str, str]:
    """Email au commerçant pour une étape de l'échéance (WARNING / GRACE / PAUSED)."""
    paid_until = tenant.paid_until
    end, pause_on = french_date(paid_until), french_date(pause_starts_on(paid_until))
    if stage == WARNING:
        subject = f"Votre abonnement Bob se termine le {end}"
        text = (f"Votre abonnement Bob pour {tenant.name} est réglé jusqu'au {end}.\n\n"
                "Pour que Bob continue de répondre à vos clients sans interruption, pensez à le renouveler "
                f"avant cette date. Sans renouvellement, Bob se mettra en pause le {pause_on}.")
    elif stage == GRACE:
        subject = "Dernier rappel : Bob se met en pause demain"
        text = (f"Votre abonnement Bob pour {tenant.name} est arrivé à échéance le {end}.\n\n"
                f"Bob répond encore à vos clients aujourd'hui, mais il se mettra en pause demain ({pause_on}) "
                "si le paiement n'est pas reçu.")
    else:
        subject = "Bob est en pause : abonnement à renouveler"
        text = (f"Depuis le {pause_on}, Bob ne répond plus automatiquement aux clients de {tenant.name} : "
                "votre abonnement n'a pas été renouvelé.\n\n"
                "Les messages de vos clients arrivent toujours dans Conversations, et vous pouvez leur répondre "
                "vous-même. Les relances, les campagnes et les rappels de rendez-vous sont aussi en pause.\n\n"
                "Dès le renouvellement, Bob reprend automatiquement.")
    body = ("Bonjour,\n\n" + text + "\n\nPour renouveler ou pour toute question, répondez simplement à cet email.\n\n"
            "Vos conversations et vos données restent conservées.\n\n— L'équipe Bob")
    return subject, body


def notice_due(tenant, now: datetime | None = None) -> str | None:
    """Étape dont l'email n'est pas encore parti pour cette date d'échéance (jamais deux fois la même)."""
    if tenant is None or not tenant.active or getattr(tenant, "is_demo", False):
        return None
    stage = billing_stage(tenant, now)
    if stage is None:
        return None
    if tenant.billing_notice_for == tenant.paid_until and \
            STAGE_ORDER.get(tenant.billing_notice_stage or "", 0) >= STAGE_ORDER[stage]:
        return None
    return stage
