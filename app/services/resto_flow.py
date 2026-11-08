"""
Lot 107 — parcours à boutons du restaurant (validé le 04/11).

Le client choisit tout en touchant des boutons ; c'est le CODE qui répond, sans appel à l'IA (aucun coût). S'il écrit
librement, l'IA du lot 106 répond.

- Accueil : Commander, Réserver une table, Voir la carte, Parler au restaurant (+ Mon panier).
- Commander : cuisine fermée → l'heure d'ouverture, « Commander pour plus tard » (une heure dans les horaires de la
  cuisine), « Réserver une table » ; rubriques (seulement celles qui ont des plats disponibles aujourd'hui), plats en
  cartes ou en liste, détails (seulement ce que la fiche dit), accompagnement OBLIGATOIRE quand le plat en propose
  plusieurs, quantité, panier ; puis le mode (à emporter, livraison, sur place, selon le restaurant ; question sautée
  s'il n'y en a qu'un), la commune (frais affichés) et l'adresse, ou le numéro de table ; le paiement (à la livraison,
  au restaurant, ou par le lien s'il existe) ; le récapitulatif et « Valider ».
- Réserver une table : nombre de personnes (1 à 10 ; au-delà, le restaurant reprend la main), jour (seulement les
  jours où il reste de la place), service (midi, soir), heure libre pour ce groupe ; une seule confirmation (Déplacer,
  Annuler, Voir la carte). Sans réservation en ligne : le client écrit son jour et son heure, la demande est transmise.

Boutons (préfixe « g: ») : menu, human, fc (commander), fv (la carte), fr:<rubrique>:<page>, fd:<plat>, fa:<plat>,
fs:<plat>:<accompagnement>, fq:<plat>:<accompagnement|->:<n|x>, fk (panier), fx (vider), fo (commander le panier),
fl (plus tard), ft:<AAAAMMJJHHMM>, fm:<e|l|p> (emporter, livraison, sur place), fz:<commune|x|p<page>>, fy:<s|n>
(adresse), fp:<d|r|k> (paiement), fw (valider) ; fb (réserver), fb:<n|big>, fj:<n>:<AAAAMMJJ>, fe:<n>:<AAAAMMJJ>:<service>,
fh:<n>:<AAAAMMJJHHMM>.
"""
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from app.services.guided_flow import PREFIX, GuidedTurn, _uuid

HUMAN = (f"{PREFIX}human", "Parler au restaurant")
MAX_PARTY = 10
MAX_QTY = 20
PAGE = 10
LATER_STEP = 30  # minutes entre deux heures proposées pour une commande pour plus tard
OTHER = "AUTRES"
MODE_BUTTONS = {"PICKUP": ("e", "À emporter"), "DELIVERY": ("l", "Livraison"), "DINE_IN": ("p", "Sur place")}
MODE_OF = {"e": "PICKUP", "l": "DELIVERY", "p": "DINE_IN"}
PAYMENTS = {"d": "À la livraison", "r": "Au restaurant", "k": "Par lien de paiement"}
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
          "décembre"]


def _you(tu, vous, toi):
    return toi if tu else vous


def _turn(actions=None, **extra) -> GuidedTurn:
    return GuidedTurn(list(actions or []), **extra)


def _buttons(body: str, buttons: list, **extra) -> dict:
    return {"type": "buttons", "body": body, "buttons": buttons[:3], **extra}


def _money(value, currency) -> str:
    from app.services.product_cards import money_text

    return money_text(value, currency or "XOF")


def _zone(tenant):
    from app.services.local_time import tenant_zone

    return tenant_zone(tenant)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hour(moment: datetime) -> str:
    return f"{moment.hour} h" + (f" {moment.minute:02d}" if moment.minute else "")


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Aujourd'hui"
    if day == today + timedelta(days=1):
        return "Demain"
    return f"{DAYS[day.weekday()].capitalize()} {day.day} {MONTHS[day.month - 1]}"


def _when(moment: datetime, now: datetime | None = None) -> str:
    """« aujourd'hui à 19 h 30 », « demain à 12 h », « vendredi 6 novembre à 20 h »."""
    local_now = (now or _now()).astimezone(moment.tzinfo)
    label = _day_label(moment.date(), local_now.date())
    if moment.date() <= local_now.date() + timedelta(days=1):
        label = label.lower()
    return f"{label} à {_hour(moment)}"


# --- La carte -------------------------------------------------------------------------------------------------------

async def _dishes(db, tenant) -> list:
    """Plats et boissons actifs et disponibles aujourd'hui."""
    from sqlalchemy import select

    from app.models.product import Product

    rows = (await db.execute(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True))
                             .order_by(Product.name).limit(1000))).scalars().all()
    return [p for p in rows if (p.stock_quantity or 0) > 0]


def _section(product) -> str:
    from app.services.product_sheets import is_dish

    return product.bob_category if is_dish(product.bob_category) else OTHER


def _section_label(code: str) -> str:
    from app.services.product_sheets import DISH_CATEGORIES

    return DISH_CATEGORIES[code]["label"] if code in DISH_CATEGORIES else "Autres"


def sides_of(product) -> list[str]:
    """« attiéké, alloco ou riz » → ["attiéké", "alloco", "riz"] (10 au plus) ; [] s'il n'y a pas de choix."""
    raw = str((product.sheet or {}).get("sides") or "")
    parts = [" ".join(p.split()) for p in re.split(r",|;|/|\bou\b|\n", raw) if p.strip()]
    seen, out = set(), []
    for part in parts:
        if part.lower() not in seen:
            seen.add(part.lower())
            out.append(part[:60])
    return out[:10] if len(out) >= 2 else []


def _photo(product) -> str | None:
    from app.agents.tools import has_sendable_photo

    return product.image_url.strip() if has_sendable_photo(product) else None


SEATED_HOURS = 3  # lot 108 : après le scan du QR d'une table, le client y est assis pendant 3 h


def _seated(cart) -> str | None:
    """Numéro de la table scannée il y a moins de 3 h (QR posé sur la table), sinon None."""
    meta = _meta(cart) if cart is not None else {}
    try:
        since = datetime.fromisoformat(meta.get("seated") or "")
    except ValueError:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return meta.get("table") if meta.get("table") and _now() - since < timedelta(hours=SEATED_HOURS) else None


def _kept(cart) -> dict | None:
    """Ce qui reste après une commande ou un panier vidé : la table, tant que le client y est assis."""
    meta = _meta(cart)
    return {"table": meta["table"], "seated": meta["seated"]} if _seated(cart) else None


async def at_table(db, tenant, tu, customer, label: str):
    """Lot 108 — le client a scanné le QR de sa table : sur place, table connue, sans rien écrire."""
    from app.services import restaurant

    if "DINE_IN" not in restaurant.modes(tenant):  # « Sur place » coupé dans les réglages : l'accueil habituel
        return await welcome(db, tenant, tu, customer)
    cart = await _cart(db, tenant, customer, create=True)
    _set_meta(cart, table=" ".join(str(label).split())[:8].upper(), seated=_now().isoformat(), later=None)
    return await welcome(db, tenant, tu, customer)


async def welcome(db, tenant, tu, customer=None):
    table = _seated(await _cart(db, tenant, customer)) if customer is not None else None
    if table:
        rows = [(f"{PREFIX}fc", "Commander", None), (f"{PREFIX}fv", "Voir la carte", None)]
        if await _has_items(db, tenant, customer):
            rows.append((f"{PREFIX}fk", "Mon panier", None))
        rows.append((HUMAN[0], "Appeler un serveur", None))
        body = (f"Bienvenue chez *{tenant.name}* ! " + _you(tu, f"Vous êtes à la *table {table}*.", f"Tu es à la *table {table}*.")
                + " " + _you(tu, "Que souhaitez-vous faire ?", "Que veux-tu faire ?"))
        return _turn([{"type": "list", "body": body, "button": "Choisir", "rows": rows}])
    rows = [(f"{PREFIX}fc", "Commander", None),
            (f"{PREFIX}fb", "Réserver une table", None),
            (f"{PREFIX}fv", "Voir la carte", None)]
    if await _has_items(db, tenant, customer):
        rows.append((f"{PREFIX}fk", "Mon panier", None))
    rows.append((HUMAN[0], HUMAN[1], None))
    body = f"Bonjour et bienvenue chez *{tenant.name}* ! " + _you(tu, "Que souhaitez-vous faire ?", "Que veux-tu faire ?")
    return _turn([{"type": "list", "body": body, "button": "Choisir", "rows": rows}])


async def sections(db, tenant, tu, customer=None):
    """Les rubriques qui ont des plats disponibles aujourd'hui, avec leur nombre."""
    from app.services.product_sheets import DISH_CATEGORIES

    dishes = await _dishes(db, tenant)
    if not dishes:
        return _turn([_buttons(_you(tu, "Notre carte n'est pas encore en ligne. Le restaurant peut vous renseigner.",
                                    "Notre carte n'est pas encore en ligne. Le restaurant peut te renseigner."), [HUMAN])])
    counts: dict[str, int] = {}
    for dish in dishes:
        counts[_section(dish)] = counts.get(_section(dish), 0) + 1
    order = [c for c in [*DISH_CATEGORIES, OTHER] if c in counts]
    if len(order) == 1:
        return await listing(db, tenant, tu, customer, order[0], 0)
    rows = [(f"{PREFIX}fr:{code}:0", _section_label(code)[:24], f"{counts[code]} choix") for code in order][:9]
    if await _has_items(db, tenant, customer):
        rows.append((f"{PREFIX}fk", "Mon panier", None))
    return _turn([{"type": "list", "body": _you(tu, "Que vous ferait plaisir ?", "Qu'est-ce qui te ferait plaisir ?"),
                   "button": "Voir la carte", "rows": rows}])


def _card_body(product) -> str:
    """Nom, prix, composition (160 caractères au plus pour une carte)."""
    lines = [f"*{product.name}*", _money(product.price, product.currency)]
    composition = " ".join(str((product.sheet or {}).get("composition") or "").split())
    if composition:
        lines.append(composition)
    text = "\n".join(lines)
    return text if len(text) <= 160 else text[:159].rstrip() + "…"


async def listing(db, tenant, tu, customer, code: str, page: int):
    dishes = [d for d in await _dishes(db, tenant) if _section(d) == code]
    if not dishes:
        return await sections(db, tenant, tu, customer)
    chunk = dishes[page * PAGE:(page + 1) * PAGE] or dishes[:PAGE]
    with_photo = [d for d in chunk if _photo(d)]
    actions = []
    if len(with_photo) >= 2:
        actions.append({"type": "carousel", "body": f"{_section_label(code)} : " + _you(tu, "touchez « Ajouter ».", "touche « Ajouter »."),
                        "cards": [{"link": _photo(d), "body": _card_body(d), "product_id": str(d.id), "name": d.name,
                                   "buttons": [(f"{PREFIX}fa:{d.id}", "Ajouter"), (f"{PREFIX}fd:{d.id}", "Détails")]}
                                  for d in with_photo]})
        rest = [d for d in chunk if not _photo(d)]
    else:
        rest = chunk
    if rest:
        rows = [(f"{PREFIX}fd:{d.id}", d.name[:24], _money(d.price, d.currency)[:72]) for d in rest]
        actions.append({"type": "list", "body": _section_label(code) if not actions else "Et aussi :",
                        "button": "Voir les plats", "rows": rows})
    nav = []
    if len(dishes) > (page + 1) * PAGE:
        nav.append((f"{PREFIX}fr:{code}:{page + 1}", "Voir plus"))
    nav.append((f"{PREFIX}fv", "Autres rubriques"))
    nav.append((f"{PREFIX}fk", "Mon panier") if await _has_items(db, tenant, customer) else HUMAN)
    actions.append(_buttons(_you(tu, "Autre chose ?", "Autre chose ?"), nav))
    return _turn(actions)


def detail_lines(product) -> list[str]:
    """Seulement ce que la fiche du plat dit (jamais une supposition sur les allergènes)."""
    from app.services.product_sheets import NO_ALLERGEN

    sheet = product.sheet or {}
    lines = [f"*{product.name}* — {_money(product.price, product.currency)}"]
    for key, label in (("composition", "Composition"), ("menu_content", "Le menu comprend"), ("sides", "Accompagnements"),
                       ("extras", "Suppléments"), ("spicy", "Piment"), ("portion", "Portion"), ("prep_time", "Préparation"),
                       ("volume", "Contenance")):
        value = " ".join(str(sheet.get(key) or "").split())
        if value:
            lines.append(f"{label} : {value}")
    if sheet.get("vegetarian") == "Oui":
        lines.append("Végétarien")
    allergens = sheet.get("allergens")
    if allergens == NO_ALLERGEN:
        lines.append("Allergènes : aucun des 14 allergènes majeurs")
    elif allergens:
        lines.append(f"Allergènes : {allergens}")
    if product.description:
        text = " ".join(product.description.split())
        lines.append(text if len(text) <= 400 else text[:399].rstrip() + "…")
    return lines


async def details(db, tenant, tu, product):
    if product is None or (product.stock_quantity or 0) <= 0 or not product.active:
        return _turn([_buttons(_you(tu, "Ce plat n'est pas disponible aujourd'hui.", "Ce plat n'est pas disponible aujourd'hui."),
                               [(f"{PREFIX}fv", "Voir la carte"), HUMAN])])
    return _turn([_buttons("\n".join(detail_lines(product))[:1024],
                           [(f"{PREFIX}fa:{product.id}", "Ajouter"), (f"{PREFIX}fv", "Voir la carte"), HUMAN],
                           image=_photo(product), product_id=str(product.id), name=product.name)])


# --- Le panier ------------------------------------------------------------------------------------------------------

async def _cart(db, tenant, customer, create=False):
    from app.services import shop_cart

    return await shop_cart.get_cart(db, tenant, customer, create=create)


def _touch(cart, **fields):
    from app.services import shop_cart

    shop_cart._touch(cart, **fields)


def _meta(cart) -> dict:
    return dict(getattr(cart, "meta", None) or {})


def _set_meta(cart, **values):
    meta = _meta(cart)
    for key, value in values.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    _touch(cart, meta=meta or None)


async def _has_items(db, tenant, customer) -> bool:
    if customer is None:
        return False
    cart = await _cart(db, tenant, customer)
    return bool(cart is not None and cart.items)


async def lines(db, tenant, cart) -> tuple[list[dict], list[str]]:
    """Lignes du panier relues sur la carte (prix du moment) ; un plat devenu indisponible est retiré."""
    from app.models.product import Product

    out, notes, keep = [], [], []
    for item in list(cart.items or []):
        product = await db.get(Product, _uuid(str(item.get("product_id"))))
        if product is None or product.tenant_id != tenant.id or not product.active or (product.stock_quantity or 0) <= 0:
            notes.append(f"{item.get('name') or 'Un plat'} n'est plus disponible aujourd'hui : retiré du panier.")
            continue
        quantity = max(1, min(int(item.get("quantity") or 1), MAX_QTY))
        keep.append({**item, "quantity": quantity})
        price = Decimal(str(product.price or 0))
        out.append({"product": product, "option": item.get("option"), "price": price, "quantity": quantity,
                    "subtotal": price * quantity})
    if keep != list(cart.items or []):
        _touch(cart, items=keep)
    return out, notes


def _line_text(line, currency) -> str:
    option = f" ({line['option']})" if line["option"] else ""
    return f"• {line['product'].name}{option} × {line['quantity']} — {_money(line['subtotal'], currency)}"


def _subtotal(items) -> Decimal:
    return sum((line["subtotal"] for line in items), Decimal(0))


def _currency(tenant, items) -> str:
    return (items[0]["product"].currency if items else None) or tenant.currency or "XOF"


async def add_start(db, tenant, tu, customer, product):
    if product is None or (product.stock_quantity or 0) <= 0:
        return await details(db, tenant, tu, product)
    sides = sides_of(product)
    if sides:  # accompagnement obligatoire (décision du 04/11)
        rows = [(f"{PREFIX}fs:{product.id}:{i}", side[:24], None) for i, side in enumerate(sides)]
        return _turn([{"type": "list", "body": f"*{product.name}* : " + _you(tu, "avec quel accompagnement ?",
                                                                           "avec quel accompagnement ?"),
                       "button": "Choisir", "rows": rows}])
    return quantities(tu, product, None)


def quantities(tu, product, side_index):
    ref = "-" if side_index is None else str(side_index)
    side = sides_of(product)[side_index] if side_index is not None and side_index < len(sides_of(product)) else None
    rows = [(f"{PREFIX}fq:{product.id}:{ref}:{n}", str(n), _money(Decimal(str(product.price)) * n, product.currency))
            for n in range(1, 6)]
    rows.append((f"{PREFIX}fq:{product.id}:{ref}:x", "Autre quantité", f"jusqu'à {MAX_QTY}"))
    name = f"{product.name}{f' (avec {side})' if side else ''}"
    return _turn([{"type": "list", "body": f"*{name}* — " + _you(tu, "combien en voulez-vous ?", "combien en veux-tu ?"),
                   "button": "Choisir la quantité", "rows": rows}])


async def ask_quantity(db, tenant, tu, customer, product, side_index):
    cart = await _cart(db, tenant, customer, create=True)
    _touch(cart, awaiting=f"resto:qty:{product.id}:{'-' if side_index is None else side_index}")
    return _turn([{"type": "text", "body": _you(tu, f"Écrivez le nombre souhaité (1 à {MAX_QTY}).",
                                                f"Écris le nombre souhaité (1 à {MAX_QTY}).")}])


async def add(db, tenant, tu, customer, product, side_index, quantity: int):
    if product is None or (product.stock_quantity or 0) <= 0:
        return await details(db, tenant, tu, product)
    sides = sides_of(product)
    if sides and (side_index is None or not 0 <= side_index < len(sides)):
        return await add_start(db, tenant, tu, customer, product)  # l'accompagnement d'abord
    option = f"Avec {sides[side_index]}" if sides else None
    cart = await _cart(db, tenant, customer, create=True)
    items = [dict(i) for i in (cart.items or [])]
    current = next((i for i in items if i.get("product_id") == str(product.id) and i.get("option") == option), None)
    notes = []
    total = (int(current["quantity"]) if current else 0) + quantity
    if total > MAX_QTY:
        notes.append(f"{MAX_QTY} au plus par plat : pour une grande commande, le restaurant vous répond directement.")
        total = MAX_QTY
    if current:
        current["quantity"] = total
    else:
        items.append({"product_id": str(product.id), "variant": None, "option": option, "name": product.name[:80],
                      "quantity": total})
    _touch(cart, items=items, awaiting=None)
    return await show(db, tenant, tu, customer, notes=notes, added=True)


async def show(db, tenant, tu, customer, notes=None, added=False):
    cart = await _cart(db, tenant, customer)
    items, more = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    notes = list(notes or []) + more
    if not items:
        body = "\n".join(notes + [_you(tu, "Votre panier est vide.", "Ton panier est vide.")])
        return _turn([_buttons(body, [(f"{PREFIX}fv", "Voir la carte"), HUMAN])])
    currency = _currency(tenant, items)
    head = "Ajouté au panier ✅" if added else _you(tu, "Votre panier :", "Ton panier :")
    body = "\n".join(notes + [head, ""] + [_line_text(line, currency) for line in items]
                     + ["", f"*Total : {_money(_subtotal(items), currency)}*"])
    return _turn([_buttons(body, [(f"{PREFIX}fo", "Commander"), (f"{PREFIX}fv", "Ajouter un plat"),
                                  (f"{PREFIX}fx", "Vider le panier")])])


async def clear(db, tenant, tu, customer):
    cart = await _cart(db, tenant, customer)
    if cart is not None:
        _touch(cart, items=[], fulfillment=None, address=None, awaiting=None, meta=_kept(cart))
    return _turn([_buttons(_you(tu, "Votre panier est vidé.", "Ton panier est vidé."), [(f"{PREFIX}fv", "Voir la carte"), HUMAN])])


# --- Cuisine fermée, commande pour plus tard ------------------------------------------------------------------------

def later_times(tenant, now: datetime | None = None, limit: int = 9) -> list[datetime]:
    """Prochaines heures de commande possibles (toutes les 30 min, dans les horaires de la cuisine, 3 jours)."""
    from app.services.booking import _minutes

    hours = getattr(tenant, "kitchen_hours", None) or {}
    zone = _zone(tenant)
    local = (now or _now()).astimezone(zone)
    earliest = local + timedelta(minutes=LATER_STEP)
    out = []
    for offset in range(3):
        day = local.date() + timedelta(days=offset)
        for start_txt, end_txt in hours.get(str(day.weekday())) or []:
            minute, end = _minutes(start_txt), _minutes(end_txt)
            while minute + LATER_STEP <= end:
                moment = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=zone)
                if moment >= earliest:
                    out.append(moment)
                minute += LATER_STEP
    return out[:limit]


def _later(cart, tenant) -> datetime | None:
    raw = _meta(cart).get("later") if cart is not None else None
    if not raw:
        return None
    try:
        moment = datetime.strptime(raw, "%Y%m%d%H%M").replace(tzinfo=_zone(tenant))
    except ValueError:
        return None
    return moment if moment > _now() else None


async def start_order(db, tenant, tu, customer):
    """« Commander » : la carte si la cuisine est ouverte ; sinon l'heure d'ouverture et le choix."""
    from app.services import restaurant

    cart = await _cart(db, tenant, customer) if customer is not None else None
    if restaurant.kitchen_open(tenant) or _later(cart, tenant):
        return await sections(db, tenant, tu, customer)
    return closed(tenant, tu)


def closed(tenant, tu):
    from app.services import restaurant

    when = restaurant.next_opening(tenant)
    body = ("La cuisine est fermée pour le moment" + (f" : elle ouvre {restaurant.opening_text(when)}." if when else ".")
            + " " + _you(tu, "Vous pouvez commander pour plus tard ou réserver une table.",
                         "Tu peux commander pour plus tard ou réserver une table."))
    return _turn([_buttons(body, [(f"{PREFIX}fl", "Pour plus tard"), (f"{PREFIX}fb", "Réserver une table"),
                                  (f"{PREFIX}fv", "Voir la carte")])])


async def ask_later(db, tenant, tu, customer):
    times = later_times(tenant)
    if not times:
        return _turn([_buttons("Le restaurant ne prend pas de commande dans les prochains jours.", [(f"{PREFIX}fb", "Réserver une table"), HUMAN])])
    rows = [(f"{PREFIX}ft:{t.strftime('%Y%m%d%H%M')}", _when(t)[:24].capitalize(), None) for t in times]
    return _turn([{"type": "list", "body": _you(tu, "Pour quelle heure ?", "Pour quelle heure ?"), "button": "Choisir l'heure",
                   "rows": rows}])


async def set_later(db, tenant, tu, customer, stamp: str):
    try:
        moment = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=_zone(tenant))
    except ValueError:
        return await ask_later(db, tenant, tu, customer)
    if moment not in later_times(tenant, limit=200):
        return await ask_later(db, tenant, tu, customer)
    cart = await _cart(db, tenant, customer, create=True)
    _set_meta(cart, later=stamp)
    if cart.items:
        return await order(db, tenant, tu, customer)
    return await sections(db, tenant, tu, customer)


# --- Remise, paiement, validation -----------------------------------------------------------------------------------

async def order(db, tenant, tu, customer):
    """« Commander » le panier : cuisine (ou heure choisie), puis le mode."""
    from app.services import restaurant

    cart = await _cart(db, tenant, customer)
    items, notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    if not items:
        return await show(db, tenant, tu, customer, notes=notes)
    if notes:
        return await show(db, tenant, tu, customer, notes=notes)
    if not restaurant.kitchen_open(tenant) and not _later(cart, tenant):
        return closed(tenant, tu)
    modes = restaurant.modes(tenant)
    if not modes:
        return _turn([_buttons("Le restaurant ne prend pas de commande sur WhatsApp pour le moment.", [HUMAN])])
    if "DINE_IN" in modes and _seated(cart):  # lot 108 : QR de la table, ni mode ni numéro à demander
        return await choose_mode(db, tenant, tu, customer, "DINE_IN")
    if len(modes) == 1:
        return await choose_mode(db, tenant, tu, customer, modes[0])
    buttons = [(f"{PREFIX}fm:{MODE_BUTTONS[m][0]}", MODE_BUTTONS[m][1]) for m in modes]
    return _turn([_buttons(_you(tu, "Comment souhaitez-vous recevoir votre commande ?", "Comment veux-tu recevoir ta commande ?"),
                           buttons)])


async def choose_mode(db, tenant, tu, customer, mode: str):
    from app.services import restaurant

    cart = await _cart(db, tenant, customer)
    if cart is None or not cart.items or mode not in restaurant.modes(tenant):
        return await order(db, tenant, tu, customer)
    _touch(cart, fulfillment=mode)
    _set_meta(cart, area=None, payment=None)
    if mode == "DELIVERY":
        return areas(tenant, tu, 0)
    if mode == "DINE_IN":
        if _meta(cart).get("table"):
            return await payment(db, tenant, tu, customer)
        _touch(cart, awaiting="resto:table")
        return _turn([{"type": "text", "body": _you(tu, "Écrivez le numéro de votre table (il est écrit sur la table).",
                                                    "Écris le numéro de ta table (il est écrit sur la table).")}])
    _touch(cart, address=None, awaiting=None)
    return await payment(db, tenant, tu, customer)


def areas(tenant, tu, page: int):
    zones = getattr(tenant, "delivery_areas", None) or []
    if not zones:
        return None
    chunk = zones[page * 9:(page + 1) * 9]
    rows = [(f"{PREFIX}fz:{page * 9 + i}", a["name"][:24], _money(a["fee"], tenant.currency) if a["fee"] else "Livraison offerte")
            for i, a in enumerate(chunk)]
    if len(zones) > (page + 1) * 9:
        rows.append((f"{PREFIX}fz:p{page + 1}", "Autres communes", None))
    else:
        rows.append((f"{PREFIX}fz:x", "Ma commune n'y est pas", None))
    return _turn([{"type": "list", "body": _you(tu, "Dans quelle commune livrer ?", "Dans quelle commune livrer ?"),
                   "button": "Choisir la commune", "rows": rows[:10]}])


async def delivery_start(db, tenant, tu, customer):
    turn = areas(tenant, tu, 0)
    if turn is not None:
        return turn
    return await ask_address(db, tenant, tu, customer)


async def choose_area(db, tenant, tu, customer, raw: str):
    zones = getattr(tenant, "delivery_areas", None) or []
    if raw.startswith("p") and raw[1:].isdigit():
        return areas(tenant, tu, int(raw[1:])) or await ask_address(db, tenant, tu, customer)
    if raw == "x":
        from app.services import restaurant

        others = [(f"{PREFIX}fm:{MODE_BUTTONS[m][0]}", MODE_BUTTONS[m][1]) for m in restaurant.modes(tenant) if m != "DELIVERY"]
        names = ", ".join(a["name"] for a in zones)
        return _turn([_buttons(f"Nous livrons seulement : {names}.", (others + [HUMAN])[:3])])
    if not raw.isdigit() or int(raw) >= len(zones):
        return areas(tenant, tu, 0)
    cart = await _cart(db, tenant, customer)
    if cart is None or not cart.items:
        return await show(db, tenant, tu, customer)
    _set_meta(cart, area=zones[int(raw)]["name"])
    return await ask_address(db, tenant, tu, customer)


async def ask_address(db, tenant, tu, customer):
    cart = await _cart(db, tenant, customer, create=True)
    _touch(cart, fulfillment="DELIVERY")
    if customer.address:
        _touch(cart, awaiting=None)
        return _turn([_buttons(_you(tu, "Livraison à cette adresse ?", "Livraison à cette adresse ?") + f"\n\n{customer.address}",
                               [(f"{PREFIX}fy:s", "Oui, cette adresse"), (f"{PREFIX}fy:n", "Autre adresse")])])
    _touch(cart, awaiting="resto:address")
    return _turn([{"type": "text", "body": _you(
        tu, "Écrivez l'adresse de livraison : quartier, rue, un repère, et un numéro joignable si ce n'est pas celui-ci.",
        "Écris l'adresse de livraison : quartier, rue, un repère, et un numéro joignable si ce n'est pas celui-ci.")}])


async def address_choice(db, tenant, tu, customer, which: str):
    cart = await _cart(db, tenant, customer)
    if cart is None or not cart.items:
        return await show(db, tenant, tu, customer)
    if which == "s" and customer.address:
        _touch(cart, address=customer.address[:500], awaiting=None)
        return await payment(db, tenant, tu, customer)
    _touch(cart, awaiting="resto:address")
    return _turn([{"type": "text", "body": _you(tu, "Écrivez l'adresse de livraison : quartier, rue, un repère.",
                                                "Écris l'adresse de livraison : quartier, rue, un repère.")}])


def _payment_choices(tenant, mode: str) -> list[str]:
    out = ["d" if mode == "DELIVERY" else "r"]
    if getattr(tenant, "payment_link", None):
        out.append("k")
    return out


async def payment(db, tenant, tu, customer):
    cart = await _cart(db, tenant, customer)
    if cart is None or not cart.items or not cart.fulfillment:
        return await order(db, tenant, tu, customer)
    choices = _payment_choices(tenant, cart.fulfillment)
    if len(choices) == 1:
        return await choose_payment(db, tenant, tu, customer, choices[0])
    labels = {"d": "Payer à la livraison", "r": "Payer sur place" if cart.fulfillment == "DINE_IN" else "Payer au retrait",
              "k": "Payer par lien"}
    return _turn([_buttons(_you(tu, "Comment souhaitez-vous payer ?", "Comment veux-tu payer ?"),
                           [(f"{PREFIX}fp:{c}", labels[c]) for c in choices])])


async def choose_payment(db, tenant, tu, customer, which: str):
    cart = await _cart(db, tenant, customer)
    if cart is None or not cart.items or not cart.fulfillment:
        return await order(db, tenant, tu, customer)
    if which not in _payment_choices(tenant, cart.fulfillment):
        return await payment(db, tenant, tu, customer)
    _set_meta(cart, payment=which)
    return await recap(db, tenant, tu, customer)


def _terms(tenant, cart) -> dict:
    """Frais (commune), table, adresse ; ValueError si une étape manque."""
    meta = _meta(cart)
    mode = cart.fulfillment
    if mode == "DELIVERY":
        zones = getattr(tenant, "delivery_areas", None) or []
        if zones:
            from app.services import restaurant

            found = restaurant.area_of(tenant, meta.get("area"))
            if found is None:
                raise ValueError("area")
            fee, area = found["fee"], found["name"]
        else:
            fee, area = (float(tenant.delivery_fee) if getattr(tenant, "delivery_fee", None) is not None else None), None
        if not cart.address:
            raise ValueError("address")
        return {"fulfillment": mode, "delivery_fee": fee, "delivery_area": area, "table_number": None, "address": cart.address}
    if mode == "DINE_IN":
        if not meta.get("table"):
            raise ValueError("table")
        return {"fulfillment": mode, "delivery_fee": None, "delivery_area": None, "table_number": meta["table"], "address": None}
    return {"fulfillment": mode, "delivery_fee": None, "delivery_area": None, "table_number": None, "address": None}


async def _missing_step(db, tenant, tu, customer, step: str):
    if step == "area":
        return await delivery_start(db, tenant, tu, customer)
    if step == "address":
        return await ask_address(db, tenant, tu, customer)
    return await choose_mode(db, tenant, tu, customer, "DINE_IN")


async def recap(db, tenant, tu, customer):
    from app.services import restaurant

    cart = await _cart(db, tenant, customer)
    items, notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    if not items or notes:
        return await show(db, tenant, tu, customer, notes=notes)
    if cart.fulfillment not in restaurant.modes(tenant):
        return await order(db, tenant, tu, customer)
    try:
        terms = _terms(tenant, cart)
    except ValueError as missing:
        return await _missing_step(db, tenant, tu, customer, str(missing))
    currency = _currency(tenant, items)
    total = _subtotal(items)
    body = ["*Récapitulatif*", ""] + [_line_text(line, currency) for line in items] + [""]
    if terms["fulfillment"] == "DELIVERY":
        fee = terms["delivery_fee"]
        where = f" ({terms['delivery_area']})" if terms["delivery_area"] else ""
        body.append(f"Livraison{where} : " + ("offerte" if fee == 0 else _money(fee, currency) if fee is not None
                                              else "frais précisés par le restaurant"))
        total += Decimal(str(fee or 0))
        body.append(f"Adresse : {terms['address']}")
    elif terms["fulfillment"] == "DINE_IN":
        body.append(f"Sur place, table {terms['table_number']}")
    else:
        body.append("À emporter" + (f" : {tenant.pickup_address}" if getattr(tenant, "pickup_address", None) else ""))
    later = _later(cart, tenant)
    body.append(f"Pour : {_when(later)}" if later else "Dès que possible")
    body.append(f"Paiement : {PAYMENTS.get(_meta(cart).get('payment'), 'Au restaurant').lower()}")
    body += ["", f"*Total : {_money(total, currency)}*"]
    return _turn([_buttons("\n".join(body), [(f"{PREFIX}fw", "Valider"), (f"{PREFIX}fk", "Modifier")])])


async def _already_or_empty(db, tenant, tu, customer, notes):
    """Panier vide au moment de valider : commande déjà enregistrée (double appui) ou rien choisi."""
    from sqlalchemy import select

    from app.models.order import Order

    if notes:
        return await show(db, tenant, tu, customer, notes=notes)
    recent = (await db.execute(select(Order).where(
        Order.tenant_id == tenant.id, Order.customer_id == customer.id,
        Order.created_at >= _now() - timedelta(minutes=30)).order_by(Order.created_at.desc()).limit(1))).scalar_one_or_none()
    if recent is not None:
        return _turn([_buttons(_you(tu, f"Votre commande n° {str(recent.id)[:8].upper()} est bien enregistrée.",
                                    f"Ta commande n° {str(recent.id)[:8].upper()} est bien enregistrée."),
                               [(f"{PREFIX}s", "Suivre ma commande"), (f"{PREFIX}fv", "Voir la carte")])])
    return await show(db, tenant, tu, customer)


async def validate(db, tenant, tu, customer, conversation):
    from app.services import restaurant
    from app.services.order_service import OrderCreationError, create_order
    from app.services.receipt_service import generate_order_confirmation_text, get_order_item_lines

    cart = await _cart(db, tenant, customer)
    items, notes = (await lines(db, tenant, cart)) if cart is not None else ([], [])
    if not items:
        return await _already_or_empty(db, tenant, tu, customer, notes)
    if notes or cart.fulfillment not in restaurant.modes(tenant):
        return await recap(db, tenant, tu, customer)
    later = _later(cart, tenant)
    if not later and not restaurant.kitchen_open(tenant):
        return closed(tenant, tu)
    try:
        terms = _terms(tenant, cart)
    except ValueError as missing:
        return await _missing_step(db, tenant, tu, customer, str(missing))
    pay = _meta(cart).get("payment") or _payment_choices(tenant, cart.fulfillment)[0]
    try:
        created = await create_order(
            db, tenant_id=tenant.id, customer_id=customer.id,
            items=[{"product_id": str(line["product"].id), "quantity": line["quantity"], "option": line["option"]} for line in items],
            delivery_address=terms["address"], payment_method=PAYMENTS[pay], created_by="IA",
            conversation_id=conversation.id if conversation else None, delivery_fee=terms["delivery_fee"],
            fulfillment=terms["fulfillment"], table_number=terms["table_number"], delivery_area=terms["delivery_area"],
            scheduled_for=later.astimezone(timezone.utc) if later else None)
    except OrderCreationError as exc:
        return _turn([_buttons(f"{exc.message}.", [(f"{PREFIX}fk", "Voir mon panier"), HUMAN])])
    _touch(cart, items=[], fulfillment=None, address=None, awaiting=None, meta=_kept(cart))
    text = generate_order_confirmation_text(created, await get_order_item_lines(db, created.id, created.currency), tenant.name,
                                            tenant.payment_link if pay == "k" else None, tu=tu,
                                            pay_on=None if pay == "k" else PAYMENTS[pay].lower())
    if created.fulfillment == "PICKUP" and getattr(tenant, "pickup_address", None):
        text += f"\nRetrait : {tenant.pickup_address}"
    buttons = [(f"{PREFIX}s", "Suivre ma commande"), (f"{PREFIX}fv", "Voir la carte")]
    from app.services.guided_flow import confirmation

    return _turn(confirmation(text, buttons, created.id, tu))  # lot 112 : reçu en image, sinon le texte


# --- Réserver une table ---------------------------------------------------------------------------------------------

def party_question(tu):
    rows = [(f"{PREFIX}fb:{n}", f"{n} personne{'s' if n > 1 else ''}", None) for n in range(1, MAX_PARTY)]
    rows.append((f"{PREFIX}fb:big", f"{MAX_PARTY} ou plus", "Le restaurant vous répond"))
    return _turn([{"type": "list", "body": _you(tu, "Pour combien de personnes ?", "Pour combien de personnes ?"),
                   "button": "Choisir", "rows": rows}])


async def _booking(db, tenant):
    from app.services import booking

    settings = await booking.load_settings(db, tenant.id)
    return settings if booking.booking_enabled(settings) else None


async def days(db, tenant, tu, customer, party: int):
    from app.services import booking

    settings = await _booking(db, tenant)
    if settings is None:  # pas de réservation en ligne : le client écrit son jour et son heure
        cart = await _cart(db, tenant, customer, create=True)
        _touch(cart, awaiting=f"resto:avail:{party}")
        return _turn([{"type": "text", "body": f"Pour {party} personne{'s' if party > 1 else ''}. "
                       + _you(tu, "Quel jour et à quelle heure ? (par exemple : samedi à 20 h)",
                              "Quel jour et à quelle heure ? (par exemple : samedi à 20 h)")}])
    zone, now = _zone(tenant), _now()
    today = now.astimezone(zone).date()
    rows = []
    for offset in range(booking.HORIZON_DAYS + 1):
        day = today + timedelta(days=offset)
        free = await booking.free_slots(db, tenant, settings, zone, now, day=day, party=party, limit=100)
        if free:
            rows.append((f"{PREFIX}fj:{party}:{day.strftime('%Y%m%d')}", _day_label(day, today)[:24], None))
    if not rows:
        return _turn([_buttons(f"Plus de table libre pour {party} personnes dans les 7 prochains jours.", [HUMAN, (f"{PREFIX}fv", "Voir la carte")])])
    return _turn([{"type": "list", "body": _you(tu, "Quel jour ?", "Quel jour ?"), "button": "Choisir le jour", "rows": rows[:10]}])


def _clock(minute: int) -> str:
    return f"{minute // 60} h" + (f" {minute % 60:02d}" if minute % 60 else "")


def _service_name(start_minute: int) -> str:
    if start_minute < 11 * 60:
        return "Matin"
    if start_minute < 17 * 60:
        return "Midi"
    return "Soir"


async def services(db, tenant, tu, customer, party: int, raw_day: str):
    from app.services import booking

    settings = await _booking(db, tenant)
    try:
        day = datetime.strptime(raw_day, "%Y%m%d").date()
    except ValueError:
        return await days(db, tenant, tu, customer, party)
    if settings is None:
        return await days(db, tenant, tu, customer, party)
    zone, now = _zone(tenant), _now()
    free = await booking.free_slots(db, tenant, settings, zone, now, day=day, party=party, limit=100)
    groups: dict = {}
    for slot in free:
        groups.setdefault(booking._service_of(slot, settings), []).append(slot)
    if not groups:
        return await days(db, tenant, tu, customer, party)
    if len(groups) == 1:
        return times(tu, party, next(iter(groups.values())))
    rows = [(f"{PREFIX}fe:{party}:{raw_day}:{i}", _service_name(key[0]), f"De {_clock(key[0])} à {_clock(key[1])}")
            for i, key in enumerate(sorted(groups))]
    return _turn([{"type": "list", "body": _you(tu, "Plutôt midi ou soir ?", "Plutôt midi ou soir ?"), "button": "Choisir", "rows": rows}])


async def service_times(db, tenant, tu, customer, party: int, raw_day: str, index: int):
    from app.services import booking

    settings = await _booking(db, tenant)
    try:
        day = datetime.strptime(raw_day, "%Y%m%d").date()
    except ValueError:
        return await days(db, tenant, tu, customer, party)
    if settings is None:
        return await days(db, tenant, tu, customer, party)
    free = await booking.free_slots(db, tenant, settings, _zone(tenant), _now(), day=day, party=party, limit=100)
    groups: dict = {}
    for slot in free:
        groups.setdefault(booking._service_of(slot, settings), []).append(slot)
    keys = sorted(groups)
    if not 0 <= index < len(keys):
        return await services(db, tenant, tu, customer, party, raw_day)
    return times(tu, party, groups[keys[index]])


def times(tu, party: int, slots: list):
    rows = [(f"{PREFIX}fh:{party}:{s.strftime('%Y%m%d%H%M')}", _hour(s), None) for s in slots[:10]]
    return _turn([{"type": "list", "body": _you(tu, "À quelle heure ?", "À quelle heure ?"), "button": "Choisir l'heure", "rows": rows}])


async def book(db, tenant, tu, customer, conversation, party: int, slot: str | None = None, availability: str | None = None):
    """La réservation, par le même outil que Bob (couverts revérifiés sous verrou)."""
    from app.agents.tools import ToolExecutor
    from app.services import appointment_service, followups

    tools = ToolExecutor(db, tenant.id, conversation, customer.id, business_type=tenant.business_type)
    tools.client_chose_other_time = availability is not None
    request = {"kind": "TABLE", "party_size": party}
    if slot:
        request["slot"] = slot
    if availability:
        request["availability"] = availability
    result = await tools._tool_request_appointment(request)
    cart = await _cart(db, tenant, customer)
    if cart is not None and (cart.awaiting or "").startswith("resto:avail"):
        _touch(cart, awaiting=None)
    if result.get("status") == "appointment_confirmed" and tools.booking_outbox:
        appointment = tools.booking_outbox[-1]
        text = appointment_service.confirmation_message(appointment, tenant.name, _zone(tenant))
        buttons = (followups.booking_buttons(tenant, appointment) or [])[:2] + [(f"{PREFIX}fv", "Voir la carte")]
        return _turn([{"type": "buttons", "body": text, "message_type": "appointment_confirmed", "buttons": buttons}],
                     booked=[appointment])
    if result.get("status") == "appointment_requested":
        return _turn(handoff=True, handoff_done=True, handoff_text=(
            f"Demande de réservation envoyée pour {party} personne{'s' if party > 1 else ''}, {availability}. "
            + _you(tu, "Le restaurant vous confirme l'heure ici.", "Le restaurant te confirme l'heure ici.")))
    if result.get("available_slots") is not None or result.get("slots") is not None:
        turn = await days(db, tenant, tu, customer, party)
        turn.actions.insert(0, {"type": "text", "body": "Cette heure vient d'être prise."})
        return turn
    if "Le jour et la date" in (result.get("error") or ""):
        return _turn([{"type": "text", "body": (result.get("instruction") or "").split("question : ", 1)[-1] or result["error"]}])
    return _turn([_buttons(result.get("error") or "Je n'ai pas pu enregistrer la réservation.", [HUMAN, (f"{PREFIX}menu", "Menu")])])


# --- Messages écrits attendus ---------------------------------------------------------------------------------------

async def expects_text(db, tenant, customer, text) -> bool:
    from app.services import shop_cart

    if shop_cart.keyword(text):
        return True
    wanted = shop_cart.awaiting(await _cart(db, tenant, customer))
    if not wanted or not wanted.startswith("resto:"):
        return False
    if wanted.startswith("resto:qty:"):
        return bool(re.fullmatch(r"\s*\d{1,3}\s*", text or ""))
    if wanted == "resto:table":
        return bool(re.fullmatch(r"\s*(?:table\s*)?[\w-]{1,8}\s*", text or "", re.IGNORECASE))
    return 3 <= len((text or "").strip()) <= 300 and "?" not in (text or "")


async def handle_text(db, tenant, tu, customer, conversation, text):
    from app.models.product import Product
    from app.services import auto_flow, shop_cart

    word = shop_cart.keyword(text)
    if word == "cart":
        return await show(db, tenant, tu, customer)
    if word == "track":
        return await shop_cart.track(db, tenant, tu, customer)
    cart = await _cart(db, tenant, customer)
    wanted = shop_cart.awaiting(cart)
    if not wanted or not wanted.startswith("resto:"):
        return None
    clean = " ".join((text or "").split())
    if wanted.startswith("resto:qty:") and re.fullmatch(r"\d{1,3}", clean):
        _, _, raw_product, raw_side = wanted.split(":")
        product = await db.get(Product, _uuid(raw_product))
        if product is None or product.tenant_id != tenant.id:
            _touch(cart, awaiting=None)
            return None
        side = None if raw_side == "-" else int(raw_side)
        if not 1 <= int(clean) <= MAX_QTY:
            return await ask_quantity(db, tenant, tu, customer, product, side)
        return await add(db, tenant, tu, customer, product, side, int(clean))
    if wanted == "resto:table":
        match = re.fullmatch(r"(?:table\s*)?([\w-]{1,8})", clean, re.IGNORECASE)
        if match:
            _touch(cart, awaiting=None)
            _set_meta(cart, table=match.group(1).upper())
            return await payment(db, tenant, tu, customer)
    if wanted == "resto:address" and 3 <= len(clean) <= 500 and "?" not in clean:
        _touch(cart, address=clean[:500], awaiting=None)
        return await payment(db, tenant, tu, customer)
    if wanted.startswith("resto:avail:") and auto_flow._plausible(clean):
        party = int(wanted.rsplit(":", 1)[1])
        return await book(db, tenant, tu, customer, conversation, party, availability=clean[:200])
    _touch(cart, awaiting=None)  # le client parle d'autre chose : l'IA répond
    return None


# --- Aiguillage -----------------------------------------------------------------------------------------------------

async def handle(db, tenant, text, reply_id, referral=None, tu=False, customer=None, conversation=None):
    from app.services.guided_flow import wants_welcome

    if reply_id and reply_id.startswith(PREFIX):
        parts = reply_id[len(PREFIX):].split(":")
        turn = await _tap(db, tenant, tu, customer, conversation, parts[0], parts)
        return turn if turn is not None else await welcome(db, tenant, tu, customer)
    if not reply_id and wants_welcome(text, referral):
        return await welcome(db, tenant, tu, customer)
    if not reply_id and customer is not None:
        return await handle_text(db, tenant, tu, customer, conversation, text)
    return None


async def _dish(db, tenant, raw):
    from app.models.product import Product

    product = await db.get(Product, _uuid(raw)) if _uuid(raw) else None
    return product if product is not None and product.tenant_id == tenant.id and product.active else None


async def _tap(db, tenant, tu, customer, conversation, action, parts):  # noqa: C901 — un aiguillage, écran par écran
    from app.services import shop_cart

    if action == "menu":
        return await welcome(db, tenant, tu, customer)
    if action == "human":
        table = _seated(await _cart(db, tenant, customer)) if customer is not None else None
        if table:  # lot 108 : à table, « Appeler un serveur »
            return _turn(handoff=True, handoff_reason=f"Table {table} : le client appelle un serveur",
                         handoff_text=_you(tu, f"Je préviens l'équipe : quelqu'un vient vous voir à la table {table}.",
                                           f"Je préviens l'équipe : quelqu'un vient te voir à la table {table}."))
        return _turn(handoff=True, handoff_text=_you(tu, "Je préviens le restaurant : il vous répond ici dès que possible.",
                                                     "Je préviens le restaurant : il te répond ici dès que possible."))
    if action in ("fv", "cat"):  # « cat » : boutons communs (suivi, livraison reçue, reçu)
        return await sections(db, tenant, tu, customer)
    if action == "fr" and len(parts) == 3 and parts[2].isdigit():
        return await listing(db, tenant, tu, customer, parts[1], int(parts[2]))
    if action == "fd" and len(parts) == 2:
        return await details(db, tenant, tu, await _dish(db, tenant, parts[1]))
    if customer is None or conversation is None:
        return None
    if action == "fc":
        return await start_order(db, tenant, tu, customer)
    if action == "fa" and len(parts) == 2:
        return await add_start(db, tenant, tu, customer, await _dish(db, tenant, parts[1]))
    if action == "fs" and len(parts) == 3 and parts[2].isdigit():
        product = await _dish(db, tenant, parts[1])
        return quantities(tu, product, int(parts[2])) if product is not None and int(parts[2]) < len(sides_of(product)) \
            else await add_start(db, tenant, tu, customer, product)
    if action == "fq" and len(parts) == 4:
        product = await _dish(db, tenant, parts[1])
        if product is None or not (parts[2] == "-" or parts[2].isdigit()):
            return await details(db, tenant, tu, product)
        side = None if parts[2] == "-" else int(parts[2])
        if parts[3] == "x":
            return await ask_quantity(db, tenant, tu, customer, product, side)
        if parts[3].isdigit() and 1 <= int(parts[3]) <= MAX_QTY:
            return await add(db, tenant, tu, customer, product, side, int(parts[3]))
        return None
    if action in ("fk", "k"):
        return await show(db, tenant, tu, customer)
    if action == "fx":
        return await clear(db, tenant, tu, customer)
    if action == "fo":
        return await order(db, tenant, tu, customer)
    if action == "fl":
        return await ask_later(db, tenant, tu, customer)
    if action == "ft" and len(parts) == 2:
        return await set_later(db, tenant, tu, customer, parts[1])
    if action == "fm" and len(parts) == 2 and parts[1] in MODE_OF:
        return await choose_mode(db, tenant, tu, customer, MODE_OF[parts[1]])
    if action == "fz" and len(parts) == 2:
        return await choose_area(db, tenant, tu, customer, parts[1])
    if action == "fy" and len(parts) == 2:
        return await address_choice(db, tenant, tu, customer, parts[1])
    if action == "fp" and len(parts) == 2:
        return await choose_payment(db, tenant, tu, customer, parts[1])
    if action == "fw":
        return await validate(db, tenant, tu, customer, conversation)
    if action == "s":
        return await shop_cart.track(db, tenant, tu, customer)
    if action == "fb" and len(parts) == 1:
        return party_question(tu)
    if action == "fb" and len(parts) == 2:
        if parts[1] == "big":
            return _turn(handoff=True, handoff_reason=f"Réservation pour un groupe de {MAX_PARTY} personnes ou plus",
                         handoff_text=_you(tu, "Pour un groupe, le restaurant vous répond ici directement pour organiser votre venue.",
                                           "Pour un groupe, le restaurant te répond ici directement pour organiser ta venue."))
        if parts[1].isdigit() and 1 <= int(parts[1]) < MAX_PARTY:
            return await days(db, tenant, tu, customer, int(parts[1]))
        return party_question(tu)
    if action == "fj" and len(parts) == 3 and parts[1].isdigit():
        return await services(db, tenant, tu, customer, int(parts[1]), parts[2])
    if action == "fe" and len(parts) == 4 and parts[1].isdigit() and parts[3].isdigit():
        return await service_times(db, tenant, tu, customer, int(parts[1]), parts[2], int(parts[3]))
    if action == "fh" and len(parts) == 3 and parts[1].isdigit() and re.fullmatch(r"\d{12}", parts[2]):
        stamp = parts[2]
        slot = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:{stamp[10:]}"
        return await book(db, tenant, tu, customer, conversation, int(parts[1]), slot=slot)
    return None
