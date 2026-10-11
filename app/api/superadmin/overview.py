"""
Lot 66 — Super Admin : « Vue d'ensemble » (chiffres clés, à faire du jour, parcours des prospects, activité récente)
et historique d'une boutique (réglages changés, inscription, pré-inscription…).
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentSuperAdmin, get_current_superadmin
from app.models.audit_log import AuditLog
from app.models.tenant import Tenant
from app.services import bob_pause
from app.services.business_type import BUSINESS_TYPES, normalize

router = APIRouter(prefix="/api/v1/superadmin", tags=["superadmin"])

ACTIVITY_LIMIT = 8
HISTORY_LIMIT = 30
NEW_PREREG_DAYS = 3
FUNNEL_DAYS = 30

# Ce que montrent l'activité récente et l'historique d'une fiche (les autres actions du journal restent internes).
ACTION_LABELS = {
    "TENANT_REGISTERED": "Compte créé (inscription)",
    "DEMO_PROMOTED_TO_ACCOUNT": "Compte créé depuis la démo",
    "PREREG_ACCESS_OPENED": "Accès complet ouvert",
    "PREREG_ACCESS_CLOSED": "Remis en pré-inscription",
    "PLAN_CHANGED_BY_ADMIN": "Plan changé",
    "PAID_UNTIL_CHANGED_BY_ADMIN": "Échéance changée (payé jusqu'au)",
    "COMMISSION_CHANGED_BY_ADMIN": "Commission changée",
    "TENANT_SUSPENDED_BY_ADMIN": "Boutique suspendue",
    "TENANT_REACTIVATED_BY_ADMIN": "Boutique réactivée",
    "BUSINESS_TYPE_CHANGED_BY_ADMIN": "Secteur changé",
    "PREREG_FILE_UPLOADED": "Fichier déposé pour l'installation",
    "WA_TEMPLATES_SYNCED": "Modèles WhatsApp créés / vérifiés chez Meta",
    "WA_TEMPLATES_ALLOWED": "Modèles WhatsApp autorisés",
    "WA_TEMPLATES_BLOCKED": "Modèles WhatsApp désactivés pour la boutique",
    "LAUNCH_SLOTS_CHANGED": "Places de l'offre de lancement changées",
}
PROSPECT_EVENTS = {"CLICK": "a ouvert son lien personnel", "DEMO": "a essayé la démo", "SIGNUP": "a créé son compte",
                   "REPLY": "a répondu à un email"}
PLAN_LABELS = {"FREE": "Gratuit", "INDEPENDANT": "Indépendant", "STARTER": "Starter", "PRO": "Pro", "ENTERPRISE": "Entreprise"}
STAGE_LABELS = {bob_pause.WARNING: "se termine le", bob_pause.GRACE: "est échu (jour de grâce) depuis le",
                bob_pause.PAUSED: "est en pause : non renouvelé depuis le"}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _value(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, str) and len(value) == 10 and value[4] == "-":
        return f"{value[8:10]}/{value[5:7]}/{value[:4]}"
    return str(value)


def describe(log: AuditLog) -> str:
    label = ACTION_LABELS.get(log.action, log.action)
    details = log.details or {}
    if "from" in details or "to" in details:
        before, after = details.get("from"), details.get("to")
        if log.action == "PLAN_CHANGED_BY_ADMIN":
            before, after = PLAN_LABELS.get(before, before), PLAN_LABELS.get(after, after)
        if log.action == "BUSINESS_TYPE_CHANGED_BY_ADMIN":
            before = BUSINESS_TYPES.get(before or "", {}).get("label", before)
            after = BUSINESS_TYPES.get(after or "", {}).get("label", after)
        suffix = " %" if log.action == "COMMISSION_CHANGED_BY_ADMIN" else ""
        return f"{label} : {_value(before)}{suffix if before is not None else ''} → {_value(after)}{suffix if after is not None else ''}"
    if log.action == "PREREG_FILE_UPLOADED":
        return f"{label} ({'clients' if details.get('kind') == 'CUSTOMERS' else 'produits / liste'})"
    return label


async def _admin_names(db) -> dict:
    from app.models.superadmin_user import SuperAdminUser

    return {f"superadmin:{a.id}": a.full_name or a.email for a in (await db.execute(select(SuperAdminUser))).scalars()}


async def _me(db, admin: CurrentSuperAdmin):
    from app.models.superadmin_user import SuperAdminUser

    me = await db.get(SuperAdminUser, admin.superadmin_user_id)
    if me is None or not me.active:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return me


@router.get("/overview")
async def overview(admin: CurrentSuperAdmin = Depends(get_current_superadmin), db: AsyncSession = Depends(get_db)) -> dict:
    from app.core.config import get_settings
    from app.models.preregistration import PreregistrationFile
    from app.models.prospect import Prospect, ProspectCampaign, ProspectEvent
    from app.services import preregistration, prospect_mailer
    from app.services import prospection as pros
    from app.services.llm_usage_service import costs_by_tenant, month_bounds

    me = await _me(db, admin)
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    tenants = (await db.execute(select(Tenant))).scalars().all()
    real = [t for t in tenants if not t.is_demo]
    active = [t for t in real if t.active and not preregistration.is_locked(t)]
    locked = [t for t in real if preregistration.is_locked(t)]
    with_files = set()
    if locked:
        with_files = set((await db.execute(select(PreregistrationFile.tenant_id).where(
            PreregistrationFile.tenant_id.in_([t.id for t in locked])))).scalars().all())

    # Prospects : à relancer, réponses à lire, parcours sur 30 jours
    prospects = (await db.execute(select(Prospect))).scalars().all()
    facts = await pros.tenant_facts(db, {p.tenant_id for p in prospects}) if prospects else {}
    stages = {p.id: pros.stage_of(p, facts) for p in prospects}
    today = now.date()
    replied = [p for p in prospects if p.status == "ACTIVE" and p.replied_at is not None
               and (p.last_contact_at is None or _utc(p.last_contact_at) <= _utc(p.replied_at))]
    follow = [p for p in prospects if pros.to_follow_up(p, stages[p.id], today, now) and p not in replied]
    recent = [p for p in prospects if p.created_at is not None and _utc(p.created_at) >= now - timedelta(days=FUNNEL_DAYS)]

    # Coût IA du mois
    start, end, label = month_bounds(None)
    costs = await costs_by_tenant(db, start, end)
    threshold = get_settings().llm_monthly_alert_usd
    cost = round(sum(r["cost_usd"] for r in costs), 2)

    # Abonnements : rappel, jour de grâce, pause
    due = []
    for t in real:
        stage = bob_pause.billing_stage(t, now)
        if stage in STAGE_LABELS and t.paid_until:
            due.append((t, stage))
    due.sort(key=lambda item: item[0].paid_until)

    todo = []
    if replied:
        names = ", ".join(p.company for p in sorted(replied, key=lambda p: _utc(p.replied_at), reverse=True)[:3])
        todo.append({"kind": "REPLIES", "tone": "info", "title": f"{len(replied)} prospect{'s ont' if len(replied) > 1 else ' a'} répondu à vos emails",
                     "detail": names + (" …" if len(replied) > 3 else "") + " · la séquence est arrêtée pour eux",
                     "action": "Lire les réponses", "section": "prospects", "view": "replied", "primary": True})
    for t in sorted(locked, key=lambda t: _utc(t.prereg_at), reverse=True):
        if _utc(t.prereg_at) < now - timedelta(days=NEW_PREREG_DAYS):
            continue
        sector = BUSINESS_TYPES[normalize(t.business_type)]["label"]
        todo.append({"kind": "PREREG", "tone": "warning", "title": f"Nouvelle pré-inscription : {t.name}",
                     "detail": sector + (" · a déposé des fichiers pour l'installation" if t.id in with_files else ""),
                     "action": "Voir", "section": "prereg"})
    for t, stage in due:
        todo.append({"kind": "BILLING", "tone": "danger" if stage != bob_pause.WARNING else "warning",
                     "title": f"L'abonnement de {t.name} {STAGE_LABELS[stage]} {t.paid_until.strftime('%d/%m/%Y')}",
                     "detail": BUSINESS_TYPES[normalize(t.business_type)]["label"], "action": "Ouvrir la fiche",
                     "section": "shops", "tenant_id": str(t.id)})
    if follow:
        todo.append({"kind": "FOLLOW_UP", "tone": "warning", "title": f"{len(follow)} prospect{'s' if len(follow) > 1 else ''} à relancer",
                     "detail": "Date de relance atteinte, ou contacté sans réponse depuis plusieurs jours",
                     "action": "Voir la liste", "section": "prospects", "view": "todo"})
    for r in costs:
        if threshold and r["cost_usd"] > threshold:
            todo.append({"kind": "AI_COST", "tone": "danger", "title": f"Coût IA de {r['tenant']} au-dessus du seuil",
                         "detail": f"{r['cost_usd']:.2f} $ ce mois-ci (seuil {threshold} $)".replace(".", ","),
                         "action": "Voir les coûts", "section": "aicosts"})
    # Lot 67 — erreurs techniques nouvelles ou revenues (monitoring)
    from sqlalchemy import func

    from app.models.monitoring import MonitorIssue
    from app.services import monitoring

    unread = (await db.execute(select(func.count(MonitorIssue.id)).where(MonitorIssue.status.in_(monitoring.UNREAD_STATUSES)))).scalar_one()
    if unread:
        worst = (await db.execute(select(MonitorIssue).where(MonitorIssue.status.in_(monitoring.UNREAD_STATUSES))
                                  .order_by(MonitorIssue.count.desc()).limit(1))).scalar_one()
        todo.insert(0, {"kind": "MONITORING", "tone": "danger",
                        "title": f"{unread} erreur{'s' if unread > 1 else ''} technique{'s' if unread > 1 else ''} à traiter",
                        "detail": f"La plus fréquente : {worst.title[:90]} ({worst.count} fois)",
                        "action": "Voir le monitoring", "section": "monitoring"})
    # Lot 69 — boutiques à 80 % ou plus du plafond de leur formule ce mois-ci
    from app.services import plan_quota

    for t, u in await plan_quota.tenants_over(db):
        more = f" · formule conseillée : {u['suggested_label']}" if u["suggested_label"] else ""
        todo.append({"kind": "QUOTA", "tone": "danger" if u["level"] >= 100 else "warning",
                     "title": f"{t.name} a utilisé {u['pct']} % de sa formule {u['plan_label']}",
                     "detail": f"{u['used']} conversations sur {u['limit']} ce mois-ci" + more
                               + (" · Bob en pause" if u["paused"] else " · Bob continue de répondre"),
                     "action": "Ouvrir la fiche", "section": "shops", "tenant_id": str(t.id)})
    running = (await db.execute(select(ProspectCampaign).where(ProspectCampaign.status == "RUNNING"))).scalars().all()
    if running:
        row = await prospect_mailer.get_settings_row(db)
        sent = await prospect_mailer.sent_last_24h(db, now)
        names = ", ".join(c.name for c in running[:2])
        todo.append({"kind": "CAMPAIGN", "tone": "accent", "title": f"Emails de prospection : {sent} envoyés sur {row.daily_limit} (24 h)",
                     "detail": f"En cours : {names}" + ("" if row.sending_enabled else " · envoi suspendu"),
                     "action": "Voir", "section": "prospects"})

    # Activité récente : journal (inscriptions, accès, réglages) et étapes des prospects
    names = {t.id: t.name for t in tenants}
    admins = await _admin_names(db)
    events = []
    for log in (await db.execute(select(AuditLog).where(AuditLog.action.in_(list(ACTION_LABELS)), AuditLog.action != "PREREG_FILE_UPLOADED")
                                 .order_by(AuditLog.created_at.desc()).limit(ACTIVITY_LIMIT))).scalars():
        events.append({"at": _utc(log.created_at), "tone": "success" if "OPENED" in log.action or "REGISTERED" in log.action or "PROMOTED" in log.action else "info",
                       "who": names.get(log.tenant_id, "Plateforme"), "what": describe(log), "by": admins.get(log.actor)})
    company = {p.id: p.company for p in prospects}
    for ev in (await db.execute(select(ProspectEvent).where(ProspectEvent.kind.in_(list(PROSPECT_EVENTS)))
                                .order_by(ProspectEvent.at.desc()).limit(ACTIVITY_LIMIT))).scalars():
        events.append({"at": _utc(ev.at), "tone": "warning", "who": company.get(ev.prospect_id, "Prospect"),
                       "what": PROSPECT_EVENTS[ev.kind], "by": None})
    events.sort(key=lambda e: e["at"] or now, reverse=True)

    return {
        "first_name": (me.full_name or "").split()[0] if (me.full_name or "").strip() else None,
        "me_name": me.full_name or me.email,
        "prereg_mode": await preregistration.mode_on(db),
        "kpis": {
            "active": len(active), "active_new": sum(1 for t in active if t.created_at and _utc(t.created_at) >= month_start),
            "prereg": len(locked), "prereg_with_files": len(with_files),
            "follow_up": len(follow), "replies": len(replied),
            "ai_cost": cost, "ai_threshold": threshold, "ai_month": label, "monitoring_new": unread,
        },
        "todo": todo,
        "funnel": pros.funnel(recent, stages) if recent else pros.funnel([], {}),
        "activity": [{**e, "at": e["at"].isoformat() if e["at"] else None} for e in events[:ACTIVITY_LIMIT]],
    }


@router.get("/tenants/{tenant_id}/history")
async def tenant_history(tenant_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Boutique introuvable")
    admins = await _admin_names(db)
    logs = (await db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant.id, AuditLog.action.in_(list(ACTION_LABELS)))
                             .order_by(AuditLog.created_at.desc()).limit(HISTORY_LIMIT))).scalars().all()
    return {"items": [{"at": _utc(log.created_at).isoformat() if log.created_at else None, "what": describe(log),
                       "by": admins.get(log.actor)} for log in logs]}
