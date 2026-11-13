"""
Lot 121 — « Mon équipe », l'entrée par Bob (décisions de Thérèse du 13/11) : le commerçant choisit « Bob seul » ou
« Bob + Naomy » dès l'inscription (rien de présélectionné), et peut changer plus tard dans la rubrique « Mon équipe ».

- « Rencontrer Naomy » : un clic prépare la liaison (portée complète, assistant de Bob allumé d'office) et ouvre soit
  WhatsApp avec le code de liaison déjà écrit (réglage NAOMY_PAIRING_WHATSAPP, quand Naomy sait le lire), soit la page
  habituelle de Naomy. Plus de code à recopier ni de « portée » à choisir.
- Formule gratuite : essai équipe de 7 jours (formule Indépendant, puis rappels et pause habituels du lot 51), une
  seule fois par boutique, démarré au premier « Rencontrer Naomy » — jamais à l'insu du commerçant (`confirmer`).
- « Juste être prévenu sur WhatsApp » (l'assistant seul, toutes formules) reste possible, sous la carte Naomy.
- Une boutique déjà reliée à Naomy est « en équipe » (migration, puis à chaque liaison complète).
"""
from datetime import datetime, timezone
from urllib.parse import quote

SOLO, TEAM = "SOLO", "TEAM"
MODES = {SOLO, TEAM}
CODE_LABEL = "Code de liaison"


class TeamError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def trial_possible(tenant) -> bool:
    """Essai équipe : seulement en formule gratuite, jamais payé, jamais essayé."""
    from app.models.tenant import TenantPlan

    return tenant.plan == TenantPlan.FREE and tenant.paid_until is None and tenant.team_trial_at is None \
        and not tenant.is_demo


def whatsapp_link(tenant, code: str) -> str | None:
    """wa.me vers le numéro de Naomy, message déjà écrit ; None tant que le réglage est éteint ou sans numéro."""
    import re

    from app.core.config import get_settings

    settings = get_settings()
    number = re.sub(r"\D", "", settings.naomy_whatsapp_number or "")
    if not settings.naomy_pairing_whatsapp or len(number) < 8:
        return None
    text = f"Bonjour Naomy 👋 Je relie ma boutique « {tenant.name} » à Bob.\n{CODE_LABEL} : {code}"
    return f"https://wa.me/{number}?text={quote(text)}"


async def view(db, tenant) -> dict:
    from app.services import naomy_bridge as nb

    naomy = await nb.dashboard_view(db, tenant)
    linked = bool(naomy["connected"] and naomy["scope"] == "complete")
    if linked:
        state = "linked"
    elif naomy["connected"]:
        state = "assistant"  # « juste être prévenu sur WhatsApp »
    elif naomy["request"]:
        state = "request"
    elif nb.pairing_refusal(tenant, "assistant") is not None:
        state = "unavailable"  # passerelle éteinte, ou démonstration
    else:
        state = "absent"
    trial = trial_possible(tenant)
    return {"mode": tenant.team_mode if tenant.team_mode in MODES else None,
            "naomy": {**naomy, "state": state,
                      # « Rencontrer Naomy » possible : formule suffisante, ou essai équipe encore disponible.
                      "can_meet": state in ("absent", "assistant", "request") and (naomy["refusal"] is None or trial),
                      "trial": trial and naomy["refusal"] is not None and state != "unavailable"},
            "trial_until": tenant.paid_until.isoformat() if tenant.team_trial_at and tenant.paid_until else None}


async def choose(db, tenant, mode: str) -> dict:
    if mode not in MODES:
        raise TeamError(422, "Choix attendu : Bob seul, ou Bob + Naomy.")
    tenant.team_mode = mode
    await db.commit()
    return await view(db, tenant)


async def meet_naomy(db, tenant, confirm_trial: bool = False, request_id: str | None = None,
                     now: datetime | None = None) -> dict:
    """Prépare la liaison complète (et l'essai équipe si besoin) ; renvoie le lien à ouvrir."""
    from app.services import naomy_bridge as nb
    from app.services.audit import log_audit_event
    from app.services.naomy_accounts import start_trial

    now = now or datetime.now(timezone.utc)
    refusal = nb.pairing_refusal(tenant)
    if refusal is not None:
        if nb.pairing_refusal(tenant, "assistant") is not None or not trial_possible(tenant):
            raise TeamError(403, refusal)
        if not confirm_trial:
            raise TeamError(409, "essai_a_confirmer")
        start_trial(tenant, now)
        tenant.team_trial_at = now
        await log_audit_event(db, actor="TENANT", action="TEAM_TRIAL_STARTED", tenant_id=tenant.id,
                              details={"until": tenant.paid_until.isoformat()})
    tenant.team_mode = TEAM  # avant la liaison : l'assistant sera allumé d'office (naomy_bridge.pair)
    if request_id:
        try:
            url = await nb.decide_request(db, tenant, request_id, True, "complete", now)
        except LookupError as exc:
            raise TeamError(404, str(exc)) from None
    else:
        url = await nb.create_pairing(db, tenant, now, scope="complete")
    code = url.rsplit("code=", 1)[-1]
    wa = whatsapp_link(tenant, code)
    return {"url": wa or url, "by_whatsapp": wa is not None,
            "trial_until": tenant.paid_until.isoformat() if tenant.team_trial_at and tenant.paid_until else None}
