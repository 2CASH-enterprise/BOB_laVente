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

from sqlalchemy import select

from app.services.plan_limits import KIT_CODE_PREFIX

KIT_NAME = "Kit de lancement"
GREETING_MAX = 300
GREETINGS = {
    "ONLINE_STORE": "Bonjour, je voudrais des informations",
    "CAR_DEALERSHIP": "Bonjour, je m'intéresse à un véhicule",
    "INSURANCE_BROKER": "Bonjour, j'ai une question sur mon assurance",
    "REAL_ESTATE": "Bonjour, j'ai un projet immobilier",  # lot 79
    "RESTAURANT": "Bonjour, je voudrais commander ou réserver une table",  # lot 106
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


async def follow_sector(db, tenant, previous: str | None) -> int:
    """Lot 105 a — la boutique change de secteur : le message du kit suit le nouveau secteur, sauf s'il a été écrit par
    le commerçant (« Bonjour, j'ai une question sur mon assurance » restait sur l'affiche d'une agence, 03/11)."""
    from app.models.contact_point import ContactPoint
    from app.services.business_type import normalize

    old, new = GREETINGS.get(normalize(previous)), GREETINGS.get(_sector(tenant), GREETINGS["ONLINE_STORE"])
    if old is None or old == new:
        return 0
    points = (await db.execute(select(ContactPoint).where(
        ContactPoint.tenant_id == tenant.id, ContactPoint.code.startswith(KIT_CODE_PREFIX)))).scalars().all()
    changed = 0
    for point in points:
        if " ".join((point.greeting or "").split()) == old:
            point.greeting, changed = new, changed + 1
    return changed


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
    "REAL_ESTATE": ("Un projet immobilier ?", "Achat, location, estimation de votre bien"),  # lot 79
    "RESTAURANT": ("Une petite faim ?", "Commandes à emporter, livraison et réservation de table"),  # lot 106
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
