"""
Lot 68 — prévenir le client quand la boutique fait avancer la livraison (page Commandes) : « en route » quand la
commande est remise au livreur ou en route, puis « livrée ». Une seule fois par étape. Texte fixe (celui des
modèles validés le 11/10). Canal : WhatsApp si le client a écrit dans les 20 h ; sinon le modèle approuvé par
Meta ; sinon email s'il a donné son adresse (message de service) ; sinon personne n'est prévenu.
"""
import asyncio
from datetime import datetime, timezone

from app.models.delivery import DeliveryStatus

SHIPPED = {DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT}
STEP_KEYS = {"SHIPPED": "livraison_expediee", "DELIVERED": "livraison_livree"}
NO_DATE = "dans les prochains jours"


def step_due(delivery, order=None, tenant=None) -> str | None:
    status = DeliveryStatus(delivery.status) if delivery.status else None
    if status == DeliveryStatus.READY:  # lot 108 : restaurant, commande prête
        return "READY" if delivery.ready_notified_at is None else None
    restaurant = tenant is not None and _is_restaurant(tenant)
    if restaurant and order is not None and order.fulfillment != "DELIVERY":
        return None  # à emporter ou sur place : « Prête » suffit (ni « en route » ni « livrée »)
    if status in SHIPPED and delivery.shipped_notified_at is None:
        return "SHIPPED"
    if status == DeliveryStatus.DELIVERED and delivery.delivered_notified_at is None:
        return "DELIVERED"
    return None


def _is_restaurant(tenant) -> bool:
    from app.services.business_type import is_restaurant

    return is_restaurant(tenant)


READY_TEXT = {
    "PICKUP": ("Bonjour {name}, votre commande {ref} chez {shop} est prête : vous pouvez venir la chercher.",
               "Bonjour {name}, ta commande {ref} chez {shop} est prête : tu peux venir la chercher."),
    "DINE_IN": ("Bonjour {name}, votre commande {ref} est prête : on vous l'apporte{table}.",
                "Bonjour {name}, ta commande {ref} est prête : on te l'apporte{table}."),
    "DELIVERY": ("Bonjour {name}, votre commande {ref} chez {shop} est prête : elle part bientôt en livraison.",
                 "Bonjour {name}, ta commande {ref} chez {shop} est prête : elle part bientôt en livraison."),
}


def ready_text(tenant, customer, order, tu: bool) -> str:
    """Lot 108 — texte fixe « Votre commande est prête », selon le mode (jamais rédigé par l'IA)."""
    from app.services.order_emails import _ref
    from app.services.wa_templates import first_name

    vous, toi = READY_TEXT.get(order.fulfillment or "PICKUP", READY_TEXT["PICKUP"])
    table = f" à la table {order.table_number}" if getattr(order, "table_number", None) else ""
    return (toi if tu else vous).format(name=first_name(customer), ref=_ref(order), shop=tenant.name, table=table)


def values(step: str, tenant, customer, order, delivery) -> list[str]:
    from app.services.calendar_check import label
    from app.services.order_emails import _ref
    from app.services.wa_templates import first_name

    base = [first_name(customer), _ref(order), tenant.name]
    if step == "SHIPPED":
        base.append(label(delivery.estimated_delivery) if delivery.estimated_delivery else NO_DATE)
    return base


async def notify(db, tenant, order, delivery, user_id: str, now: datetime | None = None, send_email=None) -> dict:
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.services import wa_templates
    from app.services.appointment_service import send_fixed_message
    from app.services.human_reply import customer_window_open

    step = step_due(delivery, order, tenant)
    if step is None:
        return {"step": None, "channel": None}
    now = now or datetime.now(timezone.utc)
    if step == "READY":
        return await _notify_ready(db, tenant, order, delivery, user_id, now, send_email)
    key = STEP_KEYS[step]
    customer = await db.get(Customer, order.customer_id)
    conversation = await db.get(Conversation, order.conversation_id) if order.conversation_id else None
    vals = values(step, tenant, customer, order, delivery)
    text = wa_templates.render(key, wa_templates.form_of(tenant), vals)
    channel = None
    if customer is not None and conversation is not None and await customer_window_open(db, tenant.id, customer.id, now):
        from app.services import followups  # lot 99 : « Suivre ma commande », « Pas reçue », « Racheter »…

        sent, _ = await send_fixed_message(db, tenant, conversation, customer, text, user_id, f"delivery_{step.lower()}",
                                           buttons=followups.delivery_buttons(tenant, order, step))
        channel = "WHATSAPP" if sent else None
    if channel is None and customer is not None and await wa_templates.send(
            db, tenant, customer, conversation, key, vals, message_type=f"delivery_{step.lower()}"):
        channel = "WHATSAPP"
    if channel is None and customer is not None and customer.email:
        from app.services.address_form import uses_tu
        from app.services.email_layout import customer_footer

        if send_email is None:
            from app.services.email_service import send_email
        tu = uses_tu(tenant)
        body = text.replace("Réponds simplement à ce message.", "Réponds simplement à cet email.") \
                   .replace("Répondez simplement à ce message.", "Répondez simplement à cet email.")
        subject = (f"{'Ta' if tu else 'Votre'} commande {vals[1]} " + ("est en route" if step == "SHIPPED" else "est livrée")
                   + f" — {tenant.name}")
        mail = {"to": customer.email, "subject": subject, "body": body + customer_footer(tenant.name, not tenant.is_paid, tu=tu),
                "from_name": tenant.name, "reply_to": tenant.email}
        if await asyncio.to_thread(send_email, **mail):
            channel = "EMAIL"
    if channel is not None:
        if step == "SHIPPED":
            delivery.shipped_notified_at = now
        else:
            delivery.delivered_notified_at = now
    return {"step": step, "channel": channel}


async def _notify_ready(db, tenant, order, delivery, user_id, now, send_email) -> dict:
    """Lot 108 — « Votre commande est prête » : WhatsApp si la conversation est ouverte, sinon email, sinon personne."""
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.services.address_form import uses_tu
    from app.services.appointment_service import send_fixed_message
    from app.services.human_reply import customer_window_open

    customer = await db.get(Customer, order.customer_id)
    conversation = await db.get(Conversation, order.conversation_id) if order.conversation_id else None
    tu = uses_tu(tenant)
    text = ready_text(tenant, customer, order, tu)
    channel = None
    if customer is not None and conversation is not None and await customer_window_open(db, tenant.id, customer.id, now):
        from app.services.guided_flow import PREFIX, applies

        buttons = [(f"{PREFIX}s", "Suivre ma commande"), (f"{PREFIX}human", "Parler au restaurant")] if applies(tenant) else None
        sent, _ = await send_fixed_message(db, tenant, conversation, customer, text, user_id, "delivery_ready",
                                           **({"buttons": buttons} if buttons else {}))
        channel = "WHATSAPP" if sent else None
    if channel is None and customer is not None and customer.email:
        from app.services.email_layout import customer_footer
        from app.services.order_emails import _ref

        if send_email is None:
            from app.services.email_service import send_email
        mail = {"to": customer.email, "subject": f"{'Ta' if tu else 'Votre'} commande {_ref(order)} est prête — {tenant.name}",
                "body": text + customer_footer(tenant.name, not tenant.is_paid, tu=tu), "from_name": tenant.name,
                "reply_to": tenant.email}
        if await asyncio.to_thread(send_email, **mail):
            channel = "EMAIL"
    if channel is not None:
        delivery.ready_notified_at = now
    return {"step": "READY", "channel": channel}
