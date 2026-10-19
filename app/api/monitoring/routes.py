"""
Lot 67 — monitoring technique.
- Public : POST /api/v1/monitoring/client-error — erreurs JavaScript des pages (limité par adresse, masqué).
- Public : POST /api/v1/monitoring/csp-report — signalements CSP des navigateurs (lot 73, limité, masqué).
- Super Admin : liste des erreurs regroupées, détail, statut (Je m'en occupe / Corrigée), état du service, tests.
"""
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import CurrentSuperAdmin, get_current_superadmin
from app.models.monitoring import MonitorIssue, MonitorOccurrence
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant
from app.services import monitoring, monitoring_status

public_router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])
router = APIRouter(prefix="/api/v1/superadmin/monitoring", tags=["superadmin"])

LIST_LIMIT = 200
OCCURRENCES_SHOWN = 15
FILTERS = {
    "todo": monitoring.TODO_STATUSES, "new": monitoring.UNREAD_STATUSES, "progress": ("IN_PROGRESS",),
    "resolved": ("RESOLVED",), "all": tuple(monitoring.STATUSES),
}


class ClientError(BaseModel):
    page: str = Field(max_length=20)
    message: str = Field(max_length=2000)
    source: str = Field(default="", max_length=500)
    line: int = 0
    col: int = 0
    stack: str = Field(default="", max_length=6000)
    path: str = Field(default="", max_length=300)


def _tenant_from_header(request: Request):
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        from app.core.security import decode_access_token

        return decode_access_token(header[7:].strip()).tenant_id
    except Exception:  # noqa: BLE001 — jeton absent ou expiré : l'erreur est gardée, sans boutique
        return None


@public_router.post("/client-error", status_code=status.HTTP_202_ACCEPTED)
async def client_error(payload: ClientError, request: Request, rate_limiter: RateLimiter = Depends(get_rate_limiter)) -> dict:
    ip = request.client.host if request.client else "unknown"
    try:
        allowed = await rate_limiter.is_allowed(f"monitor-js:ip:{ip}", limit=20, window_seconds=600) and \
            await rate_limiter.is_allowed("monitor-js:all", limit=300, window_seconds=600)
    except Exception:  # noqa: BLE001
        allowed = False
    if not allowed:
        return {"accepted": False}
    event = monitoring.capture_browser(payload.model_dump(), request.headers.get("user-agent", ""), _tenant_from_header(request))
    return {"accepted": event is not None}


@public_router.post("/csp-report", status_code=status.HTTP_204_NO_CONTENT)
async def csp_report(request: Request, rate_limiter: RateLimiter = Depends(get_rate_limiter)):
    """Lot 73 : le navigateur envoie ici ce que la CSP bloquerait (application/csp-report ou reports+json)."""
    from fastapi import Response
    import json

    body = await request.body()
    ip = request.client.host if request.client else "unknown"
    try:
        allowed = len(body) <= 20000 and await rate_limiter.is_allowed(f"monitor-csp:ip:{ip}", limit=30, window_seconds=600) \
            and await rate_limiter.is_allowed("monitor-csp:all", limit=300, window_seconds=600)
        data = json.loads(body or b"null") if allowed else None
    except Exception:  # noqa: BLE001 — corps illisible ou limiteur indisponible : ignoré
        data = None
    reports = []
    if isinstance(data, dict) and isinstance(data.get("csp-report"), dict):
        reports = [data["csp-report"]]
    elif isinstance(data, list):
        reports = [r.get("body") for r in data[:10] if isinstance(r, dict) and r.get("type") == "csp-violation"
                   and isinstance(r.get("body"), dict)]
    for report in reports:
        monitoring.capture_csp(report, request.headers.get("user-agent", ""))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- Super Admin

async def _me(db, admin: CurrentSuperAdmin) -> str:
    user = await db.get(SuperAdminUser, admin.superadmin_user_id)
    return (user.full_name or user.email) if user else "Super Admin"


async def _names(db, issues) -> dict:
    ids = {UUID(t) for i in issues for t in (i.tenant_ids or [])}
    if not ids:
        return {}
    return {str(t.id): t.name for t in (await db.execute(select(Tenant).where(Tenant.id.in_(ids)))).scalars()}


def _iso(value):
    value = monitoring._utc(value)
    return value.isoformat() if value else None


def _summary(issue: MonitorIssue, names: dict) -> dict:
    shops = [names.get(t, "Boutique supprimée") for t in (issue.tenant_ids or [])]
    return {
        "id": str(issue.id), "source": issue.source, "source_label": monitoring.SOURCES.get(issue.source, issue.source),
        "title": issue.title, "status": issue.status, "status_label": monitoring.STATUSES.get(issue.status, issue.status),
        "count": issue.count, "first_seen": _iso(issue.first_seen), "last_seen": _iso(issue.last_seen),
        "location": issue.location, "where": monitoring.explain(issue)["where"], "shops": shops,
        "browsers": issue.browsers or [], "assignee": issue.assignee, "reopened_count": issue.reopened_count or 0,
    }


async def _counts(db) -> dict:
    rows = dict((await db.execute(select(MonitorIssue.status, func.count()).group_by(MonitorIssue.status))).all())
    return {"todo": sum(rows.get(s, 0) for s in monitoring.TODO_STATUSES),
            "new": sum(rows.get(s, 0) for s in monitoring.UNREAD_STATUSES),
            "progress": rows.get("IN_PROGRESS", 0), "resolved": rows.get("RESOLVED", 0)}


@router.get("")
async def list_issues(filter: str = Query("todo"), source: str | None = Query(None),
                      db: AsyncSession = Depends(get_db), _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    if filter not in FILTERS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filtre inconnu")
    try:
        await monitoring.drain(db)  # erreurs encore en file (dont celles de ce processus) : la page est à jour
    except Exception:  # noqa: BLE001 — la liste s'affiche quand même ; la tâche « monitoring-drain » reprendra
        await db.rollback()
    query = select(MonitorIssue).where(MonitorIssue.status.in_(FILTERS[filter]))
    if source:
        query = query.where(MonitorIssue.source == source)
    unread_first = case((MonitorIssue.status.in_(monitoring.UNREAD_STATUSES), 0), else_=1)
    issues = (await db.execute(query.order_by(unread_first, MonitorIssue.last_seen.desc()).limit(LIST_LIMIT))).scalars().all()
    names = await _names(db, issues)
    return {"counts": await _counts(db), "issues": [_summary(i, names) for i in issues],
            "sources": [{"code": k, "label": v} for k, v in monitoring.SOURCES.items()],
            "updated_at": datetime.now(timezone.utc).isoformat()}


@router.get("/status")
async def service_status(db: AsyncSession = Depends(get_db), _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    return await monitoring_status.checks(db)


@router.post("/checks/{key}/test")
async def test_check(key: str, _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    if key not in ("smtp", "gmail"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test inconnu")
    return await asyncio.to_thread(monitoring_status.run_test, key)


async def _issue_or_404(db, issue_id: UUID) -> MonitorIssue:
    issue = await db.get(MonitorIssue, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Erreur introuvable (effacée après 30 jours ?)")
    return issue


async def _detail(db, issue: MonitorIssue) -> dict:
    names = await _names(db, [issue])
    occurrences = (await db.execute(select(MonitorOccurrence).where(MonitorOccurrence.issue_id == issue.id)
                                    .order_by(MonitorOccurrence.at.desc()).limit(OCCURRENCES_SHOWN))).scalars().all()
    missing = {str(o.tenant_id) for o in occurrences if o.tenant_id and str(o.tenant_id) not in names}
    if missing:
        names.update({str(t.id): t.name for t in (await db.execute(select(Tenant).where(Tenant.id.in_([UUID(m) for m in missing])))).scalars()})
    return {**_summary(issue, names), **monitoring.explain(issue),
            "exc_type": issue.exc_type, "meta_code": issue.meta_code, "context": issue.context,
            "last_message": issue.last_message, "trace": issue.last_trace,
            "resolved_at": _iso(issue.resolved_at), "resolved_by": issue.resolved_by, "status_changed_at": _iso(issue.status_changed_at),
            "occurrences": [{"at": _iso(o.at), "shop": names.get(str(o.tenant_id)) if o.tenant_id else None,
                             "message": o.message, "context": o.context} for o in occurrences]}


@router.get("/issues/{issue_id}")
async def get_issue(issue_id: UUID, db: AsyncSession = Depends(get_db), _: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    return await _detail(db, await _issue_or_404(db, issue_id))


class StatusChange(BaseModel):
    status: str


@router.post("/issues/{issue_id}/status")
async def change_status(issue_id: UUID, payload: StatusChange, db: AsyncSession = Depends(get_db),
                        admin: CurrentSuperAdmin = Depends(get_current_superadmin)) -> dict:
    if payload.status not in ("IN_PROGRESS", "RESOLVED", "NEW"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Statut inconnu")
    issue = await _issue_or_404(db, issue_id)
    now, me = datetime.now(timezone.utc), await _me(db, admin)
    issue.status, issue.status_changed_at = payload.status, now
    if payload.status == "IN_PROGRESS":
        issue.assignee = me
    elif payload.status == "RESOLVED":
        issue.resolved_at, issue.resolved_by = now, me
    else:
        issue.assignee = issue.resolved_at = issue.resolved_by = None
    issue.alerted_at = issue.alerted_at or now  # traitée à la main : plus d'email « nouvelle erreur » pour elle
    await db.commit()
    return await _detail(db, issue)
