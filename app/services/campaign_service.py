"""
Relances marketing par email (point 3). Le filtre de consentement est appliqué à UN
SEUL endroit (ici) — jamais dupliqué ailleurs, pour qu'il ne puisse jamais être oublié
sur un futur point d'entrée.
"""
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.email_campaign import EmailCampaign
from app.models.tenant import Tenant
from app.models.whatsapp_account import WhatsAppAccount
from app.services.email_service import send_email


async def get_eligible_customers(db: AsyncSession, tenant_id) -> list[Customer]:
    """Email renseigné ET consentement marketing explicitement accordé — jamais l'un sans l'autre."""
    stmt = select(Customer).where(
        Customer.tenant_id == tenant_id,
        Customer.email.is_not(None),
        Customer.marketing_consent.is_(True),
    )
    return list((await db.execute(stmt)).scalars().all())


def _build_email_body(body_text: str, wa_link: str | None) -> str:
    if wa_link:
        return f"{body_text}\n\n👉 Discuter sur WhatsApp : {wa_link}"
    return body_text


async def send_campaign(
    db: AsyncSession, tenant_id, created_by, subject: str, body_text: str, whatsapp_cta_message: str
) -> dict:
    customers = await get_eligible_customers(db, tenant_id)

    account_stmt = select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id)
    account = (await db.execute(account_stmt)).scalar_one_or_none()

    wa_link = None
    if account is not None and account.display_phone_number:
        digits = "".join(ch for ch in account.display_phone_number if ch.isdigit())
        wa_link = f"https://wa.me/{digits}?text={quote(whatsapp_cta_message)}"

    # Le client a une relation avec le COMMERCE, pas avec Bob : le nom affiché est celui du
    # commerce, et ses réponses partent directement chez le commerçant (Reply-To).
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()

    sent_count = 0
    for customer in customers:
        ok = send_email(
            to=customer.email,
            subject=subject,
            body=_build_email_body(body_text, wa_link),
            from_name=tenant.name,
            reply_to=tenant.email,
        )
        if ok:
            sent_count += 1
        # Un échec d'envoi individuel (email invalide, etc.) ne doit jamais interrompre les suivants.

    campaign = EmailCampaign(
        tenant_id=tenant_id, created_by=created_by, subject=subject, body_text=body_text,
        whatsapp_cta_message=whatsapp_cta_message, recipient_count=sent_count,
    )
    db.add(campaign)
    await db.flush()

    return {"campaign_id": campaign.id, "eligible_count": len(customers), "sent_count": sent_count}
