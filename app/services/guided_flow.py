"""
Lot 94 — parcours à boutons sur WhatsApp (commerce), validé le 31/10 (maquette « Bob — parcours d'achat WhatsApp »).

Quand le client touche un bouton ou une ligne de liste du parcours, c'est le CODE qui répond, avec les vraies données
de la boutique, sans appel à l'IA : accueil, rayons, produits (carrousel avec le prix de la fiche), détails, tailles.
Le client peut toujours écrire librement : l'IA répond comme avant (garde-fou des prix du lot 93).

Décisions du 31/10 :
- l'accueil à boutons part quand le client dit seulement bonjour, écrit « menu », ou arrive d'une publicité sans texte ;
  une vraie question va à l'IA ;
- actif par défaut pour les boutiques, interrupteur « Parcours à boutons » dans Réglages de Bob ;
- « Voir plus » au-delà de 10 produits ;
- lot 95 : le deuxième bouton des cartes devient « Ajouter au panier » ; panier, livraison ou retrait, confirmation et
  suivi sont dans shop_cart.py.
Concessions, agences et courtiers : inchangés (lots 96 à 98).

Identifiants des boutons (préfixe « g: ») : g:menu, g:cat, g:all:<page>, g:off:<page>, g:r:<rayon>:<page>,
g:d:<produit>, g:t:<produit>, g:human ; panier (lot 95) : g:a, g:v, g:q, g:k, g:clr, g:ok, g:m, g:ad, g:val, g:s.
« interest:<produit> » reste celui du lot 76 (cartes envoyées par l'IA).
"""
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

logger = logging.getLogger(__name__)

PREFIX = "g:"
PAGE = 10
GREETINGS = {"bonjour", "bonsoir", "salut", "hello", "hi", "hey", "coucou", "bjr", "bsr", "slt", "cc", "salam",
             "salamalekoum", "allo", "alo", "menu", "accueil", "start", "commencer", "bonne", "journee", "soiree"}
FILLERS = {"monsieur", "madame", "mr", "mme", "la", "le", "les", "boutique", "bob", "svp", "stp", "a", "vous", "tous",
           "et", "toi", "ca", "va", "comment", "cv"}


@dataclass
class GuidedTurn:
    actions: list = field(default_factory=list)
    handoff: bool = False
    handoff_text: str | None = None
    # Lot 96 : motif du transfert (sinon « Le client a demandé un conseiller ») ; transfert déjà enregistré par l'outil
    # de rendez-vous ; rendez-vous confirmés pendant ce tour (la concession est prévenue par e-mail).
    handoff_reason: str | None = None
    handoff_done: bool = False
    booked: list = field(default_factory=list)
    # Lot 98 : e-mails à envoyer au professionnel (sujet, texte), ex. une réclamation enregistrée.
    alerts: list = field(default_factory=list)


# --- Petits outils ---------------------------------------------------------------------------------------------------

def applies(tenant) -> bool:
    from app.core.config import get_settings
    from app.services.business_type import is_dealership, is_insurance, is_real_estate, is_store

    return bool(get_settings().guided_flow_enabled and tenant is not None
                and (is_store(tenant) or is_dealership(tenant) or is_real_estate(tenant) or is_insurance(tenant))
                and getattr(tenant, "guided_flow_enabled", True))  # lots 96 à 98 : concessions, agences, courtiers


async def expects_text(db, tenant, customer, text) -> bool:
    """Ce message écrit est-il attendu par le parcours (panier, quantité, adresse, reprise, disponibilité) ?"""
    from app.services import assu_flow, auto_flow, shop_cart
    from app.services.business_type import is_dealership, is_insurance, is_real_estate

    if not applies(tenant) or customer is None:
        return False
    if is_insurance(tenant):
        return await assu_flow.expects_text(db, tenant, customer, text)
    if is_dealership(tenant) or is_real_estate(tenant):
        return await auto_flow.expects_text(db, tenant, customer, text)
    return await shop_cart.expects_text(db, tenant, customer, text)


def _norm(text: str | None) -> str:
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())


def is_greeting(text: str | None) -> bool:
    """« Bonjour », « Salut Bob ! », « bonsoir madame », « menu »… mais pas « bonjour, vous avez des robes ? »."""
    words = _norm(text).split()
    return bool(words) and len(words) <= 5 and set(words) <= GREETINGS | FILLERS and bool(set(words) & GREETINGS)


def wants_welcome(text: str | None, referral) -> bool:
    return is_greeting(text) or (referral is not None and not (text or "").strip())


def _you(tu: bool, vous: str, toi: str) -> str:
    return toi if tu else vous


def _number(raw) -> Decimal | None:
    try:
        return Decimal(str(raw)) if raw not in (None, "") else None
    except InvalidOperation:
        return None


def _uuid(raw: str):
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        return None


def _variants(product) -> list[tuple[int, str, Decimal, int]]:
    """Variantes en stock : (index, libellé « Taille 38 », prix, stock)."""
    out = []
    for i, v in enumerate(getattr(product, "variants", None) or []):
        if not isinstance(v, dict) or (v.get("stock") or 0) <= 0:
            continue
        options = v.get("options") or {}
        label = " · ".join(f"{k} {val}" for k, val in options.items() if val not in (None, "")) or (v.get("sku") or f"Modèle {i + 1}")
        out.append((i, str(label), _number(v.get("price")) or _number(product.price) or Decimal(0), int(v.get("stock") or 0)))
    return out


def _offer(product) -> bool:
    before = _number((product.sheet or {}).get("compare_at_price"))
    price = _number(product.price)
    return before is not None and price is not None and before > price


# --- Données de la boutique -----------------------------------------------------------------------------------------

async def _products(db, tenant):
    from app.models.product import Product

    rows = (await db.execute(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True))
                             .order_by(Product.updated_at.desc().nulls_last(), Product.name).limit(2000))).scalars().all()
    return [p for p in rows if (p.stock_quantity or 0) > 0 or _variants(p)]


async def _categories(db, tenant, products) -> list[tuple]:
    from app.models.category import Category

    names = {c.id: c.name for c in (await db.execute(select(Category).where(Category.tenant_id == tenant.id))).scalars()}
    groups: dict = {}
    for p in products:
        if p.category_id in names:
            groups.setdefault(p.category_id, []).append(p)
    return sorted(((cid, names[cid], items) for cid, items in groups.items()), key=lambda g: (-len(g[2]), g[1]))


# --- Les écrans ------------------------------------------------------------------------------------------------------

CART_BUTTON = (f"{PREFIX}k", "Voir mon panier")
# Lot 95 : « Parler à un conseiller » faisait 22 caractères (20 au plus sur WhatsApp, coupé en « Parler à un conseill »).
HUMAN_BUTTON = (f"{PREFIX}human", "Parler à la boutique")


async def welcome(db, tenant, tu: bool, basket: bool = False) -> GuidedTurn:
    products = await _products(db, tenant)
    buttons = [(f"{PREFIX}cat", "Voir le catalogue")]
    if basket:
        buttons.append(CART_BUTTON)
    elif any(_offer(p) for p in products):
        buttons.append((f"{PREFIX}off:0", "Voir les offres"))
    buttons.append(HUMAN_BUTTON)
    body = (f"Bonjour et bienvenue chez *{tenant.name}* ! Je suis Bob, le vendeur de la boutique. "
            + _you(tu, "Que souhaitez-vous faire ?", "Que veux-tu faire ?"))
    return GuidedTurn([{"type": "buttons", "body": body, "buttons": buttons}])


async def catalogue(db, tenant, tu: bool) -> GuidedTurn:
    from app.services.product_cards import money_text

    products = await _products(db, tenant)
    if not products:
        return GuidedTurn([{"type": "text", "body": _you(tu, "Notre catalogue arrive bientôt. Dites-moi ce que vous cherchez : "
                                                          "je vous réponds tout de suite.",
                                                     "Notre catalogue arrive bientôt. Dis-moi ce que tu cherches : "
                                                     "je te réponds tout de suite.")}])
    groups = await _categories(db, tenant, products)
    if len(groups) < 2:
        return await page(db, tenant, tu, "all", None, 0)
    rows = []
    shown = groups[:9] if len(groups) > 10 else groups
    for cid, name, items in shown:
        low = min(_number(p.price) or Decimal(0) for p in items)
        count = f"{len(items)} produit{'s' if len(items) > 1 else ''}"
        rows.append((f"{PREFIX}r:{cid}:0", name, f"{count} · dès {money_text(low, tenant.currency or 'XOF')}"))
    if len(groups) > 10:
        rows.append((f"{PREFIX}all:0", "Tout voir", f"{len(products)} produits"))
    body = _you(tu, "Voici nos rayons. Choisissez-en un : je vous montre ses produits avec leur prix.",
                "Voici nos rayons. Choisis-en un : je te montre ses produits avec leur prix.")
    return GuidedTurn([{"type": "list", "body": body, "button": "Choisir un rayon", "rows": rows}])


async def page(db, tenant, tu: bool, kind: str, ref, number: int, basket: bool = False) -> GuidedTurn:
    """Une page de 10 produits : carrousel pour ceux qui ont une photo, liste pour les autres, puis « Voir plus »."""
    from app.agents.tools import has_sendable_photo
    from app.services.product_cards import card_body, money_text

    products = await _products(db, tenant)
    title = "nos produits"
    if kind == "off":
        products = [p for p in products if _offer(p)]
        title = "nos offres"
    elif kind == "r":
        groups = {cid: (name, items) for cid, name, items in await _categories(db, tenant, products)}
        if ref not in groups:
            return await catalogue(db, tenant, tu)
        title, products = groups[ref][0].lower(), groups[ref][1]
    if not products:
        return GuidedTurn([{"type": "buttons", "body": _you(tu, "Pas d'offre en ce moment. Voulez-vous voir le catalogue ?",
                                                           "Pas d'offre en ce moment. Tu veux voir le catalogue ?"),
                            "buttons": [(f"{PREFIX}cat", "Voir le catalogue"), HUMAN_BUTTON]}])
    start = max(0, number) * PAGE
    chunk = products[start:start + PAGE]
    if not chunk:
        return await catalogue(db, tenant, tu)
    actions = []
    with_photo = [p for p in chunk if has_sendable_photo(p)]
    without = [p for p in chunk if not has_sendable_photo(p)]
    if len(with_photo) == 1 and not without:
        return await details(db, tenant, tu, with_photo[0], basket)
    if len(with_photo) >= 2:
        intro = _you(tu, f"Voici {title}. Touchez « Voir détails » ou « Ajouter au panier » sous celui qui vous plaît.",
                     f"Voici {title}. Touche « Voir détails » ou « Ajouter au panier » sous celui qui te plaît.")
        actions.append({"type": "carousel", "body": intro, "cards": [
            {"link": p.image_url.strip(), "body": card_body(p, with_price=True), "product_id": str(p.id), "name": p.name,
             "buttons": [(f"{PREFIX}d:{p.id}", "Voir détails"), (f"{PREFIX}a:{p.id}", "Ajouter au panier")]}
            for p in with_photo]})
    else:
        without = with_photo + without
    if without:
        rows = [(f"{PREFIX}d:{p.id}", p.name, money_text(p.price, p.currency)) for p in without[:10]]
        actions.append({"type": "list", "body": _you(tu, f"Voici {title}." if not actions else "Et ceux-ci, sans photo pour l'instant :",
                                                     f"Voici {title}." if not actions else "Et ceux-ci, sans photo pour l'instant :"),
                        "button": "Voir les produits", "rows": rows})
    more = len(products) > start + PAGE
    nav = [(f"{PREFIX}{kind}:{ref}:{number + 1}" if kind == "r" else f"{PREFIX}{kind}:{number + 1}", "Voir plus")] if more else []
    nav.append((f"{PREFIX}cat", "Retour au catalogue"))
    if basket:
        nav.append(CART_BUTTON)
    left = len(products) - start - len(chunk)
    actions.append({"type": "buttons", "body": (f"Il reste {left} produit{'s' if left > 1 else ''}." if more else
                                                _you(tu, "Vous pouvez aussi m'écrire ce que vous cherchez.",
                                                     "Tu peux aussi m'écrire ce que tu cherches.")),
                    "buttons": nav})
    return GuidedTurn(actions)


async def details(db, tenant, tu: bool, product, basket: bool = False) -> GuidedTurn:
    from app.agents.tools import has_sendable_photo
    from app.services.product_cards import price_line

    lines = [f"*{product.name}*"]
    price = price_line(product)
    if price:
        lines.append(price)
    if product.description:
        text = " ".join(product.description.split())
        lines.append(text if len(text) <= 600 else text[:599].rstrip() + "…")
    variants = _variants(product)
    if variants:
        lines.append("En stock : " + ", ".join(label for _, label, _, _ in variants[:12]))
    buttons = ([(f"{PREFIX}t:{product.id}", "Choisir la taille" if any("taille" in label.lower() for _, label, _, _ in variants)
                 else "Choisir le modèle")] if variants else [(f"{PREFIX}a:{product.id}", "Ajouter au panier")])
    buttons.append((f"{PREFIX}cat", "Retour au catalogue"))
    if basket:
        buttons.append(CART_BUTTON)
    image = product.image_url.strip() if has_sendable_photo(product) else None
    return GuidedTurn([{"type": "buttons", "body": "\n".join(lines), "buttons": buttons, "image": image,
                        "product_id": str(product.id), "name": product.name}])


async def sizes(db, tenant, tu: bool, product) -> GuidedTurn:
    from app.services.product_cards import money_text

    variants = _variants(product)
    if not variants:
        return await details(db, tenant, tu, product)
    rows = [(f"{PREFIX}v:{product.id}:{i}", label, f"{money_text(price, product.currency)} · {stock} en stock")
            for i, label, price, stock in variants[:10]]
    body = f"*{product.name}* : " + _you(tu, "choisissez la taille ou le modèle.", "choisis la taille ou le modèle.")
    return GuidedTurn([{"type": "list", "body": body, "button": "Choisir", "rows": rows}])


async def variant_text(db, tenant_id, reply_id: str | None) -> str | None:
    """
    « g:v:<produit>:<i> » → ce que le client a choisi, noté dans la conversation : « Robe wax Awa (Taille 38) ».
    Si le parcours est coupé entre-temps, Bob reçoit « Ça m'intéresse : Robe wax Awa (Taille 38) » et prend la commande.
    """
    from app.models.product import Product
    from app.models.tenant import Tenant
    from app.services.product_cards import BUTTON_TITLE

    if not reply_id or not reply_id.startswith(f"{PREFIX}v:"):
        return None
    parts = reply_id.split(":")
    product = await db.get(Product, _uuid(parts[2])) if len(parts) == 4 and _uuid(parts[2]) else None
    if product is None or product.tenant_id != tenant_id or not parts[3].isdigit():
        return None
    label = next((label for i, label, _, _ in _variants(product) if i == int(parts[3])), None)
    chosen = product.name + (f" ({label})" if label else "")
    return chosen if applies(await db.get(Tenant, tenant_id)) else f"{BUTTON_TITLE} : {chosen}"


async def handle(db, tenant, text: str | None, reply_id: str | None, referral=None, tu: bool = False,
                 customer=None, conversation=None) -> GuidedTurn | None:
    """Réponse du parcours, ou None : l'IA répond (question libre, « Ça m'intéresse » d'une carte de l'IA)."""
    from app.models.product import Product
    from app.services import shop_cart

    if not applies(tenant):
        return None
    if reply_id and reply_id.startswith(PREFIX):  # lot 99 : boutons des relances, rappels et livraisons (tous secteurs)
        from app.services import followups

        parts = reply_id[len(PREFIX):].split(":")
        if parts[0] in followups.ACTIONS:
            turn = await followups.handle_tap(db, tenant, tu, customer, conversation, parts[0], parts)
            if turn is not None:
                return turn
    from app.services.business_type import is_dealership, is_insurance, is_real_estate

    if is_insurance(tenant):  # lot 98 : devis, contrat, réclamation (jamais de prix)
        from app.services import assu_flow

        return await assu_flow.handle(db, tenant, text, reply_id, referral, tu, customer, conversation)
    if is_dealership(tenant):  # lot 96 : essai, reprise, financement (pas de panier)
        from app.services import auto_flow

        return await auto_flow.handle(db, tenant, text, reply_id, referral, tu, customer, conversation)
    if is_real_estate(tenant):  # lot 97 : visite, estimation (jamais de chiffre)
        from app.services import immo_flow

        return await immo_flow.handle(db, tenant, text, reply_id, referral, tu, customer, conversation)
    if reply_id and reply_id.startswith(PREFIX):
        parts = reply_id[len(PREFIX):].split(":")
        action = parts[0]
        cart_turn = await shop_cart.handle_tap(db, tenant, tu, customer, conversation, action, parts)
        if cart_turn is not None:
            return cart_turn
        basket = await shop_cart.has_items(db, tenant, customer)
        if action == "menu":
            return await welcome(db, tenant, tu, basket)
        if action == "cat":
            return await catalogue(db, tenant, tu)
        if action in ("all", "off") and len(parts) == 2 and parts[1].isdigit():
            return await page(db, tenant, tu, action, None, int(parts[1]), basket)
        if action == "r" and len(parts) == 3 and _uuid(parts[1]) and parts[2].isdigit():
            return await page(db, tenant, tu, "r", _uuid(parts[1]), int(parts[2]), basket)
        if action in ("d", "t") and len(parts) == 2 and _uuid(parts[1]):
            product = await db.get(Product, _uuid(parts[1]))
            if product is None or product.tenant_id != tenant.id or not product.active:
                return await catalogue(db, tenant, tu)
            if action == "d":
                return await details(db, tenant, tu, product, basket)
            return await sizes(db, tenant, tu, product)
        if action == "human":
            return GuidedTurn(handoff=True, handoff_text=_you(
                tu, "Je préviens un conseiller : il vous répond ici dès que possible.",
                "Je préviens un conseiller : il te répond ici dès que possible."))
        return await welcome(db, tenant, tu, basket)  # bouton inconnu (ancien message) : on repart de l'accueil
    if not reply_id and wants_welcome(text, referral):
        return await welcome(db, tenant, tu, await shop_cart.has_items(db, tenant, customer))
    if not reply_id and customer is not None:
        return await shop_cart.handle_text(db, tenant, tu, customer, text)  # « panier », quantité ou adresse attendue
    return None


# --- Envoi et trace dans la conversation ----------------------------------------------------------------------------

def _trace(action: dict) -> str:
    if action["type"] == "carousel":
        return "🛍 Cartes envoyées : " + ", ".join(c["name"] for c in action["cards"])
    extra = ""
    if action["type"] == "buttons":
        extra = "\n[Boutons : " + " · ".join(title for _, title in action["buttons"]) + "]"
    elif action["type"] == "list":
        extra = "\n[Liste : " + " · ".join(title for _, title, _ in action["rows"]) + "]"
    return action["body"] + extra


def _fallback_text(action: dict) -> str:
    """Si WhatsApp refuse le message interactif : le même contenu en texte, choix numérotés à recopier."""
    if action["type"] == "buttons":
        return action["body"]
    if action["type"] == "list":
        return action["body"] + "\n" + "\n".join(f"• {title}" + (f" : {desc}" if desc else "") for _, title, desc in action["rows"])
    if action["type"] == "carousel":
        return action["body"] + "\n" + "\n".join("• " + c["body"].replace("\n", " · ") for c in action["cards"])
    return action["body"]


async def deliver(db, tenant, conversation, wa_client, to: str, turn: GuidedTurn) -> int:
    """Envoie les écrans (repli en texte si WhatsApp refuse) et les note dans la conversation. Renvoie le nombre envoyé."""
    from app.models.conversation import Message, MessageSender

    sent = 0
    for action in turn.actions:
        kind = action.get("message_type") or "guided"
        metadata = {"guided": action["type"]}
        try:
            if action["type"] == "buttons":
                await wa_client.send_buttons(to=to, body=action["body"], buttons=action["buttons"], image_link=action.get("image"))
            elif action["type"] == "list":
                await wa_client.send_list(to=to, body=action["body"], button=action["button"], rows=action["rows"])
            elif action["type"] == "carousel":
                await wa_client.send_product_carousel(to=to, body=action["body"], cards=action["cards"])
                kind = "product_cards"
                metadata = {"product_ids": [c["product_id"] for c in action["cards"]],
                            "image_urls": [c["link"] for c in action["cards"]]}
            else:
                await wa_client.send_text_message(to=to, body=action["body"])
        except Exception:  # noqa: BLE001 — message interactif refusé : le contenu part en texte
            logger.warning("Écran du parcours refusé par WhatsApp (%s) : envoi en texte", action["type"])
            try:
                await wa_client.send_text_message(to=to, body=_fallback_text(action))
            except Exception:  # noqa: BLE001
                logger.warning("Écran du parcours non envoyé (conversation %s)", conversation.id)
                continue
        if action.get("after_sent"):  # lot 98 : ex. accusé de réception d'une réclamation réellement parti
            action["after_sent"]()
        if action.get("product_id") and action["type"] == "buttons" and action.get("image"):
            metadata = {"guided": "details", "product_ids": [action["product_id"]], "image_urls": [action["image"]]}
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI, message_type=kind,
                       content=_trace(action), message_metadata=metadata))
        sent += 1
    await db.commit()
    return sent
