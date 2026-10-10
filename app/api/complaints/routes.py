"""
Lot 57 — registre des réclamations et des sinistres du courtier (règlement CIMA 01-24, art. 11).

Réservé au courtier (verrou côté serveur), toujours limité à SA boutique. Tout le monde (agent et plus) peut
saisir une réclamation et la faire avancer ; seul un administrateur règle le délai de réponse annoncé.
Rien n'est jamais supprimé : la traçabilité des réclamations reçues et traitées est une obligation.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.customer import Customer
from app.models.insurance_complaint import InsuranceComplaint
from app.models.tenant import Tenant
from app.services import insurance_complaints as complaints
from app.services.audit import log_audit_event
from app.services.business_type import only_insurance

router = APIRouter(prefix="/api/v1/complaints", tags=["complaints"], dependencies=[Depends(only_insurance())])


class ComplaintIn(BaseModel):
    customer_id: UUID | None = None
    phone: str | None = Field(None, max_length=40)
    first_name: str | None = Field(None, max_length=255)
    last_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=255)
    kind: str = Field("RECLAMATION", max_length=16)
    channel: str = Field("PHONE", max_length=16)
    subject: str = Field(..., max_length=complaints.SUBJECT_MAX)
    received_on: str | None = Field(None, max_length=10)


class ResolveIn(BaseModel):
    resolution: str = Field(..., max_length=complaints.RESOLUTION_MAX)


class KindIn(BaseModel):
    kind: str = Field(..., max_length=16)


class SettingsIn(BaseModel):
    complaint_delay_days: int = Field(..., ge=complaints.DELAY_MIN, le=complaints.DELAY_MAX)


async def _complaint(db, user: CurrentUser, complaint_id: UUID) -> InsuranceComplaint:
    complaint = await db.get(InsuranceComplaint, complaint_id)
    if complaint is None or complaint.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Réclamation introuvable")
    return complaint


async def _out(db, tenant, complaint) -> dict:
    return complaints.serialize(complaint, await db.get(Customer, complaint.customer_id), complaints.local_now(tenant).date())


@router.get("")
async def list_complaints(
    view: str = Query("todo"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if view not in (*complaints.VIEWS, "late"):
        raise HTTPException(status_code=422, detail="Vue inconnue")
    tenant = await db.get(Tenant, current_user.tenant_id)
    rows, today = await complaints.list_complaints(db, tenant, view)
    return {
        "complaints": [complaints.serialize(c, customer, today) for c, customer in rows],
        "counts": await complaints.counts(db, tenant),
        "complaint_delay_days": tenant.complaint_delay_days,
        "complaints_contact": tenant.complaints_contact,
        "kinds": [{"value": k, "label": v} for k, v in complaints.KINDS.items()],
        "channels": [{"value": k, "label": v} for k, v in complaints.CHANNELS.items() if k != "WHATSAPP"],
        "limit": complaints.LIST_LIMIT,
    }


@router.get("/export.xlsx")
async def export_registry(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Response:
    tenant = await db.get(Tenant, current_user.tenant_id)
    content = await complaints.export_xlsx(db, tenant)
    await log_audit_event(db, actor=str(current_user.user_id), action="COMPLAINTS_EXPORTED", tenant_id=tenant.id, details={})
    await db.commit()
    from app.services.spreadsheet import XLSX_MEDIA

    return Response(content, media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": 'attachment; filename="registre_reclamations.xlsx"'})


@router.put("/settings", dependencies=[Depends(require_role("ADMIN"))])
async def update_settings(
    payload: SettingsIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Le nouveau délai vaut pour les réclamations reçues ensuite (les dates limites annoncées ne bougent pas)."""
    tenant = await db.get(Tenant, current_user.tenant_id)
    tenant.complaint_delay_days = payload.complaint_delay_days
    await log_audit_event(db, actor=str(current_user.user_id), action="COMPLAINT_DELAY_UPDATED", tenant_id=tenant.id,
                          details={"days": payload.complaint_delay_days})
    await db.commit()
    return {"complaint_delay_days": tenant.complaint_delay_days}


@router.post("", status_code=201, dependencies=[Depends(require_role("AGENT"))])
async def create_complaint(
    payload: ComplaintIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    try:
        complaint = await complaints.create_manual(db, tenant, payload.model_dump(), current_user.user_id)
    except complaints.ComplaintError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="COMPLAINT_CREATED", tenant_id=tenant.id,
                          details={"complaint_id": str(complaint.id), "reference": complaint.reference})
    await db.commit()
    return await _out(db, tenant, complaint)


async def _step(db, current_user, complaint_id, action, apply) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    complaint = await _complaint(db, current_user, complaint_id)
    try:
        apply(tenant, complaint)
    except complaints.ComplaintError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action=action, tenant_id=tenant.id,
                          details={"complaint_id": str(complaint.id), "reference": complaint.reference})
    await db.commit()
    from app.services.notifications import queue_check

    queue_check(tenant.id)
    return await _out(db, tenant, complaint)


@router.post("/{complaint_id}/start", dependencies=[Depends(require_role("AGENT"))])
async def start_complaint(complaint_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)) -> dict:
    return await _step(db, current_user, complaint_id, "COMPLAINT_STARTED",
                       lambda tenant, c: complaints.start(c, current_user.user_id))


@router.post("/{complaint_id}/resolve", dependencies=[Depends(require_role("AGENT"))])
async def resolve_complaint(complaint_id: UUID, payload: ResolveIn, current_user: CurrentUser = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)) -> dict:
    if len(payload.resolution.strip()) < 5:
        raise HTTPException(status_code=422, detail="Indiquez la réponse apportée au client")
    return await _step(db, current_user, complaint_id, "COMPLAINT_RESOLVED",
                       lambda tenant, c: complaints.resolve(c, current_user.user_id, payload.resolution))


@router.post("/{complaint_id}/kind", dependencies=[Depends(require_role("AGENT"))])
async def change_kind(complaint_id: UUID, payload: KindIn, current_user: CurrentUser = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    return await _step(db, current_user, complaint_id, "COMPLAINT_KIND_CHANGED",
                       lambda tenant, c: complaints.change_kind(tenant, c, payload.kind))
