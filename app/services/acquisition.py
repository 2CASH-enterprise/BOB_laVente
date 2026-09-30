"""
Lot 28 — d'où viennent les clients.

Deux façons de connaître la source d'un client :
1. Publicité ou publication Facebook / Instagram « clic vers WhatsApp » : Meta joint au premier
   message une référence (`referral`). Bob la lit, sans aucune action du commerçant.
2. Tout le reste (Google, YouTube, TikTok, site, affiche, commercial…) : le client passe par un
   lien Bob `/w/…`, et le commerçant indique le CANAL de ce lien. Le canal d'un client arrivé par
   un lien est lu sur le lien au moment de l'affichage : corriger le canal d'un lien corrige aussi
   ses anciens clients.

Un client qui tape lui-même le numéro reste « Direct » : aucun outil ne peut deviner mieux.
La source d'acquisition reste le TOUT PREMIER contact (jamais réécrite).
"""
from datetime import datetime

# Canaux qu'un commerçant peut choisir pour un lien (ordre d'affichage).
LINK_CHANNELS = {
    "GOOGLE": "Google",
    "YOUTUBE": "YouTube",
    "TIKTOK": "TikTok",
    "FACEBOOK": "Facebook",
    "INSTAGRAM": "Instagram",
    "WEBSITE": "Site web",
    "PRINT": "Affiche, flyer, salon",
    "COMMERCIAL": "Commercial",
    "OTHER": "Autre",
}

# Sources notées automatiquement (jamais choisies pour un lien).
AD_FACEBOOK = "AD_FACEBOOK"
AD_INSTAGRAM = "AD_INSTAGRAM"
AD_META = "AD_META"  # Meta n'a pas précisé Facebook ou Instagram
NAOMY = "NAOMY"  # réservé : passerelle NaomyIA, quand elle sera connectée
AUTO_CHANNELS = {
    AD_FACEBOOK: "Pub Facebook",
    AD_INSTAGRAM: "Pub Instagram",
    AD_META: "Pub Facebook / Instagram",
    "QR": "QR code produit",
    NAOMY: "NaomyIA",
    "IMPORT": "Import",
    "DIRECT": "Direct",
}

CHANNEL_LABELS = {**LINK_CHANNELS, **AUTO_CHANNELS}

_MAX_DETAIL = 120
_MAX_AD_TEXT = 300


def normalize_link_channel(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    code = str(value).strip().upper()
    if code not in LINK_CHANNELS:
        raise ValueError(f"Canal inconnu : {value}")
    return code


def channel_of(acquisition_source: str | None, link_channel: str | None = None) -> str:
    """Canal d'un client, à partir de sa source d'acquisition (et du canal de son lien)."""
    if acquisition_source == "LINK":
        return link_channel if link_channel in LINK_CHANNELS else "OTHER"
    if acquisition_source in AUTO_CHANNELS:
        return acquisition_source
    if acquisition_source in ("FACEBOOK", "INSTAGRAM"):  # publication (pas une pub) Meta
        return acquisition_source
    return "DIRECT"  # None, « ORGANIC » ou source inconnue


def channel_label(code: str) -> str:
    return CHANNEL_LABELS.get(code, code)


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())  # aucun retour à la ligne venu de l'extérieur
    return text[:limit] or None


def parse_referral(raw) -> dict | None:
    """
    Référence Meta d'un message « clic vers WhatsApp ». Les valeurs viennent de l'extérieur :
    longueurs bornées, seuls les champs utiles sont gardés (jamais d'URL de média ni d'identifiant
    de clic).
    """
    if not isinstance(raw, dict):
        return None
    source_url = raw.get("source_url") if isinstance(raw.get("source_url"), str) else ""
    ad_id = raw.get("source_id")
    ref = {
        "is_ad": raw.get("source_type") == "ad",
        "network": _network(source_url),
        "ad_id": _clean(str(ad_id), 64) if isinstance(ad_id, (str, int)) else None,
        "headline": _clean(raw.get("headline"), _MAX_DETAIL),
        "body": _clean(raw.get("body"), _MAX_AD_TEXT),
    }
    if not (ref["ad_id"] or ref["headline"] or ref["body"] or source_url):
        return None
    return ref


def _network(source_url: str) -> str | None:
    host = source_url.lower()
    if "instagram.com" in host:
        return "INSTAGRAM"
    if "facebook.com" in host or "fb.me" in host or "fb.com" in host:
        return "FACEBOOK"
    return None


def attribution_from_referral(ref: dict) -> tuple[str, str]:
    """(source d'acquisition, détail lisible) pour un nouveau client arrivé par Meta."""
    if ref["is_ad"]:
        source = {"INSTAGRAM": AD_INSTAGRAM, "FACEBOOK": AD_FACEBOOK}.get(ref["network"], AD_META)
        detail = ref["headline"] or (f"Publicité {ref['ad_id']}" if ref["ad_id"] else "Publicité")
    else:
        source = ref["network"] or "FACEBOOK"
        detail = ref["headline"] or "Publication"
    return source, detail


def ad_context_for_ai(ref: dict | None) -> str | None:
    """
    Contexte transmis à Bob pour CE message : de quelle annonce parle le client. Présenté comme
    une donnée (texte écrit par la boutique), jamais comme une consigne.
    """
    if not ref or not (ref["headline"] or ref["body"]):
        return None
    kind = "une publicité" if ref["is_ad"] else "une publication"
    parts = [f"titre : « {ref['headline']} »" if ref["headline"] else "", f"texte : « {ref['body']} »" if ref["body"] else ""]
    return (
        f"[Information : le client écrit après avoir cliqué sur {kind} de la boutique ("
        + " ; ".join(p for p in parts if p)
        + "). Utilise-la pour comprendre de quoi il parle ; vérifie toujours les prix et la "
        "disponibilité avec tes outils, l'annonce peut ne plus être à jour.]"
    )


async def sources_summary(db, tenant_id, since: datetime | None) -> dict:
    """Clients, rendez-vous et ventes payées par canal, puis par lien ou publicité."""
    from sqlalchemy import func, select

    from app.models.appointment_request import AppointmentRequest
    from app.models.contact_point import ContactPoint
    from app.models.customer import Customer
    from app.models.order import Order, OrderStatus

    stmt = select(Customer).where(Customer.tenant_id == tenant_id)
    if since is not None:
        stmt = stmt.where(Customer.created_at >= since)
    customers = (await db.execute(stmt)).scalars().all()
    links = {cp.id: cp for cp in (await db.execute(
        select(ContactPoint).where(ContactPoint.tenant_id == tenant_id)
    )).scalars().all()}
    ids = [c.id for c in customers]
    appointments: dict = {}
    visits: dict = {}  # lot 36 : rendez-vous honorés (venus) et vendus, par client
    sold: dict = {}
    sales: dict = {}
    if ids:
        from app.models.appointment_request import OUTCOME_SOLD, VISITED_OUTCOMES

        for customer_id, outcome in (await db.execute(
            select(AppointmentRequest.customer_id, AppointmentRequest.outcome)
            .where(AppointmentRequest.tenant_id == tenant_id, AppointmentRequest.customer_id.in_(ids))
        )).all():
            appointments[customer_id] = appointments.get(customer_id, 0) + 1
            if outcome in VISITED_OUTCOMES:
                visits[customer_id] = visits.get(customer_id, 0) + 1
            if outcome == OUTCOME_SOLD:
                sold[customer_id] = sold.get(customer_id, 0) + 1
        sales = {row[0]: (row[1], float(row[2] or 0)) for row in (await db.execute(
            select(Order.customer_id, func.count(Order.id), func.sum(Order.total_amount))
            .where(Order.tenant_id == tenant_id, Order.customer_id.in_(ids), Order.status == OrderStatus.PAID)
            .group_by(Order.customer_id)
        )).all()}

    channels: dict = {}
    details: dict = {}
    for c in customers:
        link = links.get(c.acquisition_contact_point_id) if c.acquisition_source == "LINK" else None
        code = channel_of(c.acquisition_source, link.channel if link else None)
        if link is not None:
            detail = f"Lien « {link.name} »"
        elif c.acquisition_source in (AD_FACEBOOK, AD_INSTAGRAM, AD_META, "FACEBOOK", "INSTAGRAM", "QR"):
            detail = c.acquisition_detail or channel_label(code)
        else:
            detail = None
        paid_count, paid_amount = sales.get(c.id, (0, 0.0))
        for bucket, key, label in ((channels, code, channel_label(code)), (details, (code, detail), detail)):
            if label is None:
                continue
            row = bucket.setdefault(key, {"channel": code, "channel_label": channel_label(code), "label": label,
                                          "customers": 0, "appointments": 0, "visits": 0, "vehicles_sold": 0,
                                          "paid_orders": 0, "revenue": 0.0})
            row["customers"] += 1
            row["appointments"] += appointments.get(c.id, 0)
            row["visits"] += visits.get(c.id, 0)
            row["vehicles_sold"] += sold.get(c.id, 0)
            row["paid_orders"] += paid_count
            row["revenue"] += paid_amount

    order = lambda r: (-r["customers"], r["label"])  # noqa: E731
    return {
        "total_customers": len(customers),
        "channels": sorted(channels.values(), key=order),
        "details": sorted(details.values(), key=order)[:30],
    }


async def commercials_summary(db, tenant_id, since: datetime | None) -> list[dict]:
    """
    Lot 36 — par commercial (lien avec un email) : prospects rattachés, rendez-vous, venus, vendus.
    Prospects : clients rattachés à son lien (créés sur la période) ; rendez-vous : pris sur la période
    par ses prospects.
    """
    from sqlalchemy import select

    from app.models.appointment_request import OUTCOME_SOLD, VISITED_OUTCOMES, AppointmentRequest
    from app.models.contact_point import ContactPoint
    from app.models.customer import Customer

    links = (await db.execute(select(ContactPoint).where(
        ContactPoint.tenant_id == tenant_id, ContactPoint.owner_email.is_not(None),
    ))).scalars().all()
    rows = []
    for link in links:
        customers = (await db.execute(select(Customer).where(
            Customer.tenant_id == tenant_id, Customer.referred_contact_point_id == link.id,
        ))).scalars().all()
        ids = [c.id for c in customers]
        prospects = sum(1 for c in customers if since is None or (c.created_at and _aware(c.created_at) >= since))
        appointments = []
        if ids:
            stmt = select(AppointmentRequest).where(AppointmentRequest.tenant_id == tenant_id,
                                                    AppointmentRequest.customer_id.in_(ids))
            if since is not None:
                stmt = stmt.where(AppointmentRequest.created_at >= since)
            appointments = (await db.execute(stmt)).scalars().all()
        visits = sum(1 for a in appointments if a.outcome in VISITED_OUTCOMES)
        sold = sum(1 for a in appointments if a.outcome == OUTCOME_SOLD)
        rows.append({
            "name": link.owner_name or link.owner_email, "link": link.name, "active": link.active and link.archived_at is None,
            "prospects": prospects, "appointments": len(appointments), "visits": visits, "vehicles_sold": sold,
            "conversion_pct": round(sold * 100 / visits) if visits else None,
        })
    return sorted(rows, key=lambda r: (-r["vehicles_sold"], -r["appointments"], r["name"]))


def _aware(value: datetime) -> datetime:
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
