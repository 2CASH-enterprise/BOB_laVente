"""
Lot 117 — cartes en image de l'assistant de Bob sur le numéro AgenC'AI (contrat Bob ↔ Naomy 1.3.3, `image_png`).

Mes ventes, Ventes à confirmer, Commandes, Stock, Qui attend : une image soignée (même style que le reçu du lot 112 :
Jost + Cormorant, fond crème, teinte des affiches du commerçant), envoyée par Naomy en tête du message, avec le texte
complet dessous (c'est aussi le repli) et les boutons d'action. Jamais d'adresse publique : l'image part en base64
dans l'envoi signé, Naomy la dépose chez WhatsApp. Les clients n'y figurent que par leur prénom et l'initiale du nom,
ou par la fin masquée de leur numéro (« Client •• 0099 ») : jamais un numéro entier.

Dessiné par le code (Pillow), sans IA, sans coût. Si quoi que ce soit échoue, le texte seul part, comme avant.
Interrupteur : ASSISTANT_CARDS dans le .env (vide = éteint ; « on » = toutes les boutiques ; sinon une liste
d'identifiants de boutiques séparés par des virgules, pour un essai).
"""
import io
import logging
import re

from PIL import Image, ImageDraw

from app.services import receipt_image as ri

logger = logging.getLogger(__name__)

WIDTH, PAD, MAX_HEIGHT = ri.WIDTH, ri.PAD, 1920
MAX_ROWS = 8


def cards_on(tenant_id) -> bool:
    from app.core.config import get_settings

    value = (getattr(get_settings(), "assistant_cards", "") or "").strip().lower()
    if not value or value in ("off", "false", "0"):
        return False
    if value in ("on", "true", "1", "all"):
        return True
    return str(tenant_id).lower() in {v.strip() for v in value.split(",") if v.strip()}


def person(label: str | None) -> str:
    """« Awa Koné » → « Awa K. » ; « Client •• 0099 » reste tel quel ; un numéro entier n'apparaît jamais."""
    text = ri.clean(label or "")
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 6 and not text.startswith("Client ••"):
        return f"Client •• {digits[-4:]}"
    if not text or text.startswith("Client ••"):
        return text or "Un client"
    parts = text.split()
    return f"{parts[0]} {parts[1][0].upper()}." if len(parts) > 1 and parts[1][:1].isalpha() else parts[0]


def render(shop: str, title: str, subtitle: str | None, accent: tuple[str, str], blocks: list[dict]) -> bytes:
    """Une carte : en-tête (boutique, titre, date), puis des blocs : kpis, section, rows, note, empty."""
    light, dark = accent
    img = Image.new("RGB", (WIDTH, 4000), ri.GROUND)
    d = ImageDraw.Draw(img)
    inner = WIDTH - 2 * PAD
    y = 70
    name_font = ri.jost(28, 500)
    ri._spaced(d, WIDTH // 2, y, ri._fit(d, ri.clean(shop).upper() or "VOTRE BOUTIQUE", name_font, inner - 120), name_font,
               ri.INK, 7, "m")
    y += 52
    d.line((WIDTH // 2 - 95, y, WIDTH // 2 - 18, y), fill=light, width=1)
    d.line((WIDTH // 2 + 18, y, WIDTH // 2 + 95, y), fill=light, width=1)
    d.polygon([(WIDTH // 2, y - 5), (WIDTH // 2 + 5, y), (WIDTH // 2, y + 5), (WIDTH // 2 - 5, y)], outline=light)
    y += 18
    d.text((WIDTH // 2, y), title, font=ri.cormorant(76), fill=dark, anchor="mt")
    y += 100
    if subtitle:
        ri._spaced(d, WIDTH // 2, y, subtitle.upper(), ri.jost(20, 500), ri.MUTED, 4, "m")
        y += 44
    d.line((PAD, y, WIDTH - PAD, y), fill=ri.RULE, width=1)
    y += 30
    for block in blocks:
        kind = block["kind"]
        if kind == "kpis":
            tiles = block["items"][:3]
            gap = 18
            tile_w = (inner - gap * (len(tiles) - 1)) // len(tiles)
            for i, (label, value, sub) in enumerate(tiles):
                x = PAD + i * (tile_w + gap)
                highlight = i == block.get("highlight", -1)
                d.rectangle((x, y, x + tile_w, y + 170), fill=dark if highlight else None,
                            outline=dark if highlight else ri.RULE, width=2)
                ink, soft = ("#FFFFFF", ri.CREAM) if highlight else (ri.INK, ri.MUTED)
                d.text((x + tile_w // 2, y + 22), label, font=ri.jost(22, 500), fill=soft, anchor="mt")
                size = 46
                while size > 26 and ri._w(d, value, ri.jost(size, 500)) > tile_w - 28:  # le chiffre entier, jamais coupé
                    size -= 2
                big = ri.jost(size, 500)
                d.text((x + tile_w // 2, y + 64 + (46 - size) // 2), ri._fit(d, value, big, tile_w - 24), font=big, fill=ink,
                       anchor="mt")
                if sub:
                    d.text((x + tile_w // 2, y + 126), ri._fit(d, sub, ri.jost(20, 400), tile_w - 24), font=ri.jost(20, 400),
                           fill=soft, anchor="mt")
            y += 200
        elif kind == "section":
            ri._spaced(d, PAD, y, block["text"].upper(), ri.jost(20, 500), dark, 4)
            y += 44
        elif kind == "rows":
            rows = block["items"]
            shown = rows[:MAX_ROWS]
            font, small = ri.jost(28, 400), ri.jost(21, 300)
            for left, right, detail in shown:
                ri._row(d, y, ri.clean(left), ri.clean(right), font, ri.INK, block.get("right_color"))
                y += 42
                if detail:
                    d.text((PAD, y), ri._fit(d, ri.clean(detail), small, inner - 40), font=small, fill=ri.SOFT)
                    y += 30
                y += 18
            if len(rows) > len(shown):
                d.text((PAD, y), f"… et {len(rows) - len(shown)} de plus dans Bob", font=ri.jost(22, 400), fill=ri.MUTED)
                y += 44
            y += 6
        elif kind in ("note", "empty"):
            font = ri.jost(26 if kind == "empty" else 22, 400)
            for line in ri._wrap(d, block["text"], font, inner, lines=3):
                d.text((WIDTH // 2, y), line, font=font, fill=ri.MUTED if kind == "note" else ri.INK, anchor="mt")
                y += 38
            y += 18
        if y > MAX_HEIGHT - 150:
            break
    y += 10
    sig_f, sig_b = ri.jost(19, 400), ri.jost(19, 500)
    parts = [("Bob", sig_b, ri.INK), (" · votre vendeur sur WhatsApp", sig_f, ri.MUTED)]
    x = WIDTH // 2 - sum(ri._w(d, t, f) for t, f, _ in parts) // 2
    for text, f, color in parts:
        d.text((x, y), text, font=f, fill=color)
        x += ri._w(d, text, f)
    y += 64
    img = img.crop((0, 0, WIDTH, min(y, MAX_HEIGHT)))
    d = ImageDraw.Draw(img)
    d.rectangle((ri.FRAME, ri.FRAME, WIDTH - ri.FRAME - 1, img.height - ri.FRAME - 1), outline=light, width=1)
    d.rectangle((ri.FRAME + 8, ri.FRAME + 8, WIDTH - ri.FRAME - 9, img.height - ri.FRAME - 9), outline=ri.RULE, width=1)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _today(tenant, now) -> str:
    from app.services.local_time import tenant_zone

    local = now.astimezone(tenant_zone(tenant))
    return f"{ri.DAYS[local.weekday()]} {local.day} {ri.MONTHS[local.month - 1]}"


def safe(build):
    """Une carte, ou None si quoi que ce soit échoue (le texte part seul)."""
    try:
        return build()
    except Exception:  # noqa: BLE001
        logger.exception("Carte de l'assistant non dessinée")
        return None


def sales_card(tenant, summary: dict, now) -> bytes:
    cur = ri.currency_label(summary["currency"])

    def tile(label, p):
        return (label, str(p["revenue"]), f"{cur} · {p['sales']} vente{'s' if p['sales'] > 1 else ''}")

    blocks = [{"kind": "kpis", "highlight": 0, "items": [tile("Aujourd'hui", summary["today"]), tile("7 jours", summary["week"]),
                                                         tile("30 jours", summary["month"])]}]
    if summary["top"]:
        blocks += [{"kind": "section", "text": "Ce qui se vend le mieux · 30 jours"},
                   {"kind": "rows", "items": [(f"{t['name']} × {t['quantity']}", f"{t['revenue']} {cur}", None)
                                              for t in summary["top"][:5]]}]
    else:
        blocks.append({"kind": "note", "text": "Les ventes comptent dès que vous touchez « Vendu »."})
    return render(tenant.name, "Mes ventes", _today(tenant, now), ri.accent_colors(tenant), blocks)


def pending_card(tenant, items: list[dict], now) -> bytes:
    if not items:
        blocks = [{"kind": "empty", "text": "Aucune vente à confirmer pour l'instant."}]
    else:
        cur = ri.currency_label(items[0]["currency"])
        blocks = [{"kind": "rows", "items": [(person(o["customer"]), f"{o['total']} {cur}", o["items"] or None) for o in items]},
                  {"kind": "note", "text": "Touchez « Vendu » quand le paiement est reçu."}]
    title = f"À confirmer · {len(items)}" if items else "À confirmer"
    return render(tenant.name, title, _today(tenant, now), ri.accent_colors(tenant), blocks)


def orders_card(tenant, orders: list[dict], now) -> bytes:
    if not orders:
        blocks = [{"kind": "empty", "text": "Aucune commande aujourd'hui pour l'instant."}]
    else:
        cur = ri.currency_label(orders[0]["currency"])
        sold = [o for o in orders if o["status"] == "Vendue"]
        blocks = [{"kind": "kpis", "highlight": 0, "items": [
            ("Commandes", str(len(orders)), "aujourd'hui"),
            ("Vendues", str(len(sold)), f"{ri.amount(sum(o['value'] for o in sold))} {cur}"),
            ("À confirmer", str(sum(1 for o in orders if o["status"] == "À confirmer")), None)]},
            {"kind": "rows", "items": [(person(o["customer"]), f"{o['total']} {cur}", f"{o['status']} · {o['items']}" if o["items"]
                                        else o["status"]) for o in orders]}]
    return render(tenant.name, "Commandes", _today(tenant, now), ri.accent_colors(tenant), blocks)


def stock_card(tenant, out: list[str], low: list[tuple[str, int]], now) -> bytes:
    if not out and not low:
        blocks = [{"kind": "empty", "text": "Tous vos produits sont en stock."}]
    else:
        blocks = []
        if out:  # les ruptures en tête
            blocks += [{"kind": "section", "text": f"Épuisés · {len(out)}"},
                       {"kind": "rows", "items": [(n, "0", None) for n in out], "right_color": "#A23B2A"}]
        if low:
            blocks += [{"kind": "section", "text": f"Bientôt épuisés · {len(low)}"},
                       {"kind": "rows", "items": [(n, str(q), None) for n, q in low]}]
        blocks.append({"kind": "note", "text": "Bob ne propose plus un produit épuisé. Pour un produit : « stock robe »."})
    return render(tenant.name, "Mon stock", _today(tenant, now), ri.accent_colors(tenant), blocks)


def waiting_card(tenant, items: list[dict], now) -> bytes:
    if not items:
        blocks = [{"kind": "empty", "text": "Personne ne vous attend : Bob s'occupe de tous vos clients."}]
    else:
        blocks = [{"kind": "rows", "items": [(person(w["name"]), _since(w.get("since"), now),
                                              f"« {w['last_message']} »" if w.get("last_message") else None) for w in items]},
                  {"kind": "note", "text": "Répondez-leur, ou laissez Bob continuer avec l'un d'eux."}]
    title = f"Qui attend · {len(items)}" if items else "Qui attend"
    return render(tenant.name, title, _today(tenant, now), ri.accent_colors(tenant), blocks)


def _since(iso: str | None, now) -> str:
    from datetime import datetime

    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    minutes = max(0, int((now - moment).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} j"
