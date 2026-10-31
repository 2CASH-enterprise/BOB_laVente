"""
Lot 97 — parcours à boutons des agences immobilières (écrans « Immo » de la maquette), validé le 31/10.

Le but : une VISITE (acheteur, locataire) ou un rendez-vous d'ESTIMATION (propriétaire qui vend ou met en location),
enregistrés par le même outil que Bob (request_appointment) via auto_flow.book. Le projet du client est noté dans sa
fiche (real_estate.update_project). Aucun appel d'IA ; JAMAIS de chiffre d'estimation ; les garde-fous du lot 79
restent (aucune pièce demandée, aucune question discriminante, aucun chiffre de prêt).

Décisions du 31/10 :
- accueil en liste : Acheter, Louer, Vendre ou faire estimer, Mettre en location, Parler à un conseiller — selon les
  activités déclarées par l'agence ;
- prix sur les cartes SEULEMENT avec les honoraires de la fiche, toujours ensemble (loyer / mois, charges,
  honoraires) ; sans honoraires : « Prix et honoraires : demandez au conseiller » (alerte sur la fiche du bien) ;
- type de bien (sauté s'il n'y en a qu'un), quartier (tiré des fiches, avec le nombre), budget en tranches (sauté
  jusqu'à 10 biens), cartes « Voir détails » + « Demander une visite ».

Boutons (préfixe « g: ») : g:menu, g:ib, g:pa, g:pl, g:pv, g:pg, g:it:<filtres>, g:iq:<filtres>, g:il:<filtres>:<page>,
g:d:<bien>, g:vi:<bien>, g:ot:<V|G>:<TYPE>, g:sl:<V|S>:<bien|->:<créneau|x>, g:human.
Filtres : « tL,pAPPARTEMENT,q1a2b3c4d,l100000,h300000 » (L location / V vente, type, quartier (empreinte), prix).
"""
import re
import zlib

from app.services import auto_flow
from app.services.auto_flow import HUMAN, PREFIX, PAGE, _buttons, _price, _turn, _you, amount_label

TYPE_LABELS = {"APPARTEMENT": "Appartement", "MAISON": "Maison ou villa", "LOCAL": "Bureau ou local",
               "TERRAIN": "Terrain", "PARKING": "Parking ou box", "AUTRE": "Autre bien"}
TYPE_PLURALS = {"APPARTEMENT": ("appartement", "appartements"), "MAISON": ("maison", "maisons"),
                "LOCAL": ("local", "locaux"), "TERRAIN": ("terrain", "terrains"), "PARKING": ("parking", "parkings"),
                "AUTRE": ("bien", "biens")}
OWNER_TYPES = ("APPARTEMENT", "MAISON", "TERRAIN", "LOCAL")
TX = {"L": "Location", "V": "Vente"}
PROJECT_OF_TX = {"L": "LOCATION", "V": "ACHAT"}
OWNER_PROJECTS = {"V": "VENTE", "G": "GESTION"}
CARD_LIMIT = 160


def _plain(text) -> str:
    from app.services.real_estate import _plain as plain

    return plain(text)


def _place(product) -> str | None:
    sheet = product.sheet or {}
    value = sheet.get("district") or sheet.get("city")
    return " ".join(str(value).split()) if value else None


def _place_key(place: str | None) -> str:
    return format(zlib.crc32(_plain(place or "").encode()), "08x")


def _count(n: int, code: str | None, tx: str | None) -> str:
    one, many = TYPE_PLURALS.get(code or "AUTRE", ("bien", "biens"))
    suffix = {"L": " à louer", "V": " à vendre"}.get(tx or "", "")
    return f"{n} {one if n == 1 else many}{suffix}"


# --- Filtres codés dans les boutons ---------------------------------------------------------------------------------

def encode(filters: dict) -> str:
    return ",".join(f"{k}{v}" for k, v in filters.items() if v not in (None, ""))


def decode(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(","):
        if len(part) < 2 or part[0] not in "tpqlh":
            continue
        value = part[1:]
        if part[0] in "lh":
            if not value.isdigit():
                continue
            value = int(value)
        out[part[0]] = value
    return out


async def _properties(db, tenant) -> list:
    from app.services.guided_flow import _products
    from app.services.product_sheets import is_property

    return [p for p in await _products(db, tenant) if is_property(p.bob_category)]


def _keep(product, filters: dict, skip: str = "") -> bool:
    sheet = product.sheet or {}
    if "t" in filters and "t" not in skip and sheet.get("transaction") and _plain(sheet["transaction"]) != _plain(TX.get(filters["t"])):
        return False
    if "p" in filters and "p" not in skip and product.bob_category != filters["p"]:
        return False
    if "q" in filters and "q" not in skip and _place_key(_place(product)) != filters["q"]:
        return False
    price = _price(product)
    if "l" in filters and "l" not in skip and price < filters["l"]:
        return False
    if "h" in filters and "h" not in skip and price >= filters["h"]:
        return False
    return True


async def _project(db, tenant, customer, **values):
    from app.services import real_estate

    if customer is not None:
        await real_estate.update_project(db, tenant.id, customer.id, values)


# --- Prix, charges, honoraires : toujours ensemble ------------------------------------------------------------------

def price_lines(product) -> list[str]:
    """« 350 000 XOF / mois, charges 25 000 XOF » + « Honoraires : … » ; sans honoraires dans la fiche : aucun prix."""
    from app.services.product_cards import money_text
    from app.services.product_sheets import to_number

    sheet = product.sheet or {}
    fees = " ".join(str(sheet.get("fees") or "").split())
    if not fees or _price(product) <= 0:
        return ["Prix et honoraires : demandez au conseiller"]
    rent = _plain(sheet.get("transaction")) == _plain("Location")
    line = f"*{money_text(product.price, product.currency)}*" + (" / mois" if rent else "")
    charges = " ".join(str(sheet.get("charges") or "").split())
    if charges:
        figure = to_number(charges)
        line += f", charges {money_text(figure, product.currency) if figure is not None and re.fullmatch(r'[\d\s.,]+', charges) else charges}"
    return [line, f"Honoraires : {fees}"]


def _detail(product) -> str | None:
    sheet = product.sheet or {}
    parts = []
    surface = sheet.get("surface") or sheet.get("land_surface")
    if surface:
        parts.append(f"{surface} m²")
    if sheet.get("bedrooms"):
        parts.append(f"{sheet['bedrooms']} chambre{'s' if str(sheet['bedrooms']).strip() not in ('1', '0') else ''}")
    elif sheet.get("rooms"):
        parts.append(f"{sheet['rooms']} pièces")
    if sheet.get("floor"):
        parts.append(f"{sheet['floor']} étage" if str(sheet["floor"]).strip()[-1:].isalpha() else f"étage {sheet['floor']}")
    if _place(product):
        parts.append(_place(product))
    return " · ".join(str(p) for p in parts) or None


def card_body(product) -> str:
    """Nom, détail, prix + honoraires (160 caractères au plus : le détail saute avant le prix, jamais les honoraires)."""
    detail = _detail(product)
    prices = price_lines(product)
    for lines in ([product.name, detail, *prices], [product.name, *prices]):
        text = "\n".join(line for line in lines if line)
        if len(text) <= CARD_LIMIT:
            return text
    return "\n".join([product.name[:100], "Prix et honoraires : voir détails"])


# --- Écrans ---------------------------------------------------------------------------------------------------------

async def welcome(db, tenant, tu):
    from app.services.real_estate import activities_of

    activities = activities_of(tenant)
    rows = []
    if "VENTE" in activities:
        rows.append((f"{PREFIX}pa", "Acheter", None))
    if "LOCATION" in activities or "GESTION" in activities:
        rows.append((f"{PREFIX}pl", "Louer", None))
    if "VENTE" in activities:
        rows.append((f"{PREFIX}pv", "Vendre ou faire estimer", None))
    if "GESTION" in activities:
        rows.append((f"{PREFIX}pg", "Mettre en location", _you(tu, "Confier votre bien à l'agence", "Confier ton bien à l'agence")))
    rows.append((f"{PREFIX}human", "Parler à un conseiller", None))
    body = (f"Bonjour et bienvenue à *{tenant.name}* ! Je suis Bob, l'assistant de l'agence. "
            + _you(tu, "Quel est votre projet ?", "Quel est ton projet ?"))
    return _turn([{"type": "list", "body": body, "button": "Choisir", "rows": rows}])


async def search_start(db, tenant, tu, customer, tx: str):
    await _project(db, tenant, customer, project=PROJECT_OF_TX[tx])
    return await type_question(db, tenant, tu, customer, {"t": tx})


async def type_question(db, tenant, tu, customer, filters):
    props = [p for p in await _properties(db, tenant) if _keep(p, filters)]
    if not props:
        word = "à louer" if filters.get("t") == "L" else "à vendre"
        return _turn([_buttons(_you(tu, f"Nous n'avons pas de bien {word} en ce moment. Un conseiller peut noter votre recherche.",
                                    f"Nous n'avons pas de bien {word} en ce moment. Un conseiller peut noter ta recherche."),
                               [HUMAN, (f"{PREFIX}menu", "Menu")])])
    codes = sorted({p.bob_category for p in props}, key=lambda c: list(TYPE_LABELS).index(c) if c in TYPE_LABELS else 99)
    if len(codes) == 1:
        return await place_question(db, tenant, tu, customer, {**filters, "p": codes[0]})
    if len(codes) <= 3:
        return _turn([_buttons("Quel type de bien ?", [(f"{PREFIX}it:{encode({**filters, 'p': c})}", TYPE_LABELS.get(c, c)[:20])
                                                       for c in codes])])
    rows = [(f"{PREFIX}it:{encode({**filters, 'p': c})}", TYPE_LABELS.get(c, c), _count(sum(1 for p in props if p.bob_category == c), c, filters.get("t")))
            for c in codes][:10]
    return _turn([{"type": "list", "body": "Quel type de bien ?", "button": "Choisir", "rows": rows}])


async def place_question(db, tenant, tu, customer, filters):
    if filters.get("p") and filters.get("t"):
        await _project(db, tenant, customer, project=PROJECT_OF_TX[filters["t"]], property_type=filters["p"])
    props = [p for p in await _properties(db, tenant) if _keep(p, filters)]
    places: dict = {}
    for p in props:
        place = _place(p)
        if place:
            key = _place_key(place)
            name, n = places.get(key, (place, 0))
            places[key] = (name, n + 1)
    if len(places) < 2:
        return await budget_or_list(db, tenant, tu, customer, filters)
    ranked = sorted(places.items(), key=lambda kv: (-kv[1][1], kv[1][0]))[:9]
    rows = [(f"{PREFIX}iq:{encode({**filters, 'q': key})}", name[:24], _count(n, filters.get("p"), filters.get("t")))
            for key, (name, n) in ranked]
    rows.append((f"{PREFIX}iq:{encode(filters)}", "Peu importe", _count(len(props), filters.get("p"), filters.get("t"))))
    return _turn([{"type": "list", "body": "Dans quel quartier ?", "button": "Choisir un quartier", "rows": rows}])


async def budget_or_list(db, tenant, tu, customer, filters):
    props = [p for p in await _properties(db, tenant) if _keep(p, filters)]
    if len(props) <= PAGE:
        return await listing(db, tenant, tu, filters, 0)
    prices = sorted(_price(p) for p in props)
    n = len(prices)
    low, high = auto_flow._nice(prices[n // 3]), auto_flow._nice(prices[(2 * n) // 3])
    bounds = [(None, low), (low, high), (high, None)] if high > low else [(None, low), (low, None)]
    currency = props[0].currency or tenant.currency or "XOF"
    rows = []
    for lo, hi in bounds:
        count = sum(1 for p in props if (lo is None or _price(p) >= lo) and (hi is None or _price(p) < hi))
        if count:
            rows.append((f"{PREFIX}il:{encode({**filters, 'l': lo, 'h': hi})}:0", auto_flow._range_label(lo, hi, currency)[:24],
                         _count(count, filters.get("p"), filters.get("t"))))
    rows.append((f"{PREFIX}il:{encode(filters)}:0", "Tous les budgets", _count(n, filters.get("p"), filters.get("t"))))
    body = "Quel budget environ ?" + (" (loyer par mois)" if filters.get("t") == "L" else "")
    return _turn([{"type": "list", "body": body, "button": "Choisir un budget", "rows": rows}])


async def listing(db, tenant, tu, filters, number):
    from app.agents.tools import has_sendable_photo
    from app.services.product_cards import money_text

    props = [p for p in await _properties(db, tenant) if _keep(p, filters)]
    if not props:
        return _turn([_buttons(_you(tu, "Aucun bien ne correspond pour l'instant. Un conseiller peut noter votre recherche.",
                                    "Aucun bien ne correspond pour l'instant. Un conseiller peut noter ta recherche."),
                               [(f"{PREFIX}ib", "Voir d'autres biens"), HUMAN])])
    start = max(0, number) * PAGE
    chunk = props[start:start + PAGE]
    if not chunk:
        return await listing(db, tenant, tu, filters, 0)
    if len(props) == 1:
        return await details(db, tenant, tu, props[0])
    actions = []
    with_photo = [p for p in chunk if has_sendable_photo(p)]
    without = [p for p in chunk if not has_sendable_photo(p)]
    if len(with_photo) >= 2:
        actions.append({"type": "carousel", "body": _you(tu, "Voici nos biens. Touchez « Demander une visite » sous celui qui vous plaît.",
                                                         "Voici nos biens. Touche « Demander une visite » sous celui qui te plaît."),
                        "cards": [{"link": p.image_url.strip(), "body": card_body(p), "product_id": str(p.id), "name": p.name,
                                   "buttons": [(f"{PREFIX}d:{p.id}", "Voir détails"), (f"{PREFIX}vi:{p.id}", "Demander une visite")]}
                                  for p in with_photo]})
    else:
        without = with_photo + without
    if without:
        rows = [(f"{PREFIX}d:{p.id}", p.name[:24], (money_text(p.price, p.currency) if (p.sheet or {}).get("fees")
                                                    else "Prix sur demande")[:72]) for p in without[:10]]
        actions.append({"type": "list", "body": "Voici nos biens." if not actions else "Et ceux-ci, sans photo pour l'instant :",
                        "button": "Voir les biens", "rows": rows})
    more = len(props) > start + PAGE
    nav = [(f"{PREFIX}il:{encode(filters)}:{number + 1}", "Voir plus")] if more else []
    nav += [(f"{PREFIX}ib", "Autre recherche"), HUMAN]
    left = len(props) - start - len(chunk)
    actions.append(_buttons(f"Il reste {left} bien{'s' if left > 1 else ''}." if more else
                            _you(tu, "Vous pouvez aussi m'écrire ce que vous cherchez.", "Tu peux aussi m'écrire ce que tu cherches."),
                            nav))
    return _turn(actions)


async def details(db, tenant, tu, product):
    from app.agents.tools import has_sendable_photo

    sheet = product.sheet or {}
    lines = [f"*{product.name}*"]
    if _detail(product):
        lines.append(_detail(product))
    lines += price_lines(product)
    if sheet.get("dpe"):
        lines.append(f"DPE {sheet['dpe']}" if len(str(sheet["dpe"])) == 1 else str(sheet["dpe"]))
    if product.description:
        text = " ".join(product.description.split())
        lines.append(text if len(text) <= 500 else text[:499].rstrip() + "…")
    image = product.image_url.strip() if has_sendable_photo(product) else None
    return _turn([_buttons("\n".join(lines), [(f"{PREFIX}vi:{product.id}", "Demander une visite"),
                                             (f"{PREFIX}ib", "Voir d'autres biens"), HUMAN],
                           image=image, product_id=str(product.id), name=product.name)])


async def owner_start(db, tenant, tu, customer, which: str):
    await _project(db, tenant, customer, project=OWNER_PROJECTS[which])
    body = _you(tu, "Avec plaisir. Quel type de bien ?", "Avec plaisir. Quel type de bien ?")
    rows = [(f"{PREFIX}ot:{which}:{code}", TYPE_LABELS[code], None) for code in OWNER_TYPES]
    return _turn([{"type": "list", "body": body, "button": "Choisir", "rows": rows}])


async def owner_type(db, tenant, tu, customer, which: str, code: str):
    from app.services import shop_cart

    await _project(db, tenant, customer, project=OWNER_PROJECTS[which], property_type=code)
    cart = await shop_cart.get_cart(db, tenant, customer, create=True)
    shop_cart._touch(cart, awaiting=f"immo:place:{which}")
    land = " (et celle du terrain)" if code == "MAISON" else ""
    return _turn([{"type": "text", "body": _you(
        tu, f"Dans quel quartier, et quelle surface environ{land} ? (par exemple : Bingerville, 180 m²)",
        f"Dans quel quartier, et quelle surface environ{land} ? (par exemple : Bingerville, 180 m²)")}])


async def owner_summary(db, tenant, customer) -> str | None:
    """« Vente · Maison · Bingerville, 180 m² » : le bien du propriétaire, joint à la demande d'estimation."""
    from app.services import prospect
    from app.services.real_estate import PROJECTS, PROPERTY_TYPES

    profile = await prospect.load(db, tenant.id, customer.id)
    projects = ((profile.real_estate or {}).get("projects") or {}) if profile is not None else {}
    owned = [(key, projects[key]) for key in ("VENTE", "GESTION") if key in projects]
    if not owned:
        return None
    key, data = max(owned, key=lambda kv: kv[1].get("updated_at") or "")
    parts = [PROJECTS[key], PROPERTY_TYPES.get(data.get("property_type"), None), data.get("address")]
    return " · ".join(p for p in parts if p)[:300]


# --- Aiguillage -----------------------------------------------------------------------------------------------------

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
    from app.services.real_estate import activities_of

    if action in ("menu", "ib"):
        return await welcome(db, tenant, tu)
    if action == "human":
        return await auto_flow._tap(db, tenant, tu, customer, conversation, "human", parts)
    if action in ("pa", "pl"):
        return await search_start(db, tenant, tu, customer, "V" if action == "pa" else "L")
    if action == "pv" and "VENTE" in activities_of(tenant):
        return await owner_start(db, tenant, tu, customer, "V")
    if action == "pg" and "GESTION" in activities_of(tenant):
        return await owner_start(db, tenant, tu, customer, "G")
    if action == "it" and len(parts) == 2:
        return await place_question(db, tenant, tu, customer, decode(parts[1]))
    if action == "iq" and len(parts) == 2:
        filters = decode(parts[1])
        if filters.get("q") and filters.get("t"):
            props = [p for p in await _properties(db, tenant) if _keep(p, filters)]
            if props:
                await _project(db, tenant, customer, project=PROJECT_OF_TX[filters["t"]], zone=_place(props[0]))
        return await budget_or_list(db, tenant, tu, customer, filters)
    if action == "il" and len(parts) == 3 and parts[2].isdigit():
        filters = decode(parts[1])
        if int(parts[2]) == 0 and ("l" in filters or "h" in filters) and filters.get("t"):
            props = await _properties(db, tenant)
            currency = (props[0].currency if props else None) or tenant.currency or "XOF"
            await _project(db, tenant, customer, project=PROJECT_OF_TX[filters["t"]],
                           budget=auto_flow._range_label(filters.get("l"), filters.get("h"), currency)
                           + (" par mois" if filters["t"] == "L" else ""))
        return await listing(db, tenant, tu, filters, int(parts[2]))
    if action == "d" and len(parts) == 2:
        product = await auto_flow._vehicle(db, tenant, parts[1])
        return await details(db, tenant, tu, product) if product is not None else await welcome(db, tenant, tu)
    if customer is None or conversation is None:
        return None
    if action == "vi" and len(parts) == 2:
        product = await auto_flow._vehicle(db, tenant, parts[1])
        return await auto_flow.slots(db, tenant, tu, customer, "V", product) if product is not None else await welcome(db, tenant, tu)
    if action == "ot" and len(parts) == 3 and parts[1] in OWNER_PROJECTS and parts[2] in OWNER_TYPES:
        return await owner_type(db, tenant, tu, customer, parts[1], parts[2])
    if action == "sl" and len(parts) == 4 and parts[1] in ("V", "S"):
        return await auto_flow._tap(db, tenant, tu, customer, conversation, "sl", parts)
    return None


async def handle_text(db, tenant, tu, customer, conversation, text):
    from app.services import shop_cart

    cart = await shop_cart.get_cart(db, tenant, customer)
    wanted = shop_cart.awaiting(cart)
    if wanted and wanted.startswith("auto:"):
        return await auto_flow.handle_text(db, tenant, tu, customer, conversation, text)
    match = re.fullmatch(r"immo:place:([VG])", wanted or "")
    if not match:
        if wanted:
            shop_cart._touch(cart, awaiting=None)
        return None
    if not auto_flow._plausible(text):
        shop_cart._touch(cart, awaiting=None)  # une question : l'IA répond, Bob n'attend plus
        return None
    clean = " ".join(text.split())
    surface = re.search(r"(\d[\d\s.]*)\s*m(?:²|2)\b", clean)
    values = {"project": OWNER_PROJECTS[match.group(1)], "address": clean[:200]}
    if surface:
        values["surface"] = re.sub(r"\D", "", surface.group(1))
    await _project(db, tenant, customer, **values)
    shop_cart._touch(cart, awaiting=None)
    return await auto_flow.slots(db, tenant, tu, customer, "S", None,
                                 note=_you(tu, "Merci.", "Merci."))


async def expects_text(db, tenant, customer, text) -> bool:
    return await auto_flow.expects_text(db, tenant, customer, text)

