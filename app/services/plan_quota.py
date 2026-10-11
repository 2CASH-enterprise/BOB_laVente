"""
Lot 69 — plafonds de conversations des formules payantes (tarifs du 11/10/2026).

Choix 5A : Bob ne s'arrête JAMAIS en plein mois pour un client qui paie (mode « WARN », par défaut). On prévient la
boutique à 80 % puis à 100 % de sa formule (email + bandeau sur son accueil) et le Super Admin (« À faire »), avec la
formule conseillée. Le Super Admin peut changer d'avis plus tard : mode « PAUSE » = au-delà du plafond, Bob répond le
message d'attente jusqu'au mois suivant, comme pour le plan gratuit. Les plafonds se changent dans le Super Admin.
Le plan gratuit garde sa règle (plan_limits : 100 conversations).
"""
from datetime import datetime, timezone

from sqlalchemy import select

ORDER = ["INDEPENDANT", "STARTER", "PRO", "ENTERPRISE"]
LABELS = {"FREE": "Gratuit", "INDEPENDANT": "Indépendant", "STARTER": "Starter", "PRO": "Pro", "ENTERPRISE": "Entreprise"}
DEFAULT_QUOTAS = {"INDEPENDANT": 350, "STARTER": 1500, "PRO": 5000, "ENTERPRISE": None}  # None = sans limite
PRICES = {"INDEPENDANT": (15000, 23), "STARTER": (49500, 75), "PRO": (165000, 252), "ENTERPRISE": None}  # HT/mois : FCFA, €
LEVELS = (80, 100)
MODES = ("WARN", "PAUSE")


def price_label(plan: str) -> str:
    price = PRICES.get(plan)
    if not price:
        return "sur devis"
    # Espaces insécables : un prix ne se coupe jamais en fin de ligne (« 49 500 FCFA »).
    return f"{price[0]:,}\u00a0FCFA ({price[1]}\u00a0€) HT par mois".replace(",", "\u00a0")


async def settings(db) -> dict:
    from app.models.preregistration import PlatformSettings

    row = await db.get(PlatformSettings, 1)
    quotas = dict(DEFAULT_QUOTAS)
    for plan, value in ((row.plan_quotas if row else None) or {}).items():
        if plan in quotas:
            quotas[plan] = int(value) if value not in (None, "") else None
    return {"quotas": quotas, "mode": (row.quota_mode if row and row.quota_mode in MODES else "WARN")}


def suggested(plan: str, used: int, quotas: dict) -> str | None:
    """La première formule au-dessus dont le plafond couvre l'usage (Entreprise au bout)."""
    if plan not in ORDER:
        return None
    for nxt in ORDER[ORDER.index(plan) + 1:]:
        if quotas.get(nxt) is None or quotas[nxt] >= used:
            return nxt
    return None


def level_of(used: int, limit: int | None) -> int:
    if not limit:
        return 0
    pct = used * 100 / limit
    return 100 if pct >= 100 else 80 if pct >= 80 else 0


async def usage(db, tenant, conf: dict | None = None) -> dict | None:
    """Conversations du mois / plafond de la formule ; None pour le plan gratuit, une démo ou une formule sans plafond."""
    from app.services.plan_limits import count_conversations_this_month

    plan = getattr(tenant.plan, "value", tenant.plan)
    if tenant.is_demo or plan not in ORDER:
        return None
    conf = conf or await settings(db)
    limit = conf["quotas"].get(plan)
    used = await count_conversations_this_month(db, tenant.id)
    nxt = suggested(plan, used, conf["quotas"]) if limit else None
    return {"plan": plan, "plan_label": LABELS[plan], "limit": limit, "used": used,
            "pct": round(used * 100 / limit) if limit else None, "level": level_of(used, limit),
            "mode": conf["mode"], "paused": bool(limit) and conf["mode"] == "PAUSE" and used > limit,
            "suggested": nxt, "suggested_label": LABELS.get(nxt) if nxt else None,
            "suggested_limit": conf["quotas"].get(nxt) if nxt else None, "suggested_price": price_label(nxt) if nxt else None}


async def paused(db, tenant) -> bool:
    """Mode « PAUSE » seulement : au-delà du plafond de sa formule, Bob répond le message d'attente (webhook)."""
    u = await usage(db, tenant)
    return bool(u and u["paused"])


def alert_email(tenant, u: dict) -> tuple[str, str]:
    over = u["level"] >= 100
    head = (f"Bob a répondu à {u['used']} conversations ce mois-ci : le plafond de votre formule {u['plan_label']} "
            f"({u['limit']} conversations) est atteint." if over else
            f"Bob a déjà répondu à {u['used']} conversations ce mois-ci, sur les {u['limit']} de votre formule {u['plan_label']}.")
    if u["paused"]:
        keep = "Bob est en pause jusqu'au 1er du mois prochain : vos clients reçoivent un message d'attente."
    else:
        keep = "Bob continue de répondre à tous vos clients : rien ne s'arrête."
    advice = ""
    if u["suggested"]:
        cap = (f"jusqu'à {u['suggested_limit']:,} conversations".replace(",", "\u00a0") if u["suggested_limit"]
               else "sans limite de conversations")
        advice = (f"\n\nSi ce rythme se confirme, la formule {u['suggested_label']} ({cap}, {u['suggested_price']}) "
                  "vous conviendra mieux. Répondez simplement à cet email pour en parler.")
    subject = (f"Bob : plafond de votre formule {u['plan_label']} atteint" if over
               else f"Bob : {u['pct']} % des conversations de votre formule utilisées")
    body = f"Bonjour,\n\n{head}\n\n{keep}{advice}\n\nÀ bientôt,\nL'équipe de Bob"
    return subject, body


async def check_all(db, now: datetime | None = None, send=None) -> dict:
    """Toutes les heures : un email par seuil (80 %, puis 100 %) et par mois, jamais deux fois."""
    from app.models.tenant import Tenant
    from app.services.preregistration import is_locked

    if send is None:
        from app.services.email_service import send_email as send
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    conf = await settings(db)
    report = {"checked": 0, "alerts": 0}
    tenants = (await db.execute(select(Tenant).where(Tenant.active.is_(True), Tenant.is_demo.is_(False)))).scalars().all()
    for tenant in tenants:
        if is_locked(tenant):
            continue
        u = await usage(db, tenant, conf)
        if u is None or not u["limit"]:
            continue
        report["checked"] += 1
        already = tenant.quota_alert_level if tenant.quota_alert_month == month else 0
        if u["level"] <= already:
            continue
        subject, body = alert_email(tenant, u)
        if tenant.email and send(to=tenant.email, subject=subject, body=body):
            tenant.quota_alert_month, tenant.quota_alert_level = month, u["level"]
            report["alerts"] += 1
            await db.commit()
    return report


async def tenants_over(db, threshold: int = 80) -> list[tuple]:
    """(boutique, usage) des boutiques à 80 % ou plus de leur formule ce mois-ci, la plus haute d'abord (vue d'ensemble)."""
    from app.models.tenant import Tenant

    conf = await settings(db)
    out = []
    for tenant in (await db.execute(select(Tenant).where(Tenant.active.is_(True), Tenant.is_demo.is_(False)))).scalars():
        u = await usage(db, tenant, conf)
        if u and u["limit"] and u["pct"] is not None and u["pct"] >= threshold:
            out.append((tenant, u))
    return sorted(out, key=lambda item: item[1]["pct"], reverse=True)
