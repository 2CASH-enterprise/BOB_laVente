"""
Lot 83 — demandes reçues par email (portails immobiliers, formulaire du site) : réglages de l'agence, liste, réponse à
valider, envoi, test. Réservé à l'agence immobilière (verrou côté serveur), à partir du plan Indépendant.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.inbound_lead import InboundLead
from app.models.product import Product
from app.models.tenant import Tenant
from app.services import inbound_leads as leads
from app.services.business_type import only_real_estate

router = APIRouter(prefix="/api/v1/leads", tags=["leads"], dependencies=[Depends(only_real_estate())])


class SettingsIn(BaseModel):
    auto_send: bool


class ReplyIn(BaseModel):
    reply_subject: str = Field(min_length=1, max_length=255)
    reply_text: str = Field(min_length=1, max_length=8000)


async def _tenant(db, current_user) -> Tenant:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not leads.available(tenant):
        raise HTTPException(status_code=403, detail=leads.NOT_AVAILABLE)
    return tenant


async def _lead(db, tenant, lead_id) -> InboundLead:
    lead = await db.get(InboundLead, lead_id)
    if lead is None or lead.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Demande introuvable")
    return lead


def _view(lead: InboundLead, products: dict) -> dict:
    product = products.get(lead.product_id)
    return {"id": str(lead.id), "source": lead.source, "source_label": leads.label(lead.source), "status": lead.status,
            "status_label": leads.STATUSES.get(lead.status, lead.status), "subject": lead.subject,
            "contact_name": lead.contact_name, "contact_email": lead.contact_email, "contact_phone": lead.contact_phone,
            "message": lead.message, "reference": lead.reference, "product_name": product.name if product else None,
            "customer_id": str(lead.customer_id) if lead.customer_id else None, "reply_subject": lead.reply_subject,
            "reply_text": lead.reply_text, "reason": lead.reason, "is_test": lead.is_test,
            "received_at": lead.received_at.isoformat() if lead.received_at else None,
            "replied_at": lead.replied_at.isoformat() if lead.replied_at else None, "sent_by": lead.sent_by}


@router.get("/settings")
async def lead_settings(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    if not leads.available(tenant):
        return {"available": False, "message": leads.NOT_AVAILABLE}
    code = await leads.ensure_code(db, tenant)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    recent = (await db.execute(select(InboundLead).where(InboundLead.tenant_id == tenant.id,
                                                         InboundLead.received_at >= since))).scalars().all()
    by_source: dict = {}
    for lead in recent:
        if not lead.is_test:
            by_source[leads.label(lead.source)] = by_source.get(leads.label(lead.source), 0) + 1
    fresh_code = tenant.lead_gmail_code if tenant.lead_gmail_code_at and \
        datetime.now(timezone.utc) - (tenant.lead_gmail_code_at if tenant.lead_gmail_code_at.tzinfo
                                      else tenant.lead_gmail_code_at.replace(tzinfo=timezone.utc)) < timedelta(days=7) else None
    return {"available": True, "configured": leads.configured(), "address": leads.address_for(code),
            "auto_send": tenant.lead_auto_send, "reply_to": tenant.email, "gmail_code": fresh_code,
            "whatsapp_connected": bool(await leads.wa_digits(db, tenant.id)),
            "gmail_code_at": tenant.lead_gmail_code_at.isoformat() if fresh_code else None,
            "by_source": [{"label": k, "count": v} for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])],
            "median_seconds": leads.response_seconds(recent), "month_used": await leads._month_count(db, tenant.id),
            "quota": await leads.quota(db, tenant),
            "portals": [{"label": name, "domains": list(domains)} for code_, (name, domains) in leads.SOURCES.items() if domains]}


@router.put("/settings", dependencies=[Depends(require_role("MANAGER"))])
async def update_lead_settings(payload: SettingsIn, current_user: CurrentUser = Depends(get_current_user),
                               db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    tenant.lead_auto_send = payload.auto_send
    await db.commit()
    return {"auto_send": tenant.lead_auto_send}


@router.get("")
async def list_leads(status: str | None = None, current_user: CurrentUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> list[dict]:
    tenant = await _tenant(db, current_user)
    stmt = select(InboundLead).where(InboundLead.tenant_id == tenant.id)
    if status in leads.STATUSES:
        stmt = stmt.where(InboundLead.status == status)
    rows = (await db.execute(stmt.order_by(InboundLead.received_at.desc()).limit(100))).scalars().all()
    ids = {r.product_id for r in rows if r.product_id}
    products = {p.id: p for p in (await db.execute(select(Product).where(Product.id.in_(ids)))).scalars()} if ids else {}
    return [_view(r, products) for r in rows]


@router.put("/{lead_id}", dependencies=[Depends(require_role("AGENT"))])
async def edit_reply(lead_id: UUID, payload: ReplyIn, current_user: CurrentUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    lead = await _lead(db, tenant, lead_id)
    if lead.status == "SENT":
        raise HTTPException(status_code=409, detail="Cette réponse est déjà partie.")
    lead.reply_subject, lead.reply_text = payload.reply_subject.strip(), payload.reply_text.strip()
    await db.commit()
    return _view(lead, {})


@router.post("/{lead_id}/send", dependencies=[Depends(require_role("AGENT"))])
async def send_reply(lead_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)) -> dict:
    from app.models.user import User

    tenant = await _tenant(db, current_user)
    lead = await _lead(db, tenant, lead_id)
    if lead.status == "SENT":
        raise HTTPException(status_code=409, detail="Cette réponse est déjà partie.")
    if lead.status == "IGNORED":
        raise HTTPException(status_code=409, detail="Demande écartée.")
    user = await db.get(User, current_user.user_id)
    ok, reason = await leads.send(db, lead, tenant, user.email if user else "agent")
    if not ok:
        raise HTTPException(status_code=422, detail=reason)
    return _view(lead, {})


@router.post("/{lead_id}/ignore", dependencies=[Depends(require_role("AGENT"))])
async def ignore_lead(lead_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await _tenant(db, current_user)
    lead = await _lead(db, tenant, lead_id)
    if lead.status == "SENT":
        raise HTTPException(status_code=409, detail="Cette réponse est déjà partie.")
    lead.status = "IGNORED"
    await db.commit()
    return _view(lead, {})


@router.post("/test", dependencies=[Depends(require_role("MANAGER"))])
async def send_test(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Faux contact SeLoger vers l'adresse de l'agence : Bob le lit à la minute suivante et répond à l'agence."""
    import asyncio

    tenant = await _tenant(db, current_user)
    if not leads.configured():
        raise HTTPException(status_code=422, detail="La boîte des demandes n'est pas encore réglée sur le serveur.")
    await leads.ensure_code(db, tenant)
    product = (await db.execute(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True))
                                .order_by(Product.created_at.desc()).limit(1))).scalars().first()
    try:
        await asyncio.to_thread(leads.smtp_send, leads.test_email(tenant, product))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Envoi du test impossible ({type(exc).__name__}).") from None
    return {"sent_to": leads.address_for(tenant.lead_inbox_code)}
