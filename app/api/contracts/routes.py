"""
Lot 55 — registre des contrats du courtier / agent d'assurance (page « Contrats »).

Réservé au courtier (verrou côté serveur), toujours limité aux contrats de SA boutique. Tout le monde voit
les contrats et peut « Prendre en charge » une échéance ; ajouter, modifier, importer, renouveler et changer
un statut demandent le rôle Manager. La prime n'est vue et saisie que par les administrateurs (choix 4A).
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import ROLE_HIERARCHY, CurrentUser, get_current_user, require_role
from app.models.customer import Customer
from app.models.insurance_contract import InsuranceContract
from app.models.tenant import Tenant
from app.services import insurance, insurance_contracts as contracts
from app.services.audit import log_audit_event
from app.services.business_type import only_insurance

router = APIRouter(prefix="/api/v1/contracts", tags=["contracts"], dependencies=[Depends(only_insurance())])

MAX_CSV_SIZE_BYTES = 2 * 1024 * 1024


def _is_admin(user: CurrentUser) -> bool:
    return ROLE_HIERARCHY.get(user.role, -1) >= ROLE_HIERARCHY["ADMIN"]


class ContractIn(BaseModel):
    customer_id: UUID | None = None
    phone: str | None = Field(None, max_length=40)
    first_name: str | None = Field(None, max_length=255)
    last_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=255)
    branch: str | None = Field(None, max_length=60)
    insurer: str | None = Field(None, max_length=150)
    policy_number: str | None = Field(None, max_length=64)
    premium: str | float | None = None
    currency: str | None = Field(None, max_length=3)
    effective_on: str | None = Field(None, max_length=10)
    expires_on: str | None = Field(None, max_length=10)
    term: str | None = Field(None, max_length=20)
    note: str | None = Field(None, max_length=500)
    quote_request_id: UUID | None = None


class StatusIn(BaseModel):
    status: str


class RenewIn(BaseModel):
    expires_on: str | None = Field(None, max_length=10)
    term: str | None = Field(None, max_length=20)
    insurer: str | None = Field(None, max_length=150)
    policy_number: str | None = Field(None, max_length=64)
    premium: str | float | None = None
    currency: str | None = Field(None, max_length=3)


class SettingsIn(BaseModel):
    renewal_reminders_enabled: bool


async def _tenant(db, user: CurrentUser) -> Tenant:
    return await db.get(Tenant, user.tenant_id)


async def _contract(db, user: CurrentUser, contract_id: UUID) -> InsuranceContract:
    contract = await db.get(InsuranceContract, contract_id)
    if contract is None or contract.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Contrat introuvable")
    return contract


async def _out(db, tenant, contract, user: CurrentUser) -> dict:
    from sqlalchemy import select

    from app.models.conversation import Conversation

    customer = await db.get(Customer, contract.customer_id)
    conversation_id = (await db.execute(select(Conversation.id).where(
        Conversation.tenant_id == tenant.id, Conversation.customer_id == contract.customer_id,
    ).order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()
    return contracts.serialize(contract, customer, contracts.today_for(tenant), _is_admin(user), conversation_id)


def _fields(payload: BaseModel) -> dict:
    """Seulement les champs envoyés : un champ absent ne change rien."""
    return payload.model_dump(exclude_unset=True)


@router.get("")
async def list_contracts(
    view: str = Query("all"),
    branch: str | None = Query(None, max_length=32),
    q: str | None = Query(None, max_length=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if view not in contracts.VIEWS:
        raise HTTPException(status_code=422, detail="Vue inconnue : " + ", ".join(contracts.VIEWS))
    tenant = await _tenant(db, current_user)
    rows, today = await contracts.list_contracts(db, tenant, view, branch or None, q or None)
    admin = _is_admin(current_user)
    return {
        "contracts": [contracts.serialize(c, cu, today, admin, conv) for c, cu, conv in rows],
        "summary": await contracts.summary(db, tenant),
        "premium_visible": admin,
        "renewal_reminders_enabled": tenant.renewal_reminders_enabled,
        "limit": contracts.LIST_LIMIT,
        "currency": tenant.currency,
        # Listes du formulaire (une seule source : le serveur)
        "branches": [{"value": code, "label": entry[0]} for code, entry in insurance.BRANCHES.items()],
        "terms": [{"value": code, "label": label} for code, label in insurance.TERMS.items()],
        "statuses": [{"value": code, "label": contracts.STATUS_LABELS[code]} for code in contracts.MANUAL_STATUSES],
    }


@router.get("/export.xlsx", dependencies=[Depends(require_role("AGENT"))])
async def export_contracts(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Response:
    """Lot 58 (CIMA, art. 4, 14) : le registre complet, tous statuts ; la prime pour un administrateur seulement.
    Lot 59 : en Excel."""
    from app.services.insurance_exports import contracts_xlsx
    from app.services.spreadsheet import XLSX_MEDIA

    tenant = await _tenant(db, current_user)
    content = await contracts_xlsx(db, tenant, _is_admin(current_user))
    await log_audit_event(db, actor=str(current_user.user_id), action="CONTRACTS_EXPORTED", tenant_id=tenant.id, details={})
    await db.commit()
    return Response(content, media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": 'attachment; filename="registre_contrats.xlsx"'})


@router.get("/template.csv")
async def csv_template(current_user: CurrentUser = Depends(get_current_user)) -> Response:
    return Response(contracts.CSV_TEMPLATE.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="modele_contrats.csv"'})


@router.put("/settings", dependencies=[Depends(require_role("ADMIN"))])
async def update_settings(
    payload: SettingsIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await _tenant(db, current_user)
    tenant.renewal_reminders_enabled = payload.renewal_reminders_enabled
    await log_audit_event(db, actor=str(current_user.user_id), action="RENEWAL_REMINDERS_UPDATED",
                          tenant_id=tenant.id, details={"enabled": payload.renewal_reminders_enabled})
    await db.commit()
    return {"renewal_reminders_enabled": tenant.renewal_reminders_enabled}


@router.post("", status_code=201, dependencies=[Depends(require_role("MANAGER"))])
async def create_contract(
    payload: ContractIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await _tenant(db, current_user)
    try:
        contract = await contracts.create_contract(db, tenant, _fields(payload), _is_admin(current_user))
    except contracts.ContractError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="CONTRACT_CREATED", tenant_id=tenant.id,
                          details={"contract_id": str(contract.id)})
    await db.commit()
    return await _out(db, tenant, contract, current_user)


@router.patch("/{contract_id}", dependencies=[Depends(require_role("MANAGER"))])
async def update_contract(
    contract_id: UUID,
    payload: ContractIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await _tenant(db, current_user)
    contract = await _contract(db, current_user, contract_id)
    try:
        contracts.update_contract(contract, _fields(payload), _is_admin(current_user), tenant)
    except contracts.ContractError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="CONTRACT_UPDATED", tenant_id=tenant.id,
                          details={"contract_id": str(contract.id)})
    await db.commit()
    return await _out(db, tenant, contract, current_user)


@router.post("/{contract_id}/status", dependencies=[Depends(require_role("MANAGER"))])
async def set_status(
    contract_id: UUID,
    payload: StatusIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await _tenant(db, current_user)
    contract = await _contract(db, current_user, contract_id)
    try:
        contracts.set_status(contract, payload.status)
    except contracts.ContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="CONTRACT_STATUS", tenant_id=tenant.id,
                          details={"contract_id": str(contract.id), "status": payload.status})
    await db.commit()
    return await _out(db, tenant, contract, current_user)


@router.post("/{contract_id}/renew", status_code=201, dependencies=[Depends(require_role("MANAGER"))])
async def renew_contract(
    contract_id: UUID,
    payload: RenewIn,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await _tenant(db, current_user)
    contract = await _contract(db, current_user, contract_id)
    try:
        successor = await contracts.renew_contract(db, tenant, contract, _fields(payload), _is_admin(current_user))
    except contracts.ContractError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="CONTRACT_RENEWED", tenant_id=tenant.id,
                          details={"contract_id": str(contract.id), "successor_id": str(successor.id)})
    await db.commit()
    return await _out(db, tenant, successor, current_user)


@router.post("/{contract_id}/handle", dependencies=[Depends(require_role("AGENT"))])
async def handle_renewal(
    contract_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await _tenant(db, current_user)
    contract = await _contract(db, current_user, contract_id)
    try:
        contracts.handle_renewal(contract, current_user.user_id)
    except contracts.ContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="CONTRACT_RENEWAL_HANDLED",
                          tenant_id=tenant.id, details={"contract_id": str(contract.id)})
    await db.commit()
    from app.services.notifications import queue_check

    queue_check(tenant.id)
    return await _out(db, tenant, contract, current_user)


@router.post("/import-csv", dependencies=[Depends(require_role("MANAGER"))])
async def import_csv(
    file: UploadFile,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    name = (file.filename or "").lower()
    if not name.endswith((".csv", ".xlsx")):
        raise HTTPException(status_code=400, detail="Format accepté : Excel (.xlsx) ou CSV")
    raw = await file.read()
    if len(raw) > MAX_CSV_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (2 Mo au plus)")
    if name.endswith(".xlsx"):  # lot 59 : le fichier Excel tel quel (première feuille)
        from app.services.spreadsheet import SpreadsheetError, read_table, to_csv_text

        try:
            table = read_table(raw, name)
        except SpreadsheetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        content = to_csv_text(table["header"], table["rows"])
    else:
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = raw.decode("cp1252", errors="replace")  # Excel français enregistre souvent en Windows-1252
    tenant = await _tenant(db, current_user)
    try:
        report = await contracts.import_csv(db, tenant, content, _is_admin(current_user))
    except contracts.ContractError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await log_audit_event(db, actor=str(current_user.user_id), action="CONTRACTS_IMPORTED", tenant_id=tenant.id,
                          details={k: report[k] for k in ("created", "updated", "skipped", "customers_created")})
    await db.commit()
    from app.services.notifications import queue_check

    queue_check(tenant.id)
    return report
