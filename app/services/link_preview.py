"""
Lot 101 — aperçu des liens de Bob (QR code d'un produit, lien de la boutique) quand on les partage.

Avant : /qr/<code> et /w/<code> renvoyaient tout de suite vers wa.me, donc WhatsApp, Facebook ou un e-mail affichaient
l'aperçu de WhatsApp (logo, « Compte business »). Maintenant : une petite page avec l'aperçu du produit ou du bien
(photo, titre, prix — avec les honoraires pour un bien, jamais sans), qui ouvre WhatsApp aussitôt. Les robots d'aperçu
ne sont pas comptés comme des scans ou des clics.
"""
from html import escape

from fastapi.responses import HTMLResponse

CRAWLERS = ("whatsapp", "facebookexternalhit", "facebot", "meta-externalagent", "twitterbot", "linkedinbot", "slackbot",
            "telegrambot", "discordbot", "skypeuripreview", "googlebot", "bingbot", "applebot", "pinterest", "embedly")


def is_crawler(user_agent: str | None) -> bool:
    agent = (user_agent or "").lower()
    return any(name in agent for name in CRAWLERS)


def product_preview(product, shop_name: str) -> tuple[str, str, str | None]:
    """(titre, description, image) d'un produit, d'un véhicule ou d'un bien — le prix exact de la fiche."""
    from app.agents.tools import has_sendable_photo
    from app.services.product_cards import money_text
    from app.services.product_sheets import is_property

    if is_property(getattr(product, "bob_category", None)):
        from app.services import immo_flow

        detail = immo_flow._detail(product)
        price = " — ".join(line.replace("*", "") for line in immo_flow.price_lines(product))
        description = " · ".join(p for p in (detail, price) if p)
    else:
        price = money_text(product.price, product.currency) if product.price and float(product.price) > 0 else ""
        description = " · ".join(p for p in (price, f"chez {shop_name}") if p)
    image = product.image_url.strip() if has_sendable_photo(product) else None
    return product.name, description, image


def page(title: str, description: str, image: str | None, target: str, shop_name: str) -> HTMLResponse:
    t, d, url = escape(title), escape(description or ""), escape(target, quote=True)
    image_tags = (f'<meta property="og:image" content="{escape(image, quote=True)}">'
                  '<meta name="twitter:card" content="summary_large_image">') if image else '<meta name="twitter:card" content="summary">'
    html = (
        '<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{t}</title>"
        f'<meta property="og:type" content="website"><meta property="og:title" content="{t}">'
        f'<meta property="og:description" content="{d}"><meta property="og:site_name" content="{escape(shop_name)}">'
        f"{image_tags}"
        f'<meta http-equiv="refresh" content="0;url={url}">'
        "<style>body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#F5F8F6;color:#13241A;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}"
        "a{display:inline-block;background:#1B4332;color:#fff;text-decoration:none;padding:14px 18px;border-radius:12px;"
        "font-weight:700}@media (prefers-color-scheme: dark){body{background:#0D1511;color:#E4EEE8}a{background:#52B788;color:#0D1511}}"
        "</style></head><body><main style=\"text-align:center;max-width:420px\">"
        f"<p style=\"font-weight:700\">{t}</p><p>{d}</p>"
        f'<a href="{url}">Ouvrir WhatsApp</a></main>'
        f"<script>location.replace({_js(target)});</script></body></html>"
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


def _js(value: str) -> str:
    import json

    return json.dumps(value).replace("<", "\\u003c")
