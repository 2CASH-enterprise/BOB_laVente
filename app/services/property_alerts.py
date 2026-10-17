"""
Lot 80 — alertes « nouveau bien » et liste des bailleurs en DPE F ou G (agence immobilière ; décisions du 17/10).

Rapprochement : quand l'agence ajoute un bien (ou en baisse le prix), Bob le compare aux projets d'achat et de
location de ses contacts, par des RÈGLES FIXES (aucune IA, aucun coût d'IA) :
  - achat ↔ bien à vendre, location ↔ bien à louer (la fiche du bien doit le dire) ;
  - le projet doit donner au moins un secteur ou un budget, et avoir eu une nouvelle depuis moins de 90 jours ;
  - secteur : un des lieux cités par le client figure dans la ville, le quartier ou le titre du bien ;
  - budget : prix (ou loyer) au plus 5 % au-dessus du budget ; pièces, surface, type, meublé : si les deux le disent.

Envoi, seulement aux contacts qui ont ACCEPTÉ d'être prévenus (CNIL, Meta) :
  1. WhatsApp gratuit si le client a écrit il y a moins de 20 h (texte + carte du bien) ;
  2. sinon email, s'il est connu (avec lien de désinscription) ;
  3. sinon, plan Pro seulement et si l'agence l'a activé, modèle WhatsApp « marketing » (payant).
Jamais deux fois le même bien au même prix au même contact ; 2 alertes par contact et par 7 jours au plus.

Plans : Free — rien ; Indépendant — 30 alertes par mois ; Starter — 50 ; Pro et Entreprise — sans plafond, envoi
automatique au choix (entre 9 h et 20 h, heure de l'agence) et liste des bailleurs en DPE F ou G.
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.models.property_alert import (
    CHANNEL_EMAIL,
    CHANNEL_TEMPLATE,
    CHANNEL_WHATSAPP,
    REASON_NEW,
    REASON_PRICE_DROP,
    PropertyAlert,
)

MONTHLY_QUOTA = {"INDEPENDANT": 30, "STARTER": 50, "PRO": None, "ENTERPRISE": None}
PRO_PLANS = ("PRO", "ENTERPRISE")
PROJECT_MAX_AGE = timedelta(days=90)
WEEKLY_MAX_PER_CONTACT = 2
BUDGET_TOLERANCE = Decimal("1.05")
AUTO_NEW_DAYS = 7          # envoi automatique : biens ajoutés depuis 7 jours au plus (ou dont le prix a baissé)
AUTO_HOURS = (9, 20)       # heure de l'agence
TRANSACTIONS = {"ACHAT": "Vente", "LOCATION": "Location"}
NOT_AVAILABLE = "Les alertes « nouveau bien » sont disponibles à partir du plan Indépendant."
PRO_ONLY = "Disponible avec le plan Pro."
CHANNEL_LABELS = {CHANNEL_WHATSAPP: "WhatsApp (gratuit)", CHANNEL_EMAIL: "Email", CHANNEL_TEMPLATE: "Modèle WhatsApp (payant)"}

DPE_DEADLINES = {
    "G": "Classe G : ne peut plus être loué (nouveau bail ou renouvellement) depuis le 1er janvier 2025",
    "F": "Classe F : ne pourra plus être loué à partir du 1er janvier 2028",
}


def _name(customer) -> str:
    """Prénom et nom du contact ; à défaut, son nom d'affichage (avec le numéro)."""
    from app.services.handoff_service import customer_display_name

    return " ".join(p for p in (customer.first_name, customer.last_name) if p) or customer_display_name(customer)


def _plan(tenant) -> str:
    plan = getattr(tenant, "plan", None)
    return getattr(plan, "value", plan) or "FREE"


def available(tenant) -> bool:
    from app.services.business_type import is_real_estate

    return is_real_estate(tenant) and _plan(tenant) in MONTHLY_QUOTA


def is_pro(tenant) -> bool:
    return _plan(tenant) in PRO_PLANS


def monthly_quota(tenant) -> int | None:
    return MONTHLY_QUOTA.get(_plan(tenant), 0)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.lower()).strip()


# --- Rapprochement --------------------------------------------------------------------------------------------

def budget_value(text: str | None) -> Decimal | None:
    """« 300 k », « 400 000 € », « 1,2 M », « 1 200 €/mois », « entre 250 et 300 k » → le montant le plus haut."""
    plain = _plain(text).replace("€", " ")
    if not plain:
        return None
    plain = re.sub(r"(?<=\d)[ .](?=\d{3}\b)", "", plain)  # « 400 000 » ou « 400.000 » → « 400000 »
    best = None
    for number, unit in re.findall(r"(\d+(?:,\d+)?)\s*(k\b|m\b|millions?\b|mille\b)?", plain):
        value = Decimal(number.replace(",", "."))
        if unit == "k" or unit == "mille":
            value *= 1000
        elif unit:
            value *= 1_000_000
        if value > 0 and (best is None or value > best):
            best = value
    return best


_ZONE_STOP = {"ou", "et", "a", "au", "aux", "de", "du", "des", "la", "le", "les", "sur", "en", "pres", "proche",
              "centre", "ville", "secteur", "quartier", "quartiers", "arrondissement", "nord", "sud", "est", "ouest"}


def _zone_words(zone: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9-]+", _plain(zone)) if len(w) >= 3 and w not in _ZONE_STOP and not w.isdigit()]


def project_active(data: dict, fallback: datetime | None, now: datetime) -> bool:
    stamp = data.get("updated_at")
    try:
        moment = _aware(datetime.fromisoformat(stamp)) if stamp else _aware(fallback)
    except ValueError:
        moment = _aware(fallback)
    return moment is not None and now - moment <= PROJECT_MAX_AGE


def matches(product, kind: str, data: dict) -> bool:
    """Le bien correspond-il à ce projet d'achat ou de location ? Règles fixes, voir l'en-tête du module."""
    from app.services import product_sheets

    sheet = getattr(product, "sheet", None) or {}
    if kind not in TRANSACTIONS or sheet.get("transaction") != TRANSACTIONS[kind]:
        return False
    if not data.get("zone") and not data.get("budget"):
        return False  # projet trop vague pour prévenir quelqu'un
    if data.get("property_type") and getattr(product, "bob_category", None) \
            and product.bob_category != data["property_type"]:
        return False
    if data.get("zone"):
        haystack = _plain(" ".join(str(v) for v in (product.name, sheet.get("city"), sheet.get("district")) if v))
        words = _zone_words(data["zone"])
        if words and not any(w in haystack for w in words):
            return False
    budget = budget_value(data.get("budget"))
    if budget is not None and Decimal(str(product.price)) > budget * BUDGET_TOLERANCE:
        return False
    for need, have in (("rooms_min", "rooms"), ("surface_min", "surface")):
        value = product_sheets.to_number(sheet.get(have))
        if data.get(need) and value is not None and value < float(data[need]):
            return False
    furnished = {"MEUBLE": "Meublé", "VIDE": "Vide"}.get(data.get("furnished"))
    if furnished and sheet.get("furnished") and sheet["furnished"] != furnished:
        return False
    return True


# --- Qui prévenir, et comment ---------------------------------------------------------------------------------

async def _latest_conversation(db, tenant_id, customer_id):
    from app.models.conversation import Conversation

    return (await db.execute(select(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.customer_id == customer_id,
    ).order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()


async def _template_ready(db, tenant) -> bool:
    from app.services import wa_templates

    return is_pro(tenant) and bool(tenant.property_alert_templates) \
        and await wa_templates.approved(db, tenant, "nouveau_bien") is not None


async def candidates(db, tenant, product, now: datetime | None = None) -> list[dict]:
    """Contacts dont le projet correspond au bien, avec le canal prévu ou la raison de ne pas les prévenir."""
    from app.models.appointment_request import AppointmentRequest
    from app.models.customer import Customer
    from app.models.prospect_profile import ProspectProfile
    from app.services.human_reply import customer_window_open
    from app.services.real_estate import _project_line

    now = _aware(now or datetime.now(timezone.utc))
    if not available(tenant) or not product.active or product.stock_quantity <= 0:
        return []
    rows = (await db.execute(select(ProspectProfile, Customer).join(Customer, Customer.id == ProspectProfile.customer_id)
                             .where(ProspectProfile.tenant_id == tenant.id, ProspectProfile.real_estate.is_not(None)))).all()
    template_ready = None
    out = []
    for profile, customer in rows:
        projects = (profile.real_estate or {}).get("projects") or {}
        kind = next((k for k in TRANSACTIONS if k in projects and matches(product, k, projects[k])
                     and project_active(projects[k], profile.updated_at, now)), None)
        if kind is None:
            continue
        item = {"customer_id": str(customer.id), "customer": _name(customer), "customer_obj": customer,
                "project": _project_line(kind, projects[kind]), "channel": None, "blocked": None, "reason": REASON_NEW}
        previous = (await db.execute(select(func.min(PropertyAlert.price)).where(
            PropertyAlert.product_id == product.id, PropertyAlert.customer_id == customer.id))).scalar_one_or_none()
        if previous is not None:
            if Decimal(str(product.price)) >= previous:
                item["blocked"] = "déjà prévenu de ce bien"
            else:
                item["reason"] = REASON_PRICE_DROP
        if item["blocked"] is None and not customer.marketing_consent:
            item["blocked"] = "n'a pas accepté d'être prévenu"
        if item["blocked"] is None and (await db.execute(select(func.count(AppointmentRequest.id)).where(
                AppointmentRequest.customer_id == customer.id, AppointmentRequest.product_id == product.id))).scalar_one():
            item["blocked"] = "visite déjà demandée"
        if item["blocked"] is None and (await db.execute(select(func.count(PropertyAlert.id)).where(
                PropertyAlert.customer_id == customer.id, PropertyAlert.sent_at >= now - timedelta(days=7)))).scalar_one() \
                >= WEEKLY_MAX_PER_CONTACT:
            item["blocked"] = f"déjà {WEEKLY_MAX_PER_CONTACT} alertes cette semaine"
        if item["blocked"] is None:
            conversation = await _latest_conversation(db, tenant.id, customer.id)
            item["conversation"] = conversation
            # Lot 49 : règle unique de la fenêtre de 20 h (toutes conversations du client confondues).
            if conversation is not None and await customer_window_open(db, tenant.id, customer.id, now):
                item["channel"] = CHANNEL_WHATSAPP
            elif customer.email:
                item["channel"] = CHANNEL_EMAIL
            else:
                if template_ready is None:
                    template_ready = await _template_ready(db, tenant)
                if template_ready:
                    item["channel"] = CHANNEL_TEMPLATE
                else:
                    item["blocked"] = ("pas d'email ni de conversation récente" if not is_pro(tenant)
                                       else "pas d'email ni de conversation récente (modèle WhatsApp non activé)")
        out.append(item)
    out.sort(key=lambda i: (i["blocked"] is not None, i["customer"]))
    return out


def public(items: list[dict]) -> list[dict]:
    """Ce que voit l'agent (sans les objets de la base)."""
    return [{"customer_id": i["customer_id"], "customer": i["customer"], "project": i["project"],
             "channel": i["channel"], "channel_label": CHANNEL_LABELS.get(i["channel"]), "blocked": i["blocked"],
             "price_drop": i["reason"] == REASON_PRICE_DROP} for i in items]


async def used_this_month(db, tenant, now: datetime | None = None) -> int:
    from app.services.local_time import tenant_zone

    now = _aware(now or datetime.now(timezone.utc))
    local = now.astimezone(tenant_zone(tenant))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return (await db.execute(select(func.count(PropertyAlert.id)).where(
        PropertyAlert.tenant_id == tenant.id, PropertyAlert.sent_at >= start))).scalar_one()


async def quota_left(db, tenant, now: datetime | None = None) -> int | None:
    quota = monthly_quota(tenant)
    if quota is None:
        return None
    return max(0, quota - await used_this_month(db, tenant, now))


# --- Envoi ---------------------------------------------------------------------------------------------------

def _first_name(customer) -> str:
    return (customer.first_name or "").strip()


def intro(customer, reason: str) -> str:
    hello = f"Bonjour {_first_name(customer)}" if _first_name(customer) else "Bonjour"
    if reason == REASON_PRICE_DROP:
        return f"{hello}, le prix d'un bien qui correspond à votre recherche vient de baisser :"
    return f"{hello}, un nouveau bien correspond à votre recherche :"


def _describe(product) -> str:
    from app.services.real_estate import card_detail

    detail = card_detail(product)
    return f"{product.name} ({detail})" if detail else product.name


async def _send_whatsapp(db, tenant, item: dict, product) -> bool:
    """Message gratuit (fenêtre ouverte) : une phrase puis la carte du bien, sans prix (lot 76)."""
    from app.integrations.whatsapp.client import WhatsAppClient
    from app.models.conversation import Message, MessageSender
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services import product_cards
    from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound
    from app.agents.tools import has_sendable_photo

    from app.services.human_reply import customer_window_open

    customer, conversation = item["customer_obj"], item.get("conversation")
    if not await customer_window_open(db, tenant.id, customer.id):
        return False  # vérifié de nouveau juste avant l'envoi : jamais un message libre hors fenêtre
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()
    if account is None or conversation is None:
        return False
    try:
        await check_and_log_outbound(db, tenant_id=tenant.id, requested_by="BOB",
                                     permission=Permission.CAN_REPLY_TO_CUSTOMER, human_in_control=True)
    except OutboundDenied:
        return False
    text = intro(customer, item["reason"])
    client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
    try:
        if has_sendable_photo(product):
            await client.send_text_message(to=customer.whatsapp_number, body=text)
            await client.send_product_card(to=customer.whatsapp_number, link=product.image_url.strip(),
                                           body=product_cards.card_body(product),
                                           button_id=product_cards.button_id(str(product.id)),
                                           button_title=product_cards.BUTTON_TITLE)
        else:
            await client.send_text_message(to=customer.whatsapp_number, body=f"{text}\n{_describe(product)}")
    except Exception:  # noqa: BLE001 — refus de Meta, réseau : l'alerte n'est pas comptée
        return False
    # Gardé dans la conversation : Bob le relit (le client répond « oui, il m'intéresse »), le tableau de bord l'affiche.
    db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                   message_type="property_alert", content=f"{text}\n{_describe(product)}",
                   message_metadata={"sent_by": "BOB", "product_ids": [str(product.id)]}))
    return True


def _email(tenant, item: dict, product, wa_digits: str | None) -> tuple[str, str]:
    from urllib.parse import quote

    from app.services.email_layout import customer_footer
    from app.services.unsubscribe_service import build_unsubscribe_url

    customer = item["customer_obj"]
    subject = (f"Baisse de prix — {tenant.name}" if item["reason"] == REASON_PRICE_DROP
               else f"Un bien pour votre recherche — {tenant.name}")
    lines = [intro(customer, item["reason"]), "", _describe(product)]
    fees = (getattr(product, "sheet", None) or {}).get("fees")
    if fees:  # loi ALUR : jamais un prix sans les honoraires
        lines.append(f"Prix : {Decimal(str(product.price)):,.0f} {product.currency} — honoraires : {fees}".replace(",", " "))
    if wa_digits:
        lines += ["", f"Il vous intéresse ? Écrivez-nous sur WhatsApp : https://wa.me/{wa_digits}?text="
                      + quote(f"Ça m'intéresse : {product.name}")]
    else:
        lines += ["", "Il vous intéresse ? Répondez simplement à cet email."]
    unsubscribe = build_unsubscribe_url(customer.id)
    why = (f"Vous recevez cet email car vous avez accepté d'être prévenu des biens de {tenant.name} qui correspondent "
           "à votre recherche.")
    body = "\n".join(lines) + customer_footer(tenant.name, not tenant.is_paid, [why, f"Se désinscrire : {unsubscribe}"])
    return subject, body


async def send_for_product(db, tenant, product, sent_by: str, now: datetime | None = None, send_email=None,
                           send_whatsapp=None) -> dict:
    """Envoie l'alerte aux contacts prêts à la recevoir, dans la limite du plafond du plan. Ne lève jamais."""
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services import wa_templates
    from app.services.campaign_service import _unsubscribe_headers
    from app.services.unsubscribe_service import build_unsubscribe_url

    if send_email is None:
        from app.services.email_service import send_email
    send_whatsapp = send_whatsapp or _send_whatsapp
    now = _aware(now or datetime.now(timezone.utc))
    report = {"sent": 0, "by_channel": {CHANNEL_WHATSAPP: 0, CHANNEL_EMAIL: 0, CHANNEL_TEMPLATE: 0},
              "failed": 0, "quota_reached": False}
    if not available(tenant):
        return report
    left = await quota_left(db, tenant, now)
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()
    digits = "".join(ch for ch in (account.display_phone_number or "") if ch.isdigit()) if account else ""
    for item in await candidates(db, tenant, product, now):
        if item["blocked"] is not None or item["channel"] is None:
            continue
        if left is not None and left <= 0:
            report["quota_reached"] = True
            break
        customer, channel = item["customer_obj"], item["channel"]
        if channel == CHANNEL_WHATSAPP:
            ok = await send_whatsapp(db, tenant, item, product)
        elif channel == CHANNEL_EMAIL:
            subject, body = _email(tenant, item, product, digits or None)
            ok = bool(send_email(to=customer.email, subject=subject, body=body, from_name=tenant.name,
                                 reply_to=tenant.email,
                                 extra_headers=_unsubscribe_headers(build_unsubscribe_url(customer.id))))
        else:
            conversation = item.get("conversation") or await _latest_conversation(db, tenant.id, customer.id)
            ok = await wa_templates.send(db, tenant, customer, conversation, "nouveau_bien",
                                         [_first_name(customer) or "cher client", tenant.name, _describe(product)],
                                         message_type="property_alert")
        if not ok:
            report["failed"] += 1
            continue
        db.add(PropertyAlert(tenant_id=tenant.id, product_id=product.id, customer_id=customer.id, channel=channel,
                             reason=item["reason"], price=product.price, sent_by=str(sent_by)[:64], sent_at=now))
        await db.flush()
        report["sent"] += 1
        report["by_channel"][channel] += 1
        if left is not None:
            left -= 1
    await db.commit()
    report["quota_left"] = await quota_left(db, tenant, now)
    return report


async def run_automatic(db, tenant, now: datetime | None = None, send_email=None, send_whatsapp=None) -> int:
    """Plan Pro, envoi automatique : biens ajoutés depuis 7 jours, ou dont le prix a baissé, entre 9 h et 20 h."""
    from app.models.product import Product
    from app.services.local_time import tenant_zone

    now = _aware(now or datetime.now(timezone.utc))
    if not (available(tenant) and is_pro(tenant) and tenant.property_alerts_auto):
        return 0
    if not AUTO_HOURS[0] <= now.astimezone(tenant_zone(tenant)).hour < AUTO_HOURS[1]:
        return 0
    products = (await db.execute(select(Product).where(
        Product.tenant_id == tenant.id, Product.active.is_(True), Product.stock_quantity > 0))).scalars().all()
    sent = 0
    for product in products:
        recent = _aware(product.created_at) is not None and now - _aware(product.created_at) <= timedelta(days=AUTO_NEW_DAYS)
        dropped = (await db.execute(select(func.count(PropertyAlert.id)).where(
            PropertyAlert.product_id == product.id, PropertyAlert.price > product.price))).scalar_one() > 0
        if recent or dropped:
            sent += (await send_for_product(db, tenant, product, "BOB", now, send_email, send_whatsapp))["sent"]
    return sent


async def match_counts(db, tenant, now: datetime | None = None) -> dict:
    """Pour la liste des biens : nombre de contacts qui correspondent, et combien peuvent être prévenus maintenant."""
    from app.models.product import Product

    out = {}
    products = (await db.execute(select(Product).where(
        Product.tenant_id == tenant.id, Product.active.is_(True), Product.stock_quantity > 0))).scalars().all()
    for product in products:
        items = await candidates(db, tenant, product, now)
        if items:
            out[str(product.id)] = {"matches": len(items), "ready": sum(1 for i in items if i["blocked"] is None)}
    return out


# --- Bailleurs en DPE F ou G (Pro) ------------------------------------------------------------------------------

async def dpe_owners(db, tenant) -> list[dict]:
    """Propriétaires (vente ou gestion) dont le logement est classé F ou G : à rappeler, sans envoi automatique."""
    from app.models.customer import Customer
    from app.models.prospect_profile import ProspectProfile
    from app.services.real_estate import _project_line

    rows = (await db.execute(select(ProspectProfile, Customer).join(Customer, Customer.id == ProspectProfile.customer_id)
                             .where(ProspectProfile.tenant_id == tenant.id, ProspectProfile.real_estate.is_not(None)))).all()
    out = []
    for profile, customer in rows:
        projects = (profile.real_estate or {}).get("projects") or {}
        for kind in ("GESTION", "VENTE"):
            dpe = (projects.get(kind) or {}).get("dpe")
            if dpe in DPE_DEADLINES:
                conversation = await _latest_conversation(db, tenant.id, customer.id)
                out.append({"customer_id": str(customer.id), "customer": _name(customer),
                            "phone": f"+{customer.whatsapp_number}" if (customer.whatsapp_number or "").isdigit() else None,
                            "dpe": dpe, "deadline": DPE_DEADLINES[dpe], "project": _project_line(kind, projects[kind]),
                            "conversation_id": str(conversation.id) if conversation else None})
                break
    out.sort(key=lambda o: (o["dpe"] != "G", o["customer"]))
    return out
