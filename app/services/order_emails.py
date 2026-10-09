"""
Lot 40 — récapitulatif de commande et reçu par email au client (boutique en ligne).

- Récapitulatif : Bob le PROPOSE après une commande (« Souhaitez-vous recevoir le récapitulatif de
  votre commande par email ? », lot 34). Dès que le client donne son email, il reçoit le
  récapitulatif de sa commande en cours, une seule fois. « En cours » = créée dans les dernières
  24 h (celle dont Bob vient de parler) : une ancienne commande restée en attente n'est jamais
  récapitulée. L'envoi est réservé en base (mise à jour conditionnelle) : deux messages traités
  en même temps n'envoient jamais deux récapitulatifs.
- Reçu : quand le commerçant clique « Paiement reçu », le reçu part aussi par email si l'email du
  client est connu, une seule fois.
- Textes fixes (jamais rédigés par l'IA), au tutoiement si la boutique l'a choisi (lot 38) ;
  expéditeur affiché = la boutique, réponses vers la boutique (Reply-To).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.models.order import Order, OrderStatus
from app.services.address_form import uses_tu
from app.services.business_type import is_online_store
from app.services.email_layout import customer_footer
from app.services.receipt_service import get_order_item_lines

RECAP_WINDOW = timedelta(hours=24)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _money(amount, currency: str) -> str:
    return f"{float(amount):,.0f} {currency}".replace(",", " ")


def _ref(order: Order) -> str:
    return str(order.id)[:8].upper()


def recap_email(order: Order, item_lines: list[str], tenant) -> tuple[str, str]:
    tu = uses_tu(tenant)
    lines = [
        "Bonjour,",
        "",
        ("Merci pour ta commande ! En voici le récapitulatif." if tu else "Merci pour votre commande ! En voici le récapitulatif."),
        "",
        f"Référence : {_ref(order)}",
        f"Total : {_money(order.total_amount, order.currency)}",
        f"Statut : {'payée' if order.status == OrderStatus.PAID else 'en attente de paiement'}",
        "",
        "Articles :",
        *[f"- {line}" for line in item_lines],
        "",
    ]
    if order.status == OrderStatus.PENDING:
        if tenant.payment_link:
            lines += [f"Payer ma commande : {tenant.payment_link}", ""]
        lines.append("Une fois la capture d'écran du paiement envoyée sur WhatsApp, "
                     + ("tu recevras ton reçu." if tu else "vous recevrez votre reçu."))
        lines.append("")
    lines.append("Une question ? " + ("Réponds simplement à cet email ou écris-nous sur WhatsApp."
                                       if tu else "Répondez simplement à cet email ou écrivez-nous sur WhatsApp."))
    lines += ["", f"À bientôt,\n{tenant.name}"]
    subject = f"{'Ta' if tu else 'Votre'} commande {_ref(order)} chez {tenant.name}"
    return subject, "\n".join(lines) + customer_footer(tenant.name, not tenant.is_paid, tu=tu)


def receipt_email(order: Order, item_lines: list[str], tenant) -> tuple[str, str]:
    tu = uses_tu(tenant)
    paid = _aware(order.paid_at) or datetime.now(timezone.utc)
    lines = [
        "Bonjour,",
        "",
        ("Nous avons bien reçu ton paiement, merci ! Voici ton reçu." if tu
         else "Nous avons bien reçu votre paiement, merci ! Voici votre reçu."),
        "",
        f"Référence : {_ref(order)}",
        f"Date du paiement : {paid.strftime('%d/%m/%Y')}",
        f"Total payé : {_money(order.total_amount, order.currency)}",
        "",
        "Articles :",
        *[f"- {line}" for line in item_lines],
        "",
        "Une question sur ta livraison ? Réponds simplement à cet email ou écris-nous sur WhatsApp." if tu
        else "Une question sur votre livraison ? Répondez simplement à cet email ou écrivez-nous sur WhatsApp.",
        "",
        f"Merci et à bientôt,\n{tenant.name}",
    ]
    subject = f"Reçu de {'ta' if tu else 'votre'} commande {_ref(order)} — {tenant.name}"
    return subject, "\n".join(lines) + customer_footer(tenant.name, not tenant.is_paid, tu=tu)


def _envelope(tenant, customer, subject: str, body: str) -> dict:
    return {"to": customer.email, "subject": subject, "body": body, "from_name": tenant.name, "reply_to": tenant.email}


async def recap_to_send(db, tenant, customer, now: datetime | None = None) -> dict | None:
    """Email prêt à partir (et marqué envoyé), ou None : boutique, email connu, commande récente pas encore récapitulée."""
    if tenant is None or customer is None or not customer.email or not is_online_store(tenant):
        return None
    now = _aware(now or datetime.now(timezone.utc))
    order = (await db.execute(
        select(Order).where(
            Order.tenant_id == tenant.id, Order.customer_id == customer.id,
            Order.status.in_([OrderStatus.PENDING, OrderStatus.PAID]),
            Order.created_at >= now - RECAP_WINDOW,
        ).order_by(Order.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if order is None or order.recap_emailed_at is not None:
        return None
    claimed = await db.execute(
        update(Order).where(Order.id == order.id, Order.recap_emailed_at.is_(None))
        .values(recap_emailed_at=now).execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # un autre traitement l'a déjà envoyé
        return None
    order.recap_emailed_at = now
    subject, body = recap_email(order, await get_order_item_lines(db, order.id, order.currency), tenant)
    return _envelope(tenant, customer, subject, body)


async def receipt_to_send(db, tenant, customer, order: Order, now: datetime | None = None) -> dict | None:
    if tenant is None or customer is None or not customer.email or order.receipt_emailed_at is not None:
        return None
    if order.status != OrderStatus.PAID or order.customer_id != customer.id or order.tenant_id != tenant.id:
        return None
    stamp = _aware(now or datetime.now(timezone.utc))
    claimed = await db.execute(
        update(Order).where(Order.id == order.id, Order.receipt_emailed_at.is_(None))
        .values(receipt_emailed_at=stamp).execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    order.receipt_emailed_at = stamp
    subject, body = receipt_email(order, await get_order_item_lines(db, order.id, order.currency), tenant)
    return _envelope(tenant, customer, subject, body)
