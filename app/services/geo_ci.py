"""
Lot 113 — la commune d'une position partagée sur WhatsApp, sans service de cartes (aucun coût, rien ne sort).

Centres approximatifs des communes du Grand Abidjan : la commune la plus proche, si la position est à moins de
MAX_KM de son centre. Près d'une limite la réponse peut être la voisine : Bob demande toujours de confirmer
(« Vous êtes à Cocody ? ») et le client peut choisir une autre commune. Hors d'Abidjan : rien n'est deviné.
"""
import math
import unicodedata

ABIDJAN = [
    ("Plateau", 5.3240, -4.0167), ("Cocody", 5.3550, -3.9850), ("Yopougon", 5.3450, -4.0750),
    ("Abobo", 5.4250, -4.0200), ("Adjamé", 5.3550, -4.0250), ("Attécoubé", 5.3350, -4.0400),
    ("Treichville", 5.2950, -4.0080), ("Marcory", 5.3030, -3.9850), ("Koumassi", 5.2950, -3.9500),
    ("Port-Bouët", 5.2550, -3.9300), ("Bingerville", 5.3550, -3.8900), ("Anyama", 5.4950, -4.0500),
    ("Songon", 5.3150, -4.2550),
]
MAX_KM = 6.0


def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    rad = math.radians
    a = math.sin(rad(lat2 - lat1) / 2) ** 2 + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(rad(lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def commune(lat: float, lng: float) -> str | None:
    """« Cocody », ou None si la position n'est près d'aucune commune connue."""
    name, lat0, lng0 = min(ABIDJAN, key=lambda c: km(lat, lng, c[1], c[2]))
    return name if km(lat, lng, lat0, lng0) <= MAX_KM else None


def _key(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in plain if ch.isalnum())


def area_index(zones: list, name: str | None) -> int | None:
    """La commune de livraison du restaurant qui correspond (« Port Bouet », « Cocody Angré »…), sinon None."""
    if not name:
        return None
    wanted = _key(name)
    for i, zone in enumerate(zones or []):
        key = _key(zone.get("name", ""))
        if key == wanted:
            return i
    for i, zone in enumerate(zones or []):
        key = _key(zone.get("name", ""))
        if key and (key.startswith(wanted) or wanted.startswith(key)):
            return i
    return None


def valid(lat, lng) -> tuple[float, float] | None:
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return round(lat, 6), round(lng, 6)


def map_link(location: dict | None) -> str | None:
    """Lien Google Maps du point (ouvre l'application de cartes du téléphone du livreur)."""
    if not location or location.get("lat") is None:
        return None
    return f"https://www.google.com/maps?q={location['lat']},{location['lng']}"


def label(location: dict) -> str:
    """Ce qu'on écrit à la place de l'adresse : l'adresse donnée par WhatsApp, sinon « Position partagée »."""
    text = " — ".join(part for part in (location.get("name"), location.get("address")) if part)
    return (text or "Position partagée")[:300]


KEEP_DAYS = 30


async def purge_positions(db, now=None) -> int:
    """Lot 113 — le point exact d'une commande est effacé 30 jours après la livraison (commune et adresse restent).
    Une commande jamais livrée (annulée, oubliée) perd aussi son point 30 jours après sa création + 30 jours."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models.delivery import Delivery, DeliveryStatus
    from app.models.order import Order

    now = now or datetime.now(timezone.utc)
    limit = now - timedelta(days=KEEP_DAYS)
    rows = (await db.execute(select(Order, Delivery).outerjoin(Delivery, Delivery.order_id == Order.id)
                             .where(Order.delivery_location.is_not(None)))).all()
    cleared = 0
    for order, delivery in rows:
        done_at = delivery.updated_at if delivery is not None and delivery.status == DeliveryStatus.DELIVERED else None
        created = order.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if done_at is not None and done_at.tzinfo is None:
            done_at = done_at.replace(tzinfo=timezone.utc)
        if (done_at is not None and done_at < limit) or (created is not None and created < limit - timedelta(days=KEEP_DAYS)):
            order.delivery_location = None
            cleared += 1
    await db.commit()
    return cleared
