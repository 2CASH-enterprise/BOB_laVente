"""
Lot 60 — page « Rapports » : le rapport mensuel tel qu'il part par email (aperçu sur 30 ou 90 jours, ou
depuis le dernier envoi), sa date d'envoi, et « Recevoir le rapport maintenant » (à soi seul, pour essayer).
Réservé au propriétaire et aux administrateurs : le rapport contient les résultats par commercial.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import ROLE_HIERARCHY, CurrentUser, get_current_user, require_role
from app.models.audit_log import AuditLog
from app.models.tenant import Tenant
from app.models.user import User
from app.services import monthly_report
from app.services.audit import log_audit_event

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

TEST_SENDS_PER_HOUR = 3


def _first_name(user: User | None) -> str | None:
    return (user.full_name or "").split()[0] if user and (user.full_name or "").strip() else None


@router.get("/info")
async def report_info(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await db.get(Tenant, current_user.tenant_id)
    allowed = ROLE_HIERARCHY.get(current_user.role, -1) >= ROLE_HIERARCHY["ADMIN"]
    now = datetime.now(timezone.utc)
    next_on = monthly_report.next_send_on(tenant, now)
    return {
        "can_view": allowed,
        "next_send_on": next_on.isoformat() if next_on else None,
        "last_sent_at": tenant.report_sent_at.isoformat() if tenant.report_sent_at else None,
        "recipients": [email for email, _ in await monthly_report.recipients(db, tenant)] if allowed else [],
        "demo": bool(tenant.is_demo),
    }


@router.get("/preview", response_class=HTMLResponse, dependencies=[Depends(require_role("ADMIN"))])
async def report_preview(
    period: str = Query(default="30", pattern="^(30|90|since_last)$"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    tenant = await db.get(Tenant, current_user.tenant_id)
    now = datetime.now(timezone.utc)
    if period == "since_last":
        start, end = monthly_report.report_window(tenant, now)
    else:
        start, end = monthly_report.days_window(tenant, now, int(period), include_today=True)
    data = await monthly_report.build(db, tenant, start, end)
    _, _, html = monthly_report.render(data, _first_name(await db.get(User, current_user.user_id)))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.post("/send-test", dependencies=[Depends(require_role("ADMIN"))])
async def send_test(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Le rapport tel qu'il partirait aujourd'hui, à soi seul (n'avance pas la date du prochain envoi)."""
    tenant = await db.get(Tenant, current_user.tenant_id)
    user = await db.get(User, current_user.user_id)
    recent = (await db.execute(select(func.count(AuditLog.id)).where(
        AuditLog.tenant_id == tenant.id, AuditLog.action == "REPORT_TEST_SENT",
        AuditLog.created_at >= datetime.now(timezone.utc) - timedelta(hours=1)))).scalar_one()
    if recent >= TEST_SENDS_PER_HOUR:
        raise HTTPException(status_code=429, detail="Vous avez déjà reçu 3 rapports d'essai cette heure-ci : réessayez plus tard")
    sent = await monthly_report.send_report(db, tenant, only_to=(user.email, _first_name(user)))
    if not sent:
        raise HTTPException(status_code=502, detail="L'email n'a pas pu partir pour le moment : réessayez dans quelques minutes")
    await log_audit_event(db, actor=str(current_user.user_id), action="REPORT_TEST_SENT", tenant_id=tenant.id, details={})
    await db.commit()
    return {"sent_to": user.email}
