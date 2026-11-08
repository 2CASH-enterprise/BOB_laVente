"""
Lot 112 — le reçu de commande en image (maquette validée le 07/11), pour les restaurants et les boutiques.

Deux moments : « Commande confirmée » quand le client valide, « Reçu de commande » (payé) quand le commerçant clique
« Paiement reçu ». L'image est dessinée ici (Pillow, polices Jost et Cormorant déjà servies par /fonts), déposée chez
WhatsApp (jamais d'adresse publique : elle porte le nom et l'adresse du client) et envoyée en tête du message à
boutons, avec un texte court (référence, total). Si quoi que ce soit échoue, le texte habituel part : jamais de
commande sans confirmation. Le bandeau du total prend la teinte choisie par le commerçant pour ses affiches.
"""
import io
import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

FONTS = Path(__file__).resolve().parents[1] / "static" / "fonts"
WIDTH, PAD, FRAME = 1080, 90, 26
GROUND, INK, MUTED, RULE, SOFT = "#FAF6EE", "#1A1612", "#6B6259", "#E6DCCB", "#9A9087"
CREAM = "#FFF8EC"
MAX_LINES = 25
CONFIRMED, PAID = "confirmed", "paid"
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
          "décembre"]


def applies(tenant) -> bool:
    """Restaurants et boutiques : les secteurs qui prennent des commandes (lot 112, décision du 08/11)."""
    from app.services.business_type import ONLINE_STORE, RESTAURANT

    return (getattr(tenant, "business_type", None) or ONLINE_STORE) in (ONLINE_STORE, RESTAURANT)


def _font(name: str, size: int):
    return ImageFont.truetype(str(FONTS / name), size)


def jost(size: int, weight: int = 400):
    return _font(f"jost-latin-{weight}-normal.woff2", size)


def cormorant(size: int):
    return _font("cormorant-garamond-latin-500-italic.woff2", size)


_UNDRAWABLE = re.compile("[─-\U0010FFFF]")  # émojis, symboles : absents des polices, ils feraient des carrés


def clean(text) -> str:
    return re.sub(r"\s+", " ", _UNDRAWABLE.sub("", str(text or ""))).strip()


def amount(value) -> str:
    from app.services.product_cards import money_text

    return money_text(value, "")


def currency_label(currency: str | None) -> str:
    code = (currency or "XOF").upper()
    return "FCFA" if code in ("XOF", "XAF") else ("€" if code == "EUR" else code)


def when(moment, zone) -> str:
    """« vendredi 6 novembre, 19 h 42 »."""
    from datetime import timezone

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(zone)
    return f"{DAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]}, {local.hour} h {local.minute:02d}"


def client_name(customer) -> str | None:
    """Prénom et initiale du nom (« Awa K. ») ; rien si le client n'a pas donné son nom."""
    first = clean(getattr(customer, "first_name", None))
    last = clean(getattr(customer, "last_name", None))
    if not first:
        return None
    return f"{first} {last[0].upper()}." if last else first


def accent_colors(tenant) -> tuple[str, str]:
    from app.api.kit.routes import accent_for
    from app.services import vitrine_posters

    return vitrine_posters.ACCENTS[accent_for(tenant)]


async def gather(db, order) -> dict:
    """Tout ce que l'image montre, lu en base (aucune donnée qui ne soit pas déjà dans la confirmation texte)."""
    from sqlalchemy import select

    from app.models.customer import Customer
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.tenant import Tenant
    from app.services.business_type import is_restaurant
    from app.services.local_time import tenant_zone
    from app.services.receipt_service import scheduled_line

    tenant = await db.get(Tenant, order.tenant_id)
    customer = await db.get(Customer, order.customer_id)
    rows = (await db.execute(select(OrderItem, Product.name).join(Product, Product.id == OrderItem.product_id)
                             .where(OrderItem.order_id == order.id))).all()
    zone = tenant_zone(tenant)
    restaurant = is_restaurant(tenant)
    items = [{"name": clean(name), "quantity": item.quantity, "detail": clean(item.variant_label) or None,
              "amount": float(item.subtotal)} for item, name in rows]
    fee, area = order.delivery_fee, clean(getattr(order, "delivery_area", None)) or None
    fee_row = None
    if order.fulfillment == "DELIVERY" and fee is not None:
        fee_row = (f"Livraison · {area}" if area else "Livraison", amount(fee) if float(fee) > 0 else "Offerte")
    place = None
    if order.fulfillment == "DELIVERY":
        written = clean(order.delivery_address)
        if getattr(order, "delivery_location", None) and written in ("", "Position partagée"):  # lot 113
            written = " · ".join(part for part in ("Position partagée", area) if part)
        place = ("Livraison", written or area or "À votre adresse")
    elif order.fulfillment == "PICKUP":
        place = ("Retrait", clean(getattr(tenant, "pickup_address", None)) or ("Au restaurant" if restaurant else "En boutique"))
    elif order.fulfillment == "DINE_IN":
        place = ("Sur place", f"Table {clean(order.table_number)}" if getattr(order, "table_number", None) else "Au restaurant")
    payment = clean(order.payment_method) or ("Par lien de paiement" if getattr(tenant, "payment_link", None) else None)
    from app.services.address_form import uses_tu

    return {
        "shop": clean(tenant.name) or "Votre commande", "restaurant": restaurant, "tu": uses_tu(tenant) and not restaurant,
        "ref": str(order.id)[:8].upper(), "date": when(order.created_at, zone) if order.created_at else None,
        "paid_on": when(order.paid_at, zone) if getattr(order, "paid_at", None) else None,
        "client": client_name(customer), "items": items, "fee_row": fee_row,
        "subtotal": sum(i["amount"] for i in items), "total": float(order.total_amount),
        "currency": currency_label(order.currency), "place": place, "payment": payment,
        "scheduled": scheduled_line(order.scheduled_for, zone) if getattr(order, "scheduled_for", None) else None,
        "accent": accent_colors(tenant),
    }


# --- Dessin ----------------------------------------------------------------------------------------------------------

def _w(draw, text, font) -> int:
    return int(draw.textlength(text, font=font))


def _fit(draw, text, font, width) -> str:
    if _w(draw, text, font) <= width:
        return text
    while text and _w(draw, text + "…", font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw, text, font, width, lines=2) -> list[str]:
    words, out, line = text.split(" "), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if _w(draw, trial, font) <= width:
            line = trial
            continue
        if line:
            out.append(line)
        line = word
        if len(out) == lines:
            break
    if line and len(out) < lines:
        out.append(line)
    rest = " ".join(words)
    if out and " ".join(out) != rest:
        out[-1] = _fit(draw, out[-1] + " …", font, width)
    return [_fit(draw, l, font, width) for l in out] or [""]


def _spaced(draw, x, y, text, font, fill, spacing, anchor="l"):
    total = sum(_w(draw, ch, font) for ch in text) + spacing * max(len(text) - 1, 0)
    if anchor == "m":
        x -= total // 2
    elif anchor == "r":
        x -= total
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += _w(draw, ch, font) + spacing
    return total


def _leader(draw, x1, x2, y):
    for x in range(int(x1), int(x2), 6):
        draw.point((x, y), fill="#C9BBA6")


def _row(draw, y, left, right, font, color, right_color=None):
    """« Libellé ······ montant » ; renvoie la hauteur de la ligne."""
    right_w = _w(draw, right, font)
    left = _fit(draw, left, font, WIDTH - 2 * PAD - right_w - 60)
    left_w = _w(draw, left, font)
    draw.text((PAD, y), left, font=font, fill=color)
    draw.text((WIDTH - PAD - right_w, y), right, font=font, fill=right_color or color)
    ascent = font.getmetrics()[0]
    _leader(draw, PAD + left_w + 14, WIDTH - PAD - right_w - 14, y + ascent - 4)
    return ascent


def render(data: dict, kind: str = CONFIRMED) -> bytes:
    light, dark = data["accent"]
    img = Image.new("RGB", (WIDTH, 4200), GROUND)
    d = ImageDraw.Draw(img)
    inner = WIDTH - 2 * PAD
    y = 70

    size = 30
    while size > 18 and _spaced(ImageDraw.Draw(Image.new("RGB", (1, 1))), 0, 0, data["shop"].upper(), jost(size, 500), INK, 9) > inner:
        size -= 2
    _spaced(d, WIDTH // 2, y, _fit(d, data["shop"].upper(), jost(size, 500), inner), jost(size, 500), INK, 9 if size > 22 else 5, "m")
    y += 56
    d.line((WIDTH // 2 - 95, y, WIDTH // 2 - 18, y), fill=light, width=1)
    d.line((WIDTH // 2 + 18, y, WIDTH // 2 + 95, y), fill=light, width=1)
    d.polygon([(WIDTH // 2, y - 5), (WIDTH // 2 + 5, y), (WIDTH // 2, y + 5), (WIDTH // 2 - 5, y)], outline=light)
    y += 18
    title = "Reçu de commande" if kind == PAID else "Commande confirmée"
    d.text((WIDTH // 2, y), title, font=cormorant(78), fill=dark, anchor="mt")
    y += 104
    if kind == PAID:
        stamp = f"Payé le {data['paid_on']}" if data.get("paid_on") else "Payé"
        _spaced(d, WIDTH // 2, y, stamp.upper(), jost(20, 500), dark, 4, "m")
        y += 44
    d.line((PAD, y, WIDTH - PAD, y), fill=RULE, width=1)
    y += 26

    cols = [("Commande n°", data["ref"], "l")]
    if data.get("date"):
        cols.append(("Le", data["date"], "m" if data.get("client") else "r"))
    if data.get("client"):
        cols.append(("Client", data["client"], "r"))
    label_f, value_f = jost(22, 400), jost(26, 500)
    for i, (label, value, align) in enumerate(cols):
        if align == "l":
            x = PAD
        elif align == "r":
            x = WIDTH - PAD
        else:
            x = PAD + int(inner * 0.36)
        anchor = {"l": "la", "r": "ra", "m": "la"}[align]
        d.text((x, y), label, font=label_f, fill=MUTED, anchor=anchor)
        width = inner * (0.4 if align == "m" else 0.3 if len(cols) == 3 else 0.6)
        d.text((x, y + 34), _fit(d, value, value_f, int(width)), font=value_f, fill=INK, anchor=anchor)
    y += 90
    d.line((PAD, y, WIDTH - PAD, y), fill=RULE, width=1)
    y += 30

    if data.get("scheduled"):
        text = data["scheduled"]
        f = jost(26, 500)
        w = _w(d, text, f) + 56
        d.rounded_rectangle((WIDTH // 2 - w // 2, y, WIDTH // 2 + w // 2, y + 56), radius=28, outline=light, width=2)
        d.text((WIDTH // 2, y + 28), text, font=f, fill=dark, anchor="mm")
        y += 86

    item_f, detail_f = jost(30, 400), jost(21, 300)
    items = data["items"]
    shown = items[:MAX_LINES] if len(items) <= MAX_LINES else items[:MAX_LINES - 1]
    for item in shown:
        _row(d, y, f"{item['name']} × {item['quantity']}", amount(item["amount"]), item_f, INK)
        y += 44
        if item.get("detail"):
            d.text((PAD, y), _fit(d, item["detail"], detail_f, inner - 200), font=detail_f, fill=SOFT)
            y += 32
        y += 22
    if len(shown) < len(items):
        rest = items[len(shown):]
        _row(d, y, f"Et {len(rest)} autres articles", amount(sum(i["amount"] for i in rest)), item_f, MUTED)
        y += 66
    if data.get("fee_row"):
        d.line((PAD, y - 6, WIDTH - PAD, y - 6), fill=RULE, width=1)
        y += 16
        small = jost(24, 400)
        _row(d, y, "Sous-total", amount(data["subtotal"]), small, MUTED)
        y += 52
        _row(d, y, data["fee_row"][0], data["fee_row"][1], small, MUTED)
        y += 52
    y += 14

    band = 124
    d.rectangle((PAD, y, WIDTH - PAD, y + band), fill=dark)
    _spaced(d, PAD + 30, y + band // 2 - 12, "TOTAL", jost(24, 500), CREAM, 7)
    cur_f, total_f = jost(26, 500), jost(60, 500)
    cur = data["currency"]
    cur_w = _w(d, cur, cur_f) + 6 * (len(cur) - 1)
    _spaced(d, WIDTH - PAD - 30, y + band // 2 + 2, cur, cur_f, CREAM, 6, "r")
    d.text((WIDTH - PAD - 30 - cur_w - 14, y + band // 2 + 26), amount(data["total"]), font=total_f, fill="#FFFFFF", anchor="rs")
    y += band + 24

    boxes = [b for b in (data.get("place"), ("Paiement", "Payé" if kind == PAID else data.get("payment"))) if b and b[1]]
    if boxes:
        gap = 20
        box_w = (inner - gap * (len(boxes) - 1)) // len(boxes)
        value_f = jost(25, 500)
        wrapped = [_wrap(d, value, value_f, box_w - 40) for _, value in boxes]
        box_h = 70 + 34 * max(len(w) for w in wrapped)
        for i, ((label, _), lines) in enumerate(zip(boxes, wrapped)):
            x = PAD + i * (box_w + gap)
            d.rectangle((x, y, x + box_w, y + box_h), outline=RULE, width=2)
            d.text((x + 20, y + 18), label, font=jost(20, 400), fill=MUTED)
            for j, line in enumerate(lines):
                d.text((x + 20, y + 50 + 34 * j), line, font=value_f, fill=INK)
        y += box_h + 34

    you = "ta" if data.get("tu") else "votre"
    if kind == PAID:
        thanks = ["Merci ! Le paiement est bien reçu.", "Gardez ce reçu." if not data.get("tu") else "Garde ce reçu."]
    else:
        thanks = [f"Merci pour {you} commande !",
                  "Pour la suivre, touchez « Suivre ma commande »." if not data.get("tu")
                  else "Pour la suivre, touche « Suivre ma commande »."]
    for line in thanks:
        d.text((WIDTH // 2, y), line, font=jost(22, 400), fill=MUTED, anchor="mt")
        y += 32
    y += 10
    sig_f, sig_b = jost(19, 400), jost(19, 500)
    parts = [("Proposé par ", sig_f, MUTED), ("Bob", sig_b, INK), (" · agenc-ai.com/bob", sig_f, MUTED)]
    x = WIDTH // 2 - sum(_w(d, t, f) for t, f, _ in parts) // 2
    for text, f, color in parts:
        d.text((x, y), text, font=f, fill=color)
        x += _w(d, text, f)
    y += 64

    img = img.crop((0, 0, WIDTH, y))
    d = ImageDraw.Draw(img)
    d.rectangle((FRAME, FRAME, WIDTH - FRAME - 1, y - FRAME - 1), outline=light, width=1)
    d.rectangle((FRAME + 8, FRAME + 8, WIDTH - FRAME - 9, y - FRAME - 9), outline=RULE, width=1)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def caption(order, kind: str = CONFIRMED, full_text: str | None = None) -> str:
    """Le texte sous l'image : référence et total (la notification et la recherche WhatsApp restent utiles), puis ce
    qui suit le total dans la confirmation texte (lien de paiement, adresse de retrait, suite) : rien ne se perd."""
    from app.services.product_cards import money_text

    head = "🧾 Reçu de commande" if kind == PAID else "✅ Commande confirmée"
    total = money_text(order.total_amount, "")
    text = f"{head} n° {str(order.id)[:8].upper()}\nTotal : *{total} {currency_label(order.currency)}*"
    if kind == CONFIRMED and full_text and "\nTotal : " in full_text:
        after = full_text.split("\nTotal : ", 1)[1].partition("\n")[2].strip()
        if after:
            text += "\n\n" + after
    return text[:1024]


async def build(db, order, kind: str = CONFIRMED) -> bytes | None:
    """L'image, ou None si quoi que ce soit manque (le texte habituel partira)."""
    try:
        return render(await gather(db, order), kind)
    except Exception:  # noqa: BLE001 — un reçu en image n'empêche jamais la confirmation
        logger.exception("Reçu en image non dessiné (commande %s)", getattr(order, "id", "?"))
        return None


async def upload(db, wa_client, order, kind: str = CONFIRMED) -> str | None:
    """Dessine et dépose l'image chez WhatsApp ; renvoie son identifiant, ou None."""
    picture = await build(db, order, kind)
    if picture is None:
        return None
    try:
        return await wa_client.upload_media(picture, "image/png", f"recu-{str(order.id)[:8]}.png")
    except Exception:  # noqa: BLE001
        logger.warning("Reçu en image non déposé chez WhatsApp (commande %s) : texte envoyé", order.id)
        return None


async def send(db, wa_client, to: str, order, kind: str, buttons: list | None, full_text: str | None = None):
    """Pour les envois hors parcours (paiement reçu, commande prise par l'IA) : l'image avec ses boutons. Renvoie la
    réponse de WhatsApp, ou None si l'image n'a pas pu partir (l'appelant envoie alors le texte habituel)."""
    media_id = await upload(db, wa_client, order, kind)
    if media_id is None:
        return None
    try:
        if buttons:
            return await wa_client.send_buttons(to=to, body=caption(order, kind, full_text), buttons=buttons, image_id=media_id)
        return await wa_client.send_image_by_id(to=to, media_id=media_id, caption=caption(order, kind, full_text))
    except Exception:  # noqa: BLE001
        logger.warning("Reçu en image refusé par WhatsApp (commande %s) : texte envoyé", order.id)
        return None
