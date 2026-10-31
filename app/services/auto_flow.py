"""
Lot 96 — parcours à boutons des concessions (écrans « Auto » de la maquette), validé le 31/10.

Pas de panier : le but est un ESSAI, une ESTIMATION DE REPRISE ou un RAPPEL, chacun devenant un rendez-vous (confirmé
s'il est pris sur un créneau libre de l'agenda, sinon « à confirmer » et transmis au conseiller), exactement comme
quand Bob le fait par écrit : c'est le même outil (request_appointment) qui enregistre, réserve et prévient.
Tout est écrit par le code, avec les vraies données : jamais de mensualité, jamais de valeur de reprise (lot 24).

Décisions du 31/10 : accueil en LISTE de 5 choix (véhicules, essai, estimation, financement, conseiller) ;
bouton du conseiller « Un conseiller » (20 caractères au plus) ; neuf ou occasion (question sautée si le stock n'a
qu'un des deux) ; budget en 3 tranches calculées depuis le stock (sautée jusqu'à 10 véhicules) ; état « neuf /
occasion » de la fiche véhicule, déduit du kilométrage s'il est vide.

Boutons (préfixe « g: ») : g:menu, g:veh, g:vc:<N|O|A>, g:vl:<filtres>:<page>, g:af:<filtres>, g:d:<véhicule>,
g:e:<véhicule|-|x>, g:sl:<E|R>:<véhicule|->:<AAAAMMJJHHMM|x>, g:tr, g:ty:<i>, g:tk:<i>, g:fi, g:cb, g:human.
Filtres : « cO,l8000000,h15000000,bSUV,fDIESEL,gAUTOMATIQUE » (état, prix mini/maxi, carrosserie, carburant, boîte).
"""
import re
import uuid
from decimal import Decimal

from app.services import vehicle as vehicle_service

PREFIX = "g:"
PAGE = 10
HUMAN = (f"{PREFIX}human", "Un conseiller")
KINDS = {"E": "ESSAI", "R": "ESTIMATION_REPRISE", "V": "VISITE", "S": "ESTIMATION",  # V, S : agence (lot 97)
         "A": "APPEL"}  # A : courtier, « Être rappelé » (lot 98)
YEARS = ["Avant 2010", "2010 à 2014", "2015 à 2018", "2019 à 2021", "2022 ou plus récent"]
MILEAGES = ["Moins de 50 000 km", "50 000 à 100 000 km", "100 000 à 150 000 km", "Plus de 150 000 km"]
CONDITION_PROFILE = {"N": "NEUF", "O": "OCCASION", "A": "INDIFFERENT"}
REFINE = (("b", "body_type", vehicle_service.BODY_TYPES), ("f", "fuel", vehicle_service.FUELS),
          ("g", "gearbox", vehicle_service.GEARBOXES))
SEP = " · "


def _turn(actions=None, **kw):
    from app.services.guided_flow import GuidedTurn

    return GuidedTurn(actions or [], **kw)


def _you(tu: bool, vous: str, toi: str) -> str:
    return toi if tu else vous


def _buttons(body, buttons, **extra):
    return {"type": "buttons", "body": body, "buttons": buttons[:3], **extra}


def _uuid(raw):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _price(product) -> Decimal:
    try:
        return Decimal(str(product.price or 0))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def amount_label(value, currency: str) -> str:
    """12 500 000 XOF → « 12,5 millions » ; ailleurs, le montant écrit (« 18 000 EUR »)."""
    from app.services.product_cards import money_text

    value = Decimal(str(value))
    if (currency or "XOF").upper() in ("XOF", "XAF") and value >= 1_000_000:
        millions = (value / Decimal(1_000_000)).normalize()
        text = f"{millions:f}".replace(".", ",")
        return f"{text} million{'s' if millions >= 2 else ''}"
    return money_text(value, currency)


def _nice(value: Decimal) -> int:
    """Une borne ronde : 8 260 000 → 8 500 000 ; 12 300 000 → 15 000 000 ; 960 000 → 1 000 000."""
    value = int(value)
    if value <= 0:
        return 0
    step = 5 * 10 ** max(len(str(value)) - 2, 0)
    return max(step, -(-value // step) * step)


# --- Filtres codés dans les boutons ---------------------------------------------------------------------------------

def encode(filters: dict) -> str:
    return ",".join(f"{k}{v}" for k, v in filters.items() if v not in (None, "")) or "cA"


def decode(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(","):
        if len(part) >= 2 and part[0] in "clhbfg":
            value = part[1:]
            if part[0] in "lh":
                if not value.isdigit():
                    continue
                value = int(value)
            out[part[0]] = value
    return out


async def _vehicles(db, tenant) -> list:
    from app.services.guided_flow import _products

    return await _products(db, tenant)


def _keep(product, filters: dict) -> bool:
    data = product.vehicle or {}
    condition = filters.get("c", "A")
    state = vehicle_service.condition_of(data)
    if condition == "N" and state == "OCCASION":
        return False
    if condition == "O" and state == "NEUF":
        return False
    price = _price(product)
    if "l" in filters and price < filters["l"]:
        return False
    if "h" in filters and price >= filters["h"]:
        return False
    for short, key, _ in REFINE:
        if short in filters and data.get(key) != filters[short]:
            return False
    return True


def _name(product) -> str:
    year = (product.vehicle or {}).get("year")
    return f"{product.name} ({year})" if year and str(year) not in product.name else product.name


async def _profile(db, tenant, customer, **values):
    from app.services import prospect

    if customer is not None:
        await prospect.update_profile(db, tenant.id, customer.id, values)


# --- Écrans ---------------------------------------------------------------------------------------------------------

async def welcome(db, tenant, tu):
    rows = [(f"{PREFIX}veh", "Voir les véhicules", None), (f"{PREFIX}e:-", "Réserver un essai", None),
            (f"{PREFIX}tr", "Faire estimer ma voiture", _you(tu, "Reprise de votre véhicule", "Reprise de ton véhicule")),
            (f"{PREFIX}fi", "Financement", None), (f"{PREFIX}human", "Parler à un conseiller", None)]
    body = (f"Bonjour et bienvenue chez *{tenant.name}* ! Je suis Bob, l'assistant de la concession. "
            + _you(tu, "Que souhaitez-vous faire ?", "Que veux-tu faire ?"))
    return _turn([{"type": "list", "body": body, "button": "Choisir", "rows": rows}])


async def condition_question(db, tenant, tu, customer):
    vehicles = await _vehicles(db, tenant)
    states = {vehicle_service.condition_of(p.vehicle) for p in vehicles}
    if not vehicles:
        return _turn([_buttons(_you(tu, "Notre stock est en cours de mise à jour. Un conseiller peut vous renseigner.",
                                    "Notre stock est en cours de mise à jour. Un conseiller peut te renseigner."),
                               [HUMAN, (f"{PREFIX}menu", "Menu")])])
    if not ({"NEUF", "OCCASION"} <= states):  # un seul des deux (ou rien de connu) : question sautée
        return await budget_or_list(db, tenant, tu, customer, {"c": "A"})
    return _turn([_buttons("Neuf ou occasion ?", [(f"{PREFIX}vc:N", "Neuf"), (f"{PREFIX}vc:O", "Occasion"),
                                                   (f"{PREFIX}vc:A", "Peu importe")])])


async def budget_or_list(db, tenant, tu, customer, filters):
    vehicles = [p for p in await _vehicles(db, tenant) if _keep(p, filters)]
    if len(vehicles) <= PAGE:
        return await listing(db, tenant, tu, filters, 0)
    prices = sorted(_price(p) for p in vehicles)
    n = len(prices)
    low, high = _nice(prices[n // 3]), _nice(prices[(2 * n) // 3])
    bounds = [(None, low), (low, high), (high, None)] if high > low else [(None, low), (low, None)]
    currency = vehicles[0].currency or tenant.currency or "XOF"
    rows = []
    for lo, hi in bounds:
        count = sum(1 for p in vehicles if (lo is None or _price(p) >= lo) and (hi is None or _price(p) < hi))
        if not count:
            continue
        label = _range_label(lo, hi, currency)
        rows.append((f"{PREFIX}vl:{encode({**filters, 'l': lo, 'h': hi})}:0", label[:24],
                     f"{count} véhicule{'s' if count > 1 else ''}"))
    rows.append((f"{PREFIX}vl:{encode(filters)}:0", "Tous les budgets", f"{n} véhicules"))
    return _turn([{"type": "list", "body": "Quel budget environ ?", "button": "Choisir un budget", "rows": rows}])


def _range_label(lo, hi, currency) -> str:
    if lo is None:
        return f"Moins de {amount_label(hi, currency)}"
    if hi is None:
        return f"Plus de {amount_label(lo, currency)}"
    return f"{amount_label(lo, currency).split(' ')[0]} à {amount_label(hi, currency)}"


async def listing(db, tenant, tu, filters, number):
    from app.agents.tools import has_sendable_photo
    from app.services.product_cards import card_body, money_text

    vehicles = [p for p in await _vehicles(db, tenant) if _keep(p, filters)]
    if not vehicles:
        return _turn([_buttons(_you(tu, "Aucun véhicule ne correspond pour l'instant.", "Aucun véhicule ne correspond pour l'instant."),
                               [(f"{PREFIX}vl:cA:0", "Voir tous"), HUMAN])])
    start = max(0, number) * PAGE
    chunk = vehicles[start:start + PAGE]
    if not chunk:
        return await listing(db, tenant, tu, filters, 0)
    actions = []
    with_photo = [p for p in chunk if has_sendable_photo(p)]
    without = [p for p in chunk if not has_sendable_photo(p)]
    if len(with_photo) == 1 and not without and len(vehicles) == 1:
        return await details(db, tenant, tu, with_photo[0])
    if len(with_photo) >= 2:
        actions.append({"type": "carousel", "body": _you(tu, "Voici nos véhicules. Touchez « Réserver un essai » sous celui qui vous plaît.",
                                                         "Voici nos véhicules. Touche « Réserver un essai » sous celui qui te plaît."),
                        "cards": [{"link": p.image_url.strip(), "body": card_body(p, with_price=True), "product_id": str(p.id),
                                   "name": p.name, "buttons": [(f"{PREFIX}d:{p.id}", "Voir détails"),
                                                               (f"{PREFIX}e:{p.id}", "Réserver un essai")]}
                                  for p in with_photo]})
    else:
        without = with_photo + without
    if without:
        rows = [(f"{PREFIX}d:{p.id}", _name(p)[:24], money_text(p.price, p.currency)) for p in without[:10]]
        actions.append({"type": "list", "body": "Voici nos véhicules." if not actions else "Et ceux-ci, sans photo pour l'instant :",
                        "button": "Voir les véhicules", "rows": rows})
    more = len(vehicles) > start + PAGE
    nav = [(f"{PREFIX}vl:{encode(filters)}:{number + 1}", "Voir plus")] if more else []
    if len(vehicles) > PAGE and _refine_rows(vehicles, filters):
        nav.append((f"{PREFIX}af:{encode(filters)}", "Affiner"))
    nav.append(HUMAN)
    left = len(vehicles) - start - len(chunk)
    actions.append(_buttons(f"Il reste {left} véhicule{'s' if left > 1 else ''}." if more else
                            _you(tu, "Vous pouvez aussi m'écrire ce que vous cherchez.", "Tu peux aussi m'écrire ce que tu cherches."),
                            nav))
    return _turn(actions)


def _refine_rows(vehicles, filters) -> list:
    rows = []
    for short, key, labels in REFINE:
        if short in filters:
            continue
        values = sorted({(p.vehicle or {}).get(key) for p in vehicles} - {None})
        if len(values) < 2:
            continue
        for value in values:
            count = sum(1 for p in vehicles if (p.vehicle or {}).get(key) == value)
            rows.append((f"{PREFIX}vl:{encode({**filters, short: value})}:0", labels.get(value, value)[:24],
                         f"{count} véhicule{'s' if count > 1 else ''}"))
    return rows[:10]


async def refine(db, tenant, tu, filters):
    vehicles = [p for p in await _vehicles(db, tenant) if _keep(p, filters)]
    rows = _refine_rows(vehicles, filters)
    if not rows:
        return await listing(db, tenant, tu, filters, 0)
    return _turn([{"type": "list", "body": _you(tu, "Affinez par carrosserie, carburant ou boîte :",
                                                "Affine par carrosserie, carburant ou boîte :"),
                   "button": "Affiner", "rows": rows}])


async def details(db, tenant, tu, product):
    from app.agents.tools import has_sendable_photo
    from app.services.product_cards import price_line

    lines = [f"*{_name(product)}*"]
    summary = vehicle_service.summary(product.vehicle)
    if summary:
        lines.append(summary)
    price = price_line(product)
    if price:
        lines.append(price)
    if product.description:
        text = " ".join(product.description.split())
        lines.append(text if len(text) <= 600 else text[:599].rstrip() + "…")
    image = product.image_url.strip() if has_sendable_photo(product) else None
    return _turn([_buttons("\n".join(lines), [(f"{PREFIX}e:{product.id}", "Réserver un essai"),
                                             (f"{PREFIX}veh", "Autres véhicules"), HUMAN],
                           image=image, product_id=str(product.id), name=product.name)])


async def which_vehicle(db, tenant, tu):
    return _turn([_buttons(_you(tu, "Quel véhicule souhaitez-vous essayer ?", "Quel véhicule veux-tu essayer ?"),
                           [(f"{PREFIX}veh", "Choisir un véhicule"), (f"{PREFIX}e:x", "Pas encore choisi")])])


async def slots(db, tenant, tu, customer, kind: str, product=None, note: str | None = None):
    """Créneaux réellement libres (agenda de la concession) + « Autre moment » ; sinon la disponibilité est demandée."""
    from datetime import datetime, timezone

    from app.services import booking
    from app.services.local_time import format_local, tenant_zone

    settings = await booking.load_settings(db, tenant.id)
    zone = tenant_zone(tenant)
    ref = str(product.id) if product is not None else "-"
    what = _what(tu, kind, product)
    free = await booking.free_slots(db, tenant, settings, zone, datetime.now(timezone.utc)) if booking.booking_enabled(settings) else []
    if not free:
        return await ask_time(db, tenant, tu, customer, kind, ref, f"{note + chr(10) if note else ''}{what} ")
    rows = [(f"{PREFIX}sl:{kind}:{ref}:{s.strftime('%Y%m%d%H%M')}", format_local(s.astimezone(timezone.utc), zone)[:24], "Libre")
            for s in free]
    rows.append((f"{PREFIX}sl:{kind}:{ref}:x", "Autre moment", _you(tu, "Écrivez-moi le jour et l'heure", "Écris-moi le jour et l'heure")))
    body = (f"{note}\n" if note else "") + what + " " + _you(tu, "Quand êtes-vous disponible ?", "Quand es-tu disponible ?")
    return _turn([{"type": "list", "body": body, "button": "Choisir un créneau", "rows": rows}])


def _what(tu, kind, product) -> str:
    if kind == "E":
        return f"Essai du *{_name(product)}*." if product is not None else "Essai d'un véhicule."
    if kind == "V":  # lot 97 : visite d'un bien
        return f"Visite : *{product.name}*." if product is not None else "Visite d'un bien."
    if kind == "A":  # lot 98 : le cabinet rappelle le client
        return _you(tu, "Un conseiller du cabinet vous appelle.", "Un conseiller du cabinet t'appelle.")
    if kind == "S":  # lot 97 : un conseiller vient voir le bien du propriétaire
        return _you(tu, "Pour une estimation juste, un conseiller vient voir votre bien.",
                    "Pour une estimation juste, un conseiller vient voir ton bien.")
    return _you(tu, "Estimation de votre voiture.", "Estimation de ta voiture.")


async def ask_time(db, tenant, tu, customer, kind, ref, lead=""):
    from app.services import shop_cart

    cart = await shop_cart.get_cart(db, tenant, customer, create=True)
    shop_cart._touch(cart, awaiting=f"auto:avail:{kind}:{ref}")
    return _turn([{"type": "text", "body": lead + _you(tu, "Quel jour et à quel moment êtes-vous disponible ? (par exemple : samedi matin)",
                                                       "Quel jour et à quel moment es-tu disponible ? (par exemple : samedi matin)")}])


async def book(db, tenant, tu, customer, conversation, kind: str, product, slot: str | None = None,
               availability: str | None = None, notes: str | None = None):
    """Le même outil que Bob : créneau revérifié et confirmé, ou demande transmise au conseiller."""
    from app.agents.tools import ToolExecutor
    from app.services import appointment_service, prospect, shop_cart
    from app.services.local_time import tenant_zone

    tools = ToolExecutor(db, tenant.id, conversation, customer.id, business_type=tenant.business_type)
    tools.client_chose_other_time = availability is not None
    request = {"kind": KINDS[kind]}
    if product is not None:
        request["product_id"] = str(product.id)
    if slot:
        request["slot"] = slot
    if availability:
        request["availability"] = availability
    if notes:
        request["notes"] = notes[:300]
    if kind == "S" and not notes:  # lot 97 : le bien décrit par le propriétaire accompagne la demande
        request["notes"] = await _owner_notes(db, tenant, customer)
    if kind == "R":
        profile = await prospect.load(db, tenant.id, customer.id)
        if profile is not None and profile.trade_in:
            request["trade_in"] = profile.trade_in
    result = await tools._tool_request_appointment(request)
    cart = await shop_cart.get_cart(db, tenant, customer)
    if result.get("status") == "appointment_confirmed" and tools.booking_outbox:
        if cart is not None:
            shop_cart._touch(cart, awaiting=None)
        appointment = tools.booking_outbox[-1]
        text = appointment_service.confirmation_message(appointment, tenant.name, tenant_zone(tenant))
        return _turn([{"type": "text", "body": text, "message_type": "appointment_confirmed"}, after(tu, kind)],
                     booked=[appointment])
    if result.get("status") == "appointment_requested":
        if cart is not None:
            shop_cart._touch(cart, awaiting=None)
        what = {"E": f"Demande d'essai envoyée{f' : {_name(product)}' if product is not None else ''}",
                "V": f"Demande de visite envoyée{f' : {product.name}' if product is not None else ''}",
                "A": "Demande de rappel envoyée"}.get(
            kind, "Demande d'estimation envoyée")
        return _turn(handoff=True, handoff_done=True,
                     handoff_text=f"{what}, {availability}. " + _you(tu, "Un conseiller vous confirme l'heure ici.",
                                                                    "Un conseiller te confirme l'heure ici."))
    if result.get("available_slots") is not None or result.get("slots") is not None:
        return await slots(db, tenant, tu, customer, kind, product,
                           note=_you(tu, "Ce créneau vient d'être pris.", "Ce créneau vient d'être pris."))
    if "Le jour et la date" in (result.get("error") or ""):  # lot 44 : « samedi 3 » alors que le 3 est un lundi
        question = (result.get("instruction") or "").split("question : ", 1)[-1]
        return _turn([{"type": "text", "body": question or result["error"]}])
    return _turn([_buttons(result.get("error") or "Je n'ai pas pu enregistrer la demande.", [HUMAN, (f"{PREFIX}menu", "Menu")])])


async def _owner_notes(db, tenant, customer) -> str | None:
    from app.services import immo_flow

    return await immo_flow.owner_summary(db, tenant, customer)


def after(tu, kind) -> dict:
    """« Autre chose ? » après un rendez-vous confirmé."""
    if kind == "A":  # lot 98 : courtier
        return _buttons("Autre chose ?", [(f"{PREFIX}menu", "Autre demande"), HUMAN])
    if kind in ("V", "S"):  # lot 97 : agence
        return _buttons("Autre chose ?", [(f"{PREFIX}ib", "Voir d'autres biens"), HUMAN])
    if kind == "R":
        buttons = [(f"{PREFIX}veh", "Voir les véhicules"), (f"{PREFIX}e:-", "Réserver un essai"), (f"{PREFIX}fi", "Financement")]
    else:
        buttons = [(f"{PREFIX}tr", "Estimer ma voiture"), (f"{PREFIX}fi", "Financement"),
                   (f"{PREFIX}veh", "Autres véhicules")]
    return _buttons("Autre chose ?", buttons)


async def trade_in_start(db, tenant, tu, customer):
    from app.services import shop_cart

    cart = await shop_cart.get_cart(db, tenant, customer, create=True)
    shop_cart._touch(cart, awaiting="auto:model")
    return _turn([{"type": "text", "body": _you(
        tu, "Quelle voiture souhaitez-vous faire estimer ? Écrivez la marque et le modèle (par exemple : Toyota Corolla).",
        "Quelle voiture veux-tu faire estimer ? Écris la marque et le modèle (par exemple : Toyota Corolla).")}])


async def trade_in_year(db, tenant, tu):
    rows = [(f"{PREFIX}ty:{i}", label, None) for i, label in enumerate(YEARS)] + [(f"{PREFIX}ty:x", "Je ne sais pas", None)]
    return _turn([{"type": "list", "body": "Question 2 sur 3 : " + _you(tu, "de quelle année est-elle ?", "de quelle année est-elle ?"),
                   "button": "Choisir l'année", "rows": rows}])


async def trade_in_mileage(db, tenant, tu):
    rows = [(f"{PREFIX}tk:{i}", label, None) for i, label in enumerate(MILEAGES)] + [(f"{PREFIX}tk:x", "Je ne sais pas", None)]
    return _turn([{"type": "list", "body": "Question 3 sur 3 : combien de kilomètres au compteur ?",
                   "button": "Choisir", "rows": rows}])


async def _trade_in_add(db, tenant, customer, position: int, value: str | None):
    from app.services import prospect

    profile = await prospect.load(db, tenant.id, customer.id)
    parts = (profile.trade_in.split(SEP) if profile is not None and profile.trade_in else [])[:position]
    if value:
        parts.append(value)
    if parts:
        await _profile(db, tenant, customer, trade_in=SEP.join(parts)[:300])


async def financing(db, tenant, tu, customer):
    await _profile(db, tenant, customer, payment="FINANCEMENT")
    body = _you(tu, "Un conseiller vous présente les solutions de financement lors de votre essai, ou vous rappelle avant "
                    "si vous le souhaitez.",
                "Un conseiller te présente les solutions de financement lors de ton essai, ou te rappelle avant si tu le souhaites.")
    return _turn([_buttons(body, [(f"{PREFIX}cb", "Être rappelé"), (f"{PREFIX}e:-", "Réserver un essai"),
                                  (f"{PREFIX}veh", "Voir les véhicules")])])


# --- Aiguillage -----------------------------------------------------------------------------------------------------

async def _vehicle(db, tenant, raw):
    from app.models.product import Product

    product = await db.get(Product, _uuid(raw)) if _uuid(raw) else None
    return product if product is not None and product.tenant_id == tenant.id and product.active else None


async def handle(db, tenant, text, reply_id, referral=None, tu=False, customer=None, conversation=None):
    from app.services.guided_flow import wants_welcome

    if reply_id and reply_id.startswith(PREFIX):
        parts = reply_id[len(PREFIX):].split(":")
        turn = await _tap(db, tenant, tu, customer, conversation, parts[0], parts)
        return turn if turn is not None else await welcome(db, tenant, tu)
    if not reply_id and wants_welcome(text, referral):
        return await welcome(db, tenant, tu)
    if not reply_id and customer is not None:
        return await handle_text(db, tenant, tu, customer, conversation, text)
    return None


async def _tap(db, tenant, tu, customer, conversation, action, parts):
    if action == "menu":
        return await welcome(db, tenant, tu)
    if action == "human":
        return _turn(handoff=True, handoff_text=_you(tu, "Je préviens un conseiller : il vous répond ici dès que possible.",
                                                     "Je préviens un conseiller : il te répond ici dès que possible."))
    if action == "veh":
        return await condition_question(db, tenant, tu, customer)
    if action == "vc" and len(parts) == 2 and parts[1] in CONDITION_PROFILE:
        await _profile(db, tenant, customer, condition=CONDITION_PROFILE[parts[1]])
        return await budget_or_list(db, tenant, tu, customer, {"c": parts[1]})
    if action == "vl" and len(parts) == 3 and parts[2].isdigit():
        filters = decode(parts[1])
        if int(parts[2]) == 0 and ("l" in filters or "h" in filters) and not set(filters) & {"b", "f", "g"}:
            vehicles = await _vehicles(db, tenant)
            currency = (vehicles[0].currency if vehicles else None) or tenant.currency or "XOF"
            await _profile(db, tenant, customer, budget=_range_label(filters.get("l"), filters.get("h"), currency))
        return await listing(db, tenant, tu, filters, int(parts[2]))
    if action == "af" and len(parts) == 2:
        return await refine(db, tenant, tu, decode(parts[1]))
    if action == "d" and len(parts) == 2:
        product = await _vehicle(db, tenant, parts[1])
        return await details(db, tenant, tu, product) if product is not None else await condition_question(db, tenant, tu, customer)
    if customer is None or conversation is None:
        return None
    if action == "e" and len(parts) == 2:
        if parts[1] == "-":
            return await which_vehicle(db, tenant, tu)
        product = None if parts[1] == "x" else await _vehicle(db, tenant, parts[1])
        if parts[1] != "x" and product is None:
            return await condition_question(db, tenant, tu, customer)
        return await slots(db, tenant, tu, customer, "E", product)
    if action == "sl" and len(parts) == 4 and parts[1] in KINDS:
        product = await _vehicle(db, tenant, parts[2]) if parts[2] != "-" else None
        if parts[3] == "x":
            return await ask_time(db, tenant, tu, customer, parts[1], parts[2])
        if re.fullmatch(r"\d{12}", parts[3]):
            raw = parts[3]
            return await book(db, tenant, tu, customer, conversation, parts[1], product,
                              slot=f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}")
        return None
    if action == "tr":
        return await trade_in_start(db, tenant, tu, customer)
    if action == "ty" and len(parts) == 2:
        if parts[1].isdigit() and int(parts[1]) < len(YEARS):
            await _trade_in_add(db, tenant, customer, 1, YEARS[int(parts[1])])
        return await trade_in_mileage(db, tenant, tu)
    if action == "tk" and len(parts) == 2:
        if parts[1].isdigit() and int(parts[1]) < len(MILEAGES):
            await _trade_in_add(db, tenant, customer, 2, MILEAGES[int(parts[1])])
        return await slots(db, tenant, tu, customer, "R")
    if action == "fi":
        return await financing(db, tenant, tu, customer)
    if action == "cb":
        return _turn(handoff=True, handoff_reason="Le client souhaite être rappelé pour un financement",
                     handoff_text=_you(tu, "C'est noté : un conseiller vous rappelle sur ce numéro dès que possible.",
                                       "C'est noté : un conseiller te rappelle sur ce numéro dès que possible."))
    return None


def _plausible(text: str | None) -> bool:
    words = (text or "").strip()
    return 2 <= len(words) <= 300 and "?" not in words


async def expects_text(db, tenant, customer, text) -> bool:
    from app.services import shop_cart

    wanted = shop_cart.awaiting(await shop_cart.get_cart(db, tenant, customer))
    return bool(wanted and wanted.startswith(("auto:", "immo:")) and _plausible(text))


async def handle_text(db, tenant, tu, customer, conversation, text):
    from app.services import shop_cart

    cart = await shop_cart.get_cart(db, tenant, customer)
    wanted = shop_cart.awaiting(cart)
    if not wanted or not wanted.startswith("auto:"):
        return None
    if not _plausible(text):
        shop_cart._touch(cart, awaiting=None)  # le client parle d'autre chose : Bob ne l'attend plus
        return None
    clean = " ".join(text.split())
    if wanted == "auto:model":
        shop_cart._touch(cart, awaiting=None)
        await _profile(db, tenant, customer, trade_in=clean[:120])
        return await trade_in_year(db, tenant, tu)
    match = re.fullmatch(r"auto:avail:([ERVSA]):(.+)", wanted)
    if match and conversation is not None:
        product = await _vehicle(db, tenant, match.group(2)) if match.group(2) != "-" else None
        return await book(db, tenant, tu, customer, conversation, match.group(1), product, availability=clean[:300])
    shop_cart._touch(cart, awaiting=None)
    return None
