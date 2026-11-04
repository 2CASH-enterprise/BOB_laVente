"""
Lot 106 — secteur Restaurant (validé le 04/11, Côte d'Ivoire d'abord).

Bob prend les commandes comme pour une boutique — à emporter, en livraison (frais par commune, renseignés dans les
réglages) ou sur place depuis la table — et réserve les tables : la capacité est un nombre de couverts par service
(chaque plage des horaires de réservation est un service, ex. midi et soir ; services/booking.py). Paiement à la
livraison, au restaurant ou par le lien de paiement de la boutique. Les horaires de la cuisine bornent les commandes :
cuisine fermée = pas de commande, Bob donne l'heure d'ouverture suivante. Un plat « indisponible aujourd'hui » n'est
plus proposé jusqu'au lendemain (remis automatiquement). Composition et allergènes : seulement ce que le restaurant a
renseigné, jamais une supposition. Aucune IA en plus.
"""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

MAX_AREAS = 40
MODES = {"EMPORTER": "PICKUP", "LIVRAISON": "DELIVERY", "SUR_PLACE": "DINE_IN"}  # mot de l'IA → Order.fulfillment
MODE_LABELS = {"PICKUP": "à emporter", "DELIVERY": "en livraison", "DINE_IN": "sur place (depuis la table)"}
DAY_NAMES = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class RestaurantError(ValueError):
    pass


def _plain(text) -> str:
    from app.services.product_sheets import _plain as plain

    return plain(text).strip()


def clean_delivery_areas(raw) -> list[dict]:
    """[{"name": "Cocody", "fee": 1000}, …] vérifié : noms uniques (64 car.), frais ≥ 0, 40 communes au plus."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise RestaurantError("Communes de livraison invalides.")
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise RestaurantError("Communes de livraison invalides.")
        name = " ".join(str(item.get("name") or "").split())[:64]
        if not name:
            continue
        try:
            fee = Decimal(str(item.get("fee") if item.get("fee") not in (None, "") else "0").replace(" ", "").replace(",", "."))
        except InvalidOperation:
            raise RestaurantError(f"Frais de livraison invalides pour {name}.") from None
        if fee < 0:
            raise RestaurantError(f"Frais de livraison invalides pour {name}.")
        if _plain(name) in seen:
            raise RestaurantError(f"{name} est en double.")
        seen.add(_plain(name))
        out.append({"name": name, "fee": float(fee)})
    if len(out) > MAX_AREAS:
        raise RestaurantError(f"{MAX_AREAS} communes au plus.")
    return out


def area_of(tenant, name: str | None) -> dict | None:
    """La commune du restaurant qui correspond à ce que le client a dit (« cocody » → Cocody)."""
    wanted = _plain(name)
    if not wanted:
        return None
    areas = getattr(tenant, "delivery_areas", None) or []
    return next((a for a in areas if _plain(a["name"]) == wanted), None) or next(
        (a for a in areas if _plain(a["name"]) in wanted or wanted in _plain(a["name"])), None)


def modes(tenant) -> list[str]:
    """Modes acceptés : à emporter (retrait), livraison, sur place."""
    out = []
    if getattr(tenant, "pickup_enabled", False):
        out.append("PICKUP")
    if getattr(tenant, "delivery_enabled", True):
        out.append("DELIVERY")
    if getattr(tenant, "dine_in_enabled", False):
        out.append("DINE_IN")
    return out


def _ranges(hours: dict | None, day: int) -> list[tuple[int, int]]:
    from app.services.booking import _minutes

    return [(_minutes(a), _minutes(b)) for a, b in (hours or {}).get(str(day)) or []]


def kitchen_open(tenant, now: datetime | None = None) -> bool:
    """Sans horaires de cuisine renseignés : toujours ouverte (le restaurant n'a rien limité)."""
    hours = getattr(tenant, "kitchen_hours", None)
    if not hours or not any(hours.values()):
        return True
    from app.services.local_time import tenant_zone

    local = (now or datetime.now(timezone.utc)).astimezone(tenant_zone(tenant))
    minute = local.hour * 60 + local.minute
    return any(low <= minute < high for low, high in _ranges(hours, local.weekday()))


def next_opening(tenant, now: datetime | None = None) -> datetime | None:
    """La prochaine ouverture de la cuisine (heure du restaurant), dans les 7 jours."""
    hours = getattr(tenant, "kitchen_hours", None)
    if not hours or not any(hours.values()):
        return None
    from app.services.local_time import tenant_zone

    zone = tenant_zone(tenant)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        for low, _ in sorted(_ranges(hours, day.weekday())):
            start = datetime.combine(day, time(low // 60, low % 60), tzinfo=zone)
            if start > local:
                return start
    return None


def opening_text(moment: datetime, now: datetime | None = None) -> str:
    local_now = (now or datetime.now(timezone.utc)).astimezone(moment.tzinfo)
    hour = f"{moment.hour} h" + (f" {moment.minute:02d}" if moment.minute else "")
    if moment.date() == local_now.date():
        return f"aujourd'hui à {hour}"
    if moment.date() == local_now.date() + timedelta(days=1):
        return f"demain à {hour}"
    return f"{DAY_NAMES[moment.weekday()]} à {hour}"


def hours_text(hours: dict | None) -> str:
    """« lundi : 11:00-15:00, 18:00-23:00 ; … » (jours fermés : « fermé »)."""
    if not hours or not any(hours.values()):
        return "non renseignés (la cuisine prend les commandes à toute heure)"
    parts = []
    for day in range(7):
        ranges = hours.get(str(day)) or []
        parts.append(f"{DAY_NAMES[day]} : " + (", ".join(f"{a}-{b}" for a, b in ranges) if ranges else "fermé"))
    return " ; ".join(parts)


def order_terms(tenant, mode_word: str | None, area: str | None, table: str | None, address: str | None,
                now: datetime | None = None) -> dict:
    """Ce que le code impose à une commande de restaurant : mode accepté, cuisine ouverte, commune et frais (livraison),
    numéro de table (sur place). Renvoie {fulfillment, delivery_fee, delivery_area, table_number} ou lève RestaurantError
    avec la phrase à dire au client."""
    if not kitchen_open(tenant, now):
        when = next_opening(tenant, now)
        raise RestaurantError("La cuisine est fermée en ce moment : aucune commande n'a été prise. "
                              + (f"Elle rouvre {opening_text(when, now)} : propose au client de commander à ce moment-là."
                                 if when else "Propose au client de réessayer plus tard."))
    fulfillment = MODES.get(str(mode_word or "").upper())
    accepted = modes(tenant)
    if fulfillment is None or fulfillment not in accepted:
        raise RestaurantError("Demande d'abord au client s'il veut " + " ou ".join(MODE_LABELS[m] for m in accepted)
                              + " : rien n'a été enregistré.")
    if fulfillment == "DINE_IN":
        table = " ".join(str(table or "").split())[:16]
        if not table:
            raise RestaurantError("Demande le numéro de la table au client (il est écrit sur la table) : rien n'a été enregistré.")
        return {"fulfillment": fulfillment, "delivery_fee": None, "delivery_area": None, "table_number": table}
    if fulfillment == "PICKUP":
        return {"fulfillment": fulfillment, "delivery_fee": None, "delivery_area": None, "table_number": None}
    if not (address or "").strip():
        raise RestaurantError("Demande l'adresse de livraison (quartier, repère) : rien n'a été enregistré.")
    areas = getattr(tenant, "delivery_areas", None) or []
    if not areas:
        fee = getattr(tenant, "delivery_fee", None)
        return {"fulfillment": fulfillment, "delivery_fee": float(fee) if fee is not None else None,
                "delivery_area": (area or "").strip()[:64] or None, "table_number": None}
    found = area_of(tenant, area)
    if found is None:
        names = ", ".join(a["name"] for a in areas)
        raise RestaurantError(f"Le restaurant livre seulement : {names}. Demande au client sa commune parmi celles-ci "
                              "(sinon, propose à emporter) : rien n'a été enregistré.")
    return {"fulfillment": fulfillment, "delivery_fee": found["fee"], "delivery_area": found["name"], "table_number": None}


# --- « Indisponible aujourd'hui » ------------------------------------------------------------------------------------

def set_unavailable_today(product, tenant, unavailable: bool, now: datetime | None = None) -> None:
    from app.services.bob_pause import local_today

    sheet = dict(product.sheet or {})
    if unavailable:
        sheet["unavailable_on"] = local_today(tenant, now).isoformat()
        product.stock_quantity = 0
    else:
        sheet.pop("unavailable_on", None)
        product.stock_quantity = max(1, product.stock_quantity or 0)
    product.sheet = sheet or None


async def restore_dishes(db, now: datetime | None = None) -> int:
    """Chaque heure : un plat marqué indisponible un jour passé (heure du restaurant) redevient disponible."""
    from sqlalchemy import select

    from app.models.product import Product
    from app.models.tenant import Tenant
    from app.services.bob_pause import local_today
    from app.services.business_type import RESTAURANT

    restored = 0
    tenants = (await db.execute(select(Tenant).where(Tenant.business_type == RESTAURANT))).scalars().all()
    for tenant in tenants:
        today = local_today(tenant, now).isoformat()
        for product in (await db.execute(select(Product).where(Product.tenant_id == tenant.id))).scalars():
            day = (product.sheet or {}).get("unavailable_on")
            if day and day < today:
                set_unavailable_today(product, tenant, False, now)
                restored += 1
    await db.commit()
    return restored


# --- Ce que Bob sait du restaurant ----------------------------------------------------------------------------------

def office_info(tenant) -> str:
    """Bloc « LE RESTAURANT » des consignes : modes, paiement, communes et frais, horaires de la cuisine."""
    from app.services.product_cards import money_text

    lines = ["Commande : " + (", ".join(MODE_LABELS[m] for m in modes(tenant)) or "aucun mode activé")]
    currency = getattr(tenant, "currency", None) or "XOF"
    if "DELIVERY" in modes(tenant):
        areas = getattr(tenant, "delivery_areas", None) or []
        if areas:
            lines.append("Livraison (frais par commune) : " + " ; ".join(
                f"{a['name']} {money_text(a['fee'], currency) if a['fee'] else 'offerte'}" for a in areas))
        elif getattr(tenant, "delivery_fee", None) is not None:
            lines.append(f"Livraison : {money_text(tenant.delivery_fee, currency)}"
                         + (f" ({tenant.delivery_zone})" if getattr(tenant, "delivery_zone", None) else ""))
    if "PICKUP" in modes(tenant) and getattr(tenant, "pickup_address", None):
        lines.append(f"Adresse du restaurant : {tenant.pickup_address}")
    payments = ["à la livraison", "au restaurant"] + (["par le lien de paiement"] if getattr(tenant, "payment_link", None) else [])
    lines.append("Paiement : " + ", ".join(payments))
    lines.append("Horaires de la cuisine : " + hours_text(getattr(tenant, "kitchen_hours", None)))
    return "\n".join(lines)


RESTAURANT_RULES = """RÈGLES (restaurant)

1. Tu présentes la carte du restaurant : seulement les plats et boissons trouvés avec search_products, avec leur prix
   exact. N'invente jamais un plat, un ingrédient, un accompagnement ni un prix.
2. Composition, accompagnements, piment, allergènes : dis seulement ce qui est écrit dans la fiche du plat. Si le
   client demande un allergène et que la fiche ne dit rien, réponds que tu vas vérifier avec le restaurant et ne dis
   JAMAIS « il n'y en a pas ». « Aucun des 14 » dans la fiche veut dire que le restaurant a vérifié : aucun des 14
   allergènes majeurs.
3. Une COMMANDE : demande ce que le client veut et en quelle quantité, puis le mode (à emporter, en livraison ou sur
   place), selon les modes du restaurant ci-dessous. Livraison : sa commune (les frais dépendent de la commune) et son
   adresse (quartier, repère). Sur place : le numéro de sa table. Récapitule (plats, quantités, frais de livraison s'il
   y en a, total) et demande une confirmation claire avant create_order. Les frais et l'ouverture de la cuisine sont
   vérifiés par le code : s'il refuse, dis-le simplement au client.
4. Paiement : à la livraison, au restaurant ou par le lien de paiement s'il existe. Ne promets jamais un autre moyen.
5. Une RÉSERVATION de table : demande le nombre de personnes, puis le jour et le moment ; appelle get_available_slots
   avec party_size, propose les créneaux tels quels, puis request_appointment (kind TABLE, party_size, slot). Jamais
   une table confirmée sans l'outil.
6. Jamais de négociation ni de remise : les prix de la carte sont fixes.
7. Pour une demande que tu ne peux pas traiter (grand groupe, événement, réclamation), propose de passer la main au
   restaurant."""
