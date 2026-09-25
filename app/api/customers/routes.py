"""Fiche contact CRM (section CRM.9) — pour l'humain qui reprend une conversation, jamais à l'aveugle."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.models.customer import Customer
from app.schemas.crm import CustomerDetail, CustomerSummary, CustomerUpdate
from app.services.crm_service import get_customer_detail, list_customers_with_summary
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/customers", tags=["customers"])


@router.get("", response_model=list[CustomerSummary])
async def list_customers(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_customers_with_summary(db, current_user.tenant_id)
    return [CustomerSummary(**row) for row in rows]


@router.get("/{customer_id}", response_model=CustomerDetail)
async def get_customer(
    customer_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    detail = await get_customer_detail(db, current_user.tenant_id, customer_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Client introuvable")
    return CustomerDetail(**detail)


@router.put("/{customer_id}", response_model=CustomerDetail, dependencies=[Depends(require_role("AGENT"))])
async def update_customer(
    customer_id: UUID,
    payload: CustomerUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Notes/tags/consentement éditables par un humain — jamais par l'IA (section CRM)."""
    from app.services.consent_service import WITHDRAWN_MANUALLY, grant_marketing_consent, withdraw_marketing_consent

    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=404, detail="Client introuvable")

    payload_dict = payload.model_dump(exclude_unset=True)
    if "marketing_consent" in payload_dict:
        wants_consent = payload_dict.pop("marketing_consent")
        # Le dashboard renvoie la case à CHAQUE enregistrement de la fiche (même pour une note) :
        # on n'agit que sur un vrai changement, sinon la trace d'origine serait écrasée, ou un
        # faux « retrait » créé pour un client à qui on n'a jamais rien demandé.
        if wants_consent and not customer.marketing_consent:
            grant_marketing_consent(customer, source="MANUAL")
        elif not wants_consent and customer.marketing_consent:
            withdraw_marketing_consent(customer, source=WITHDRAWN_MANUALLY)

    for field, value in payload_dict.items():
        setattr(customer, field, value)

    await db.commit()

    detail = await get_customer_detail(db, current_user.tenant_id, customer_id)
    return CustomerDetail(**detail)
