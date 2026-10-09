"""
Lot 53 — demandes de cotation (courtier / agent d'assurance) : la liste du cabinet, et « Prise en charge ».
Réservé au courtier (verrou côté serveur), et toujours limité aux demandes de SA boutique.
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.customer import Customer
from app.models.quote_request import QuoteRequest
from app.services import insurance
from app.services.audit import log_audit_event
from app.services.business_type import only_insurance

router = APIRouter(prefix="/api/v1/quote-requests", tags=["quote-requests"], dependencies=[Depends(only_insurance())])

# Lot 55 : suivi complet — reçues, en cotation, proposition envoyée, terminées (souscrit / perdu).
VIEWS = {"todo": (insurance.STATUS_SUBMITTED,), "handled": (insurance.STATUS_HANDLED,),
         "proposal": (insurance.STATUS_PROPOSAL,), "closed": (insurance.STATUS_WON, insurance.STATUS_LOST),
         "draft": (insurance.STATUS_DRAFT,), "all": tuple(insurance.STATUS_LABELS)}


class AdvanceIn(BaseModel):
    status: str
    lost_reason: str | None = Field(None, max_length=insurance.LOST_REASON_MAX)


def _out(request: QuoteRequest, customer: Customer | None, prospect: dict | None = None) -> dict:
    from app.services.handoff_service import customer_display_name

    return {
        "id": str(request.id),
        "customer": customer_display_name(customer) if customer else "Client",
        "conversation_id": str(request.conversation_id) if request.conversation_id else None,
        "branch": request.branch,
        "branch_label": insurance.branch_label(request.branch),
        "client_type": request.client_type,
        "lines": insurance.request_lines(request)[2:],  # assurance et type de client : déjà dans leurs colonnes
        # Lot 54 (CIMA) : étapes horodatées, et score du prospect (règles fixes, expliquées).
        "trace": insurance.trace_lines(request),
        "consent_at": request.consent_at,
        "prospect": {k: prospect[k] for k in ("score", "score_label", "reasons")} if prospect else None,
        "status": request.status,
        "status_label": insurance.STATUS_LABELS.get(request.status, request.status),
        "submitted_at": request.submitted_at,
        "handled_at": request.handled_at,
        "proposal_sent_at": request.proposal_sent_at,
        "closed_at": request.closed_at,
        "lost_reason": request.lost_reason,
        "next_statuses": list(insurance.TRANSITIONS.get(request.status, ())),
        "customer_id": str(request.customer_id),
        "created_at": request.created_at,
    }


@router.get("")
async def list_quote_requests(
    view: str = Query("todo"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    if view not in VIEWS:
        raise HTTPException(status_code=422, detail="Vue inconnue : " + ", ".join(VIEWS))
    rows = (await db.execute(
        select(QuoteRequest, Customer).join(Customer, Customer.id == QuoteRequest.customer_id)
        .where(QuoteRequest.tenant_id == current_user.tenant_id, Customer.tenant_id == current_user.tenant_id,
               QuoteRequest.status.in_(VIEWS[view]))
        .order_by(QuoteRequest.created_at.desc()).limit(200)
    )).all()
    from app.models.tenant import Tenant
    from app.services import insurance_prospect

    prospects = await insurance_prospect.for_customers(
        db, await db.get(Tenant, current_user.tenant_id), {customer.id for _, customer in rows})
    return [_out(request, customer, prospects.get(customer.id)) for request, customer in rows]


@router.post("/{request_id}/handle")
async def handle_quote_request(
    request_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    request = (await db.execute(select(QuoteRequest).where(
        QuoteRequest.id == request_id, QuoteRequest.tenant_id == current_user.tenant_id,
    ))).scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="Demande introuvable")
    if request.status != insurance.STATUS_SUBMITTED:
        raise HTTPException(status_code=409, detail="Cette demande n'est pas à traiter")
    request.status = insurance.STATUS_HANDLED
    request.handled_at = datetime.now(timezone.utc)
    request.handled_by = str(current_user.user_id)
    await log_audit_event(db, actor=str(current_user.user_id), action="QUOTE_REQUEST_HANDLED",
                          tenant_id=current_user.tenant_id, details={"quote_request_id": str(request.id)})
    await db.commit()
    from app.models.tenant import Tenant
    from app.services import insurance_prospect

    prospects = await insurance_prospect.for_customers(db, await db.get(Tenant, current_user.tenant_id), [request.customer_id])
    return _out(request, await db.get(Customer, request.customer_id), prospects.get(request.customer_id))


@router.post("/{request_id}/status", dependencies=[Depends(require_role("AGENT"))])
async def advance_quote_request(
    request_id: UUID,
    payload: AdvanceIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lot 55 — en cotation → proposition envoyée → souscrit / perdu (étapes horodatées)."""
    request = (await db.execute(select(QuoteRequest).where(
        QuoteRequest.id == request_id, QuoteRequest.tenant_id == current_user.tenant_id,
    ))).scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="Demande introuvable")
    if payload.status == insurance.STATUS_LOST and not (payload.lost_reason or "").strip():
        raise HTTPException(status_code=422, detail="Indiquez en quelques mots pourquoi la demande est perdue")
    try:
        insurance.advance(request, payload.status, current_user.user_id, lost_reason=payload.lost_reason)
    except insurance.TransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="QUOTE_REQUEST_STATUS",
                          tenant_id=current_user.tenant_id,
                          details={"quote_request_id": str(request.id), "status": request.status})
    await db.commit()
    from app.models.tenant import Tenant
    from app.services import insurance_prospect
    from app.services.notifications import queue_check

    queue_check(current_user.tenant_id)
    prospects = await insurance_prospect.for_customers(db, await db.get(Tenant, current_user.tenant_id), [request.customer_id])
    return _out(request, await db.get(Customer, request.customer_id), prospects.get(request.customer_id))
