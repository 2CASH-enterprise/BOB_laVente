"""
Lot 59 — « kit de lancement » : sur WhatsApp, c'est le client qui écrit en premier. Le kit donne au commerçant
de quoi l'y inviter partout où ses clients passent : un lien suivi (les clients venus par le kit sont comptés,
source « Affiche, flyer, salon »), son QR code, une affiche imprimable et des textes prêts à copier (vitrine,
facture, statut WhatsApp, signature d'email, réseaux sociaux), adaptés au secteur.

Le lien du kit est un point de contact comme ceux du lot 27 (code « KIT_… ») ; il ne compte pas dans la limite
de liens du plan. Sans numéro WhatsApp connecté, il n'y a pas de kit.
"""
import io
import secrets
from html import escape

from sqlalchemy import select

from app.services.plan_limits import KIT_CODE_PREFIX

KIT_NAME = "Kit de lancement"
GREETING_MAX = 300
GREETINGS = {
    "ONLINE_STORE": "Bonjour, je voudrais des informations",
    "CAR_DEALERSHIP": "Bonjour, je m'intéresse à un véhicule",
    "INSURANCE_BROKER": "Bonjour, j'ai une question sur mon assurance",
}


def _sector(tenant) -> str:
    from app.services.business_type import normalize

    return normalize(getattr(tenant, "business_type", None))


async def kit_link(db, tenant):
    """Le point de contact du kit (créé au premier affichage), ou None sans numéro WhatsApp connecté."""
    from app.models.contact_point import ContactPoint
    from app.models.whatsapp_account import WhatsAppAccount

    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()
    if account is None or not account.display_phone_number:
        return None, None
    cp = (await db.execute(select(ContactPoint).where(
        ContactPoint.tenant_id == tenant.id, ContactPoint.code.startswith(KIT_CODE_PREFIX),
        ContactPoint.archived_at.is_(None),
    ).order_by(ContactPoint.created_at).limit(1))).scalar_one_or_none()
    if cp is None:
        cp = ContactPoint(tenant_id=tenant.id, code=KIT_CODE_PREFIX + secrets.token_hex(4), name=KIT_NAME,
                          greeting=GREETINGS.get(_sector(tenant), GREETINGS["ONLINE_STORE"]), channel="PRINT", active=True)
        db.add(cp)
        await db.flush()
    return cp, account


def qr_svg(url: str) -> str:
    import segno

    out = io.BytesIO()
    segno.make(url, error="m").save(out, kind="svg", scale=8, border=4, xmldecl=False, svgns=True,
                                    title="QR code WhatsApp", dark="#111111", light="#ffffff")
    return out.getvalue().decode()


_HOOKS = {  # (accroche courte, accroche longue) — concession et courtier vouvoient toujours (lots 38, 53)
    "INSURANCE_BROKER": ("Une question sur votre assurance ou un renouvellement ?",
                         "Devis, renouvellement, question sur votre contrat"),
    "CAR_DEALERSHIP": ("Un véhicule vous intéresse ?", "Disponibilité, essai, reprise de votre véhicule"),
    "ONLINE_STORE": ("Une question, une commande ?", "Questions, commandes, suivi de commande"),
}


def texts(tenant, link: str) -> list[dict]:
    """Textes prêts à copier, au vouvoiement (au tutoiement pour un commerce qui l'a choisi, lot 38)."""
    from app.services.address_form import uses_tu

    tu = uses_tu(tenant)
    short, hook = _HOOKS.get(_sector(tenant), _HOOKS["ONLINE_STORE"])
    name = tenant.name
    if tu:
        return [
            {"title": "Vitrine, comptoir, affiche", "text": f"{short} Écris-nous sur WhatsApp : réponse immédiate, 7 jours sur 7. {link}"},
            {"title": "Facture, reçu, devis", "text": f"Pour toute question, écris-nous sur WhatsApp : {link}"},
            {"title": "Statut WhatsApp et réseaux sociaux", "text": f"📲 {hook} : écris-nous directement sur WhatsApp, on te répond tout de suite ! {link}"},
            {"title": "Signature d'email", "text": f"{name} — Écris-nous sur WhatsApp : {link}"},
            {"title": "Message à vos contacts (depuis votre téléphone)", "text": f"Coucou, c'est {name} ! Pour nous joindre plus facilement, écris-nous maintenant sur WhatsApp : {link}"},
        ]
    return [
        {"title": "Vitrine, comptoir, affiche", "text": f"{short} Écrivez-nous sur WhatsApp : réponse immédiate, 7 jours sur 7. {link}"},
        {"title": "Facture, reçu, devis", "text": f"Pour toute question, écrivez-nous sur WhatsApp : {link}"},
        {"title": "Statut WhatsApp et réseaux sociaux", "text": f"📲 {hook} : écrivez-nous directement sur WhatsApp, nous vous répondons tout de suite ! {link}"},
        {"title": "Signature d'email", "text": f"{name} — Écrivez-nous sur WhatsApp : {link}"},
        {"title": "Message à vos contacts (depuis votre téléphone)", "text": f"Bonjour, c'est {name}. Pour nous joindre plus facilement, écrivez-nous désormais sur WhatsApp : {link}"},
    ]


async def kit(db, tenant, base_url: str) -> dict:
    from sqlalchemy import func

    from app.models.customer import Customer

    cp, account = await kit_link(db, tenant)
    if cp is None:
        return {"connected": False}
    link = f"{base_url.rstrip('/')}/w/{cp.code}"
    digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
    customers = (await db.execute(select(func.count(Customer.id)).where(
        Customer.tenant_id == tenant.id, Customer.acquisition_contact_point_id == cp.id))).scalar_one()
    return {
        "connected": True, "phone": account.display_phone_number, "link": link, "direct_link": f"https://wa.me/{digits}",
        "greeting": cp.greeting, "active": cp.active, "clicks": cp.click_count, "customers": customers,
        "qr_svg": qr_svg(link), "texts": texts(tenant, link),
    }


def poster_html(tenant, data: dict) -> str:
    """Affiche A4 imprimable : nom, appel à écrire, QR code, numéro. Tout est échappé."""
    lead = _HOOKS.get(_sector(tenant), _HOOKS["ONLINE_STORE"])[1]
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Affiche WhatsApp — {escape(tenant.name)}</title>
<style>
  @page {{ size: A4; margin: 14mm; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #10251b; background: #f4f5f7; }}
  .sheet {{ max-width: 720px; margin: 24px auto; background: #fff; border-radius: 16px; padding: 48px 40px; text-align: center; box-sizing: border-box; }}
  .name {{ font-size: 22px; font-weight: 700; letter-spacing: .02em; color: #3d5a4c; margin: 0 0 18px; }}
  h1 {{ font-size: 44px; line-height: 1.1; margin: 0 0 12px; }}
  .lead {{ font-size: 20px; color: #3d5a4c; margin: 0 0 28px; }}
  .qr {{ width: 320px; max-width: 80%; margin: 0 auto 20px; }} .qr svg {{ width: 100%; height: auto; display: block; }}
  .hint {{ font-size: 18px; margin: 0 0 6px; }} .phone {{ font-size: 30px; font-weight: 800; margin: 0 0 22px; letter-spacing: .03em; }}
  .badge {{ display: inline-block; background: #25d366; color: #0b2a17; font-weight: 700; border-radius: 999px; padding: 10px 22px; font-size: 18px; }}
  .print {{ display: block; margin: 0 auto 24px; background: #1f3d2f; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px; font: inherit; font-weight: 600; cursor: pointer; }}
  @media print {{ body {{ background: #fff; }} .sheet {{ margin: 0; border-radius: 0; padding: 20mm 10mm; max-width: none; }} .print {{ display: none; }} }}
</style></head>
<body>
<div class="sheet">
  <button class="print" type="button" onclick="window.print()">Imprimer cette affiche</button>
  <p class="name">{escape(tenant.name)}</p>
  <h1>Écrivez-nous sur WhatsApp</h1>
  <p class="lead">{escape(lead)}</p>
  <div class="qr">{data["qr_svg"]}</div>
  <p class="hint">Scannez avec l'appareil photo de votre téléphone, ou écrivez au</p>
  <p class="phone">{escape(data["phone"])}</p>
  <span class="badge">Réponse immédiate, 7 jours sur 7</span>
</div>
</body></html>"""
