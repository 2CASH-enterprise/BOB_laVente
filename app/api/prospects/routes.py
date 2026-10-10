"""
Lot 61 — prospection d'AgenC'AI : le lien personnel /p/CODE (public) et la page Prospection du Super Admin.
Super Admins seulement (authentification séparée de celle des boutiques).
"""
import json
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import CurrentSuperAdmin, get_current_superadmin
from app.models.prospect import Prospect, ProspectEvent
from app.models.superadmin_user import SuperAdminUser
from app.services import prospection as pros
from app.services.spreadsheet import MAX_BYTES, XLSX_MEDIA, to_xlsx

public_router = APIRouter(tags=["prospection"])
router = APIRouter(prefix="/api/v1/superadmin/prospects", tags=["prospection"])


@public_router.get("/p/{code}", include_in_schema=False)
async def prospect_link(code: str, request: Request, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    """Lien personnel d'un prospect : clic noté, code gardé 30 jours dans le navigateur, puis la présentation de Bob."""
    prospect = await pros.record_click(db, code, request.headers.get("user-agent"))
    # Lot 62 : la page de présentation sous l'adresse publique de Bob (…/bob/), pas la racine du domaine.
    response = RedirectResponse(get_settings().public_base_url.rstrip("/") + "/", status_code=302)
    if prospect is not None:
        response.set_cookie(pros.COOKIE, prospect.code, max_age=pros.COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                            secure=get_settings().public_base_url.startswith("https"), path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


# Lot 62 — ouverture d'un email (image de 1 pixel) et désinscription en un clic.
_PIXEL = bytes.fromhex("47494638396101000100800000ffffff00000021f90401000000002c00000000010001000002024401003b")


@public_router.get("/p/{code}/o/{email_id}.gif", include_in_schema=False)
async def prospect_open(code: str, email_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    prospect = await _by_code(db, code)
    if prospect is not None:
        from app.services import prospect_mailer

        await prospect_mailer.record_open(db, prospect, email_id)
    return Response(_PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store, max-age=0"})


async def _by_code(db, code: str):
    code = (code or "").strip().lower()[:16]
    return (await db.execute(select(Prospect).where(Prospect.code == code))).scalar_one_or_none()


def _page(title: str, text: str, form: bool = False) -> HTMLResponse:
    from html import escape

    button = ('<form method="post" action="" style="margin-top:18px;">'
              '<button style="background:#1B4332;color:#fff;border:0;border-radius:10px;padding:12px 20px;font-size:15px;font-weight:700;cursor:pointer;">'
              'Confirmer : ne plus recevoir ces emails</button></form>') if form else ""
    return HTMLResponse(f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title></head><body style="margin:0;background:#F4FAF6;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#13241A;">
<div style="max-width:460px;margin:60px auto;background:#fff;border:1px solid #E3EBE6;border-radius:16px;padding:28px;">
<h1 style="font-size:20px;margin:0 0 10px;">{escape(title)}</h1><p style="margin:0;line-height:1.55;">{escape(text)}</p>{button}</div></body></html>""",
                        headers={"Cache-Control": "no-store"})


@public_router.get("/p/{code}/stop", include_in_schema=False)
async def prospect_stop_page(code: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """Une page avec un bouton (les antivirus qui ouvrent les liens ne désinscrivent personne par erreur)."""
    prospect = await _by_code(db, code)
    if prospect is None:
        return _page("Lien inconnu", "Ce lien n'est pas valide.")
    if prospect.status == "UNSUBSCRIBED":
        return _page("C'est fait", "Vous ne recevrez plus nos emails.")
    return _page("Ne plus recevoir nos emails", f"Pour {prospect.company} : confirmez et nous ne vous écrirons plus.", form=True)


@public_router.post("/p/{code}/stop", include_in_schema=False)
async def prospect_stop(code: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """Désinscription (bouton de la page, ou « Se désabonner » de Gmail / Outlook : List-Unsubscribe-Post)."""
    from app.services import prospect_mailer

    prospect = await _by_code(db, code)
    if prospect is None:
        return _page("Lien inconnu", "Ce lien n'est pas valide.")
    await prospect_mailer.unsubscribe(db, prospect, "lien de désinscription")
    return _page("C'est fait", "Vous ne recevrez plus nos emails. Merci de nous avoir lus.")


# --- Super Admin ---------------------------------------------------------------------------------------------

class ProspectIn(BaseModel):
    company: str | None = Field(None, max_length=300)
    contact_name: str | None = Field(None, max_length=300)
    email: str | None = Field(None, max_length=300)
    phone: str | None = Field(None, max_length=40)
    city: str | None = Field(None, max_length=200)
    country: str | None = Field(None, max_length=2)
    sector: str | None = None
    source: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=4000)
    next_action_on: str | None = None
    owner_id: str | None = None


class ContactIn(BaseModel):
    channel: str
    note: str | None = Field(None, max_length=500)
    next_action_on: str | None = None


class StatusIn(BaseModel):
    status: str
    reason: str | None = Field(None, max_length=200)


class NoteIn(BaseModel):
    note: str = Field(..., min_length=1, max_length=500)


async def _me(db, admin: CurrentSuperAdmin) -> SuperAdminUser:
    me = await db.get(SuperAdminUser, admin.superadmin_user_id)
    if me is None or not me.active:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return me


async def _owners(db) -> dict:
    return {a.id: a.full_name for a in (await db.execute(select(SuperAdminUser))).scalars().all()}


async def _get(db, prospect_id: UUID) -> Prospect:
    prospect = await db.get(Prospect, prospect_id)
    if prospect is None:
        raise HTTPException(status_code=404, detail="Prospect introuvable")
    return prospect


def _first(name: str | None) -> str | None:
    return (name or "").split()[0] if (name or "").strip() else None


def _view(p: Prospect, stage: str, owners: dict, today: date) -> dict:
    return {
        "id": str(p.id), "code": p.code, "company": p.company, "contact_name": p.contact_name, "email": p.email,
        "phone": f"+{p.phone}" if p.phone else None, "city": p.city, "country": p.country,
        "sector": p.sector, "sector_label": pros.SECTORS.get(p.sector, p.sector), "source": p.source,
        "owner_id": str(p.owner_id) if p.owner_id else None, "owner": owners.get(p.owner_id),
        "status": p.status, "status_label": pros.STATUSES.get(p.status, p.status), "status_reason": p.status_reason,
        "stage": stage, "stage_label": dict(pros.STAGES)[stage], "stage_index": pros.STAGE_INDEX[stage],
        "follow_up": pros.to_follow_up(p, stage, today),
        "next_action_on": p.next_action_on.isoformat() if p.next_action_on else None,
        "last_contact_at": p.last_contact_at.isoformat() if p.last_contact_at else None,
        "click_count": p.click_count or 0, "notes": p.notes, "link": pros.link(p.code),
        "email_step": p.email_step or 0, "opened": p.first_opened_at is not None, "replied": p.replied_at is not None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "tenant_id": str(p.tenant_id) if p.tenant_id else None,
    }


async def _stages(db, prospects: list) -> dict:
    facts = await pros.tenant_facts(db, {p.tenant_id for p in prospects})
    return {p.id: pros.stage_of(p, facts) for p in prospects}


@router.get("")
async def list_prospects(
    view: str = Query(default="all", pattern="^(all|todo|replied|clicked|signed_up|lost)$"),
    q: str | None = Query(default=None, max_length=100),
    sector: str | None = None,
    owner: str | None = None,
    admin: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _me(db, admin)
    stmt = select(Prospect).order_by(Prospect.created_at.desc())
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        from sqlalchemy import func

        stmt = stmt.where(or_(func.lower(Prospect.company).like(like), func.lower(Prospect.contact_name).like(like),
                              func.lower(Prospect.email).like(like), Prospect.phone.like(f"%{q.strip().lstrip('+')}%"),
                              func.lower(Prospect.city).like(like), func.lower(Prospect.source).like(like)))
    if sector in pros.SECTORS:
        stmt = stmt.where(Prospect.sector == sector)
    if owner:
        try:
            stmt = stmt.where(Prospect.owner_id == UUID(owner))
        except ValueError:
            raise HTTPException(status_code=422, detail="Super Admin inconnu") from None
    prospects = (await db.execute(stmt)).scalars().all()
    stages = await _stages(db, prospects)
    owners = await _owners(db)
    today = datetime.now(timezone.utc).date()
    rows = [_view(p, stages[p.id], owners, today) for p in prospects]
    counts = {"all": len(rows), "todo": sum(1 for r in rows if r["follow_up"]), "replied": sum(1 for r in rows if r["replied"]),
              "clicked": sum(1 for r in rows if r["stage_index"] >= pros.STAGE_INDEX["CLICKED"] and r["status"] == "ACTIVE"),
              "signed_up": sum(1 for r in rows if r["stage_index"] >= pros.STAGE_INDEX["SIGNED_UP"]),
              "lost": sum(1 for r in rows if r["status"] != "ACTIVE")}
    if view == "todo":
        rows = [r for r in rows if r["follow_up"]]
    elif view == "replied":
        rows = [r for r in rows if r["replied"]]
    elif view == "clicked":
        rows = [r for r in rows if r["stage_index"] >= pros.STAGE_INDEX["CLICKED"] and r["status"] == "ACTIVE"]
    elif view == "signed_up":
        rows = [r for r in rows if r["stage_index"] >= pros.STAGE_INDEX["SIGNED_UP"]]
    elif view == "lost":
        rows = [r for r in rows if r["status"] != "ACTIVE"]
    return {"prospects": rows[:500], "counts": counts,
            "owners": [{"id": str(k), "name": v} for k, v in owners.items()],
            "channels": [{"value": k, "label": v} for k, v in pros.CHANNELS.items()],
            "statuses": [{"value": k, "label": v} for k, v in pros.STATUSES.items()],
            "sectors": [{"value": k, "label": v} for k, v in pros.SECTORS.items()]}


@router.get("/funnel")
async def prospect_funnel(
    period: str = Query(default="all", pattern="^(30|90|all)$"),
    admin: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Entonnoir des prospects ajoutés sur la période, puis par source, par secteur et par Super Admin."""
    await _me(db, admin)
    stmt = select(Prospect)
    if period != "all":
        stmt = stmt.where(Prospect.created_at >= datetime.now(timezone.utc) - timedelta(days=int(period)))
    prospects = (await db.execute(stmt)).scalars().all()
    stages = await _stages(db, prospects)
    owners = await _owners(db)
    return {
        "period": period, "total": len(prospects), "funnel": pros.funnel(prospects, stages),
        "by_source": pros.group_funnel(prospects, stages, lambda p: p.source),
        "by_sector": pros.group_funnel(prospects, stages, lambda p: pros.SECTORS.get(p.sector)),
        "by_owner": pros.group_funnel(prospects, stages, lambda p: owners.get(p.owner_id)),
    }


@router.post("", status_code=201)
async def create_prospect(payload: ProspectIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    me = await _me(db, admin)
    data = payload.model_dump(exclude_unset=True)
    try:
        fields = pros.clean_fields(data)
    except (pros.ProspectError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    duplicate = await pros.find_duplicate(db, fields.get("email"), fields.get("phone"))
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"Ce prospect existe déjà : {duplicate.company}")
    prospect = Prospect(code=await pros.new_code(db), owner_id=me.id, status="ACTIVE", **fields)
    db.add(prospect)
    await db.flush()
    pros.add_event(db, prospect, "ADDED", detail="Ajouté à la main", actor=me.full_name)
    await db.commit()
    return await prospect_detail(prospect.id, admin, db)


@router.get("/export.xlsx")
async def export_prospects(admin: CurrentSuperAdmin = Depends(get_current_superadmin), db: AsyncSession = Depends(get_db)) -> Response:
    await _me(db, admin)
    prospects = (await db.execute(select(Prospect).order_by(Prospect.created_at))).scalars().all()
    stages = await _stages(db, prospects)
    owners = await _owners(db)
    header = ["Entreprise", "Contact", "Email", "Téléphone", "Ville", "Pays", "Secteur", "Source", "Suivi par", "Étape",
              "Statut", "Clics", "Dernier contact", "Ajouté le", "Lien personnel", "Note"]
    rows = [[p.company, p.contact_name or "", p.email or "", f"+{p.phone}" if p.phone else "", p.city or "", p.country,
             pros.SECTORS.get(p.sector, p.sector), p.source or "", owners.get(p.owner_id) or "", dict(pros.STAGES)[stages[p.id]],
             pros.STATUSES.get(p.status, p.status), p.click_count or 0,
             p.last_contact_at.strftime("%d/%m/%Y") if p.last_contact_at else "",
             p.created_at.strftime("%d/%m/%Y") if p.created_at else "", pros.link(p.code), p.notes or ""] for p in prospects]
    return Response(to_xlsx("Prospects", header, rows), media_type=XLSX_MEDIA,
                    headers={"Content-Disposition": 'attachment; filename="prospects.xlsx"'})


@router.post("/import/preview")
async def import_preview(file: UploadFile, sheet: str | None = Form(None),
                         admin: CurrentSuperAdmin = Depends(get_current_superadmin), db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    raw = await file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (5 Mo au plus)")
    try:
        return pros.preview(raw, file.filename or "", sheet or None)
    except pros.ProspectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/import")
async def import_prospects(
    file: UploadFile,
    mapping: str = Form(...),
    sheet: str | None = Form(None),
    country: str = Form("CI"),
    sector: str = Form("ONLINE_STORE"),
    source: str | None = Form(None),
    admin: CurrentSuperAdmin = Depends(get_current_superadmin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    me = await _me(db, admin)
    raw = await file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (5 Mo au plus)")
    try:
        chosen = json.loads(mapping)
    except ValueError:
        raise HTTPException(status_code=422, detail="Correspondance des colonnes illisible") from None
    try:
        return await pros.import_rows(db, raw, file.filename or "", sheet or None, chosen,
                                      {"country": country, "sector": sector, "source": source}, me.id, me.full_name)
    except pros.ProspectError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{prospect_id}")
async def prospect_detail(prospect_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    me = await _me(db, admin)
    prospect = await _get(db, prospect_id)
    stages = await _stages(db, [prospect])
    data = _view(prospect, stages[prospect.id], await _owners(db), datetime.now(timezone.utc).date())
    events = (await db.execute(select(ProspectEvent).where(ProspectEvent.prospect_id == prospect.id)
                               .order_by(ProspectEvent.at.desc()))).scalars().all()
    data["events"] = [{"kind": ev.kind, "label": pros.EVENT_LABELS.get(ev.kind, ev.kind),
                       "channel": pros.CHANNELS.get(ev.channel) if ev.channel else None, "detail": ev.detail,
                       "actor": ev.actor, "at": ev.at.isoformat() if ev.at else None} for ev in events]
    data["message"] = pros.message(prospect, _first(me.full_name))
    data["whatsapp_url"] = pros.whatsapp_url(prospect, _first(me.full_name)) if prospect.status == "ACTIVE" else None
    return data


@router.put("/{prospect_id}")
async def update_prospect(prospect_id: UUID, payload: ProspectIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    prospect = await _get(db, prospect_id)
    data = payload.model_dump(exclude_unset=True)
    owner = data.pop("owner_id", None) if "owner_id" in data else ...
    try:
        fields = pros.clean_fields(data, current=prospect)
    except (pros.ProspectError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    duplicate = await pros.find_duplicate(db, fields.get("email"), fields.get("phone"), exclude=prospect.id)
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"Ce prospect existe déjà : {duplicate.company}")
    for key, value in fields.items():
        setattr(prospect, key, value)
    if owner is not ...:
        try:
            known = await db.get(SuperAdminUser, UUID(owner)) if owner else None
        except ValueError:
            known = None
        if owner and known is None:
            raise HTTPException(status_code=422, detail="Super Admin inconnu")
        prospect.owner_id = UUID(owner) if owner else None
    await db.commit()
    return await prospect_detail(prospect.id, admin, db)


@router.post("/{prospect_id}/contact")
async def log_contact(prospect_id: UUID, payload: ContactIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """Un contact fait à la main (appel, WhatsApp depuis son téléphone, visite…) ; jamais pour un prospect perdu."""
    me = await _me(db, admin)
    prospect = await _get(db, prospect_id)
    if payload.channel not in pros.CHANNELS:
        raise HTTPException(status_code=422, detail="Canal inconnu")
    if prospect.status in ("UNSUBSCRIBED", "INVALID"):
        raise HTTPException(status_code=409, detail="Ce prospect ne doit plus être contacté")
    moment = datetime.now(timezone.utc)
    prospect.contacted_at = prospect.contacted_at or moment
    prospect.last_contact_at = moment
    try:
        prospect.next_action_on = date.fromisoformat(payload.next_action_on) if payload.next_action_on else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Date de relance invalide") from None
    pros.add_event(db, prospect, "CONTACT", detail=(payload.note or "").strip() or None, channel=payload.channel,
                   actor=me.full_name, at=moment)
    await db.commit()
    return await prospect_detail(prospect.id, admin, db)


@router.post("/{prospect_id}/status")
async def change_status(prospect_id: UUID, payload: StatusIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                        db: AsyncSession = Depends(get_db)) -> dict:
    me = await _me(db, admin)
    prospect = await _get(db, prospect_id)
    if payload.status not in pros.STATUSES:
        raise HTTPException(status_code=422, detail="Statut inconnu")
    prospect.status = payload.status
    prospect.status_reason = (payload.reason or "").strip() or None
    if payload.status != "ACTIVE":
        prospect.next_action_on = None
    label = pros.STATUSES[payload.status] + (f" — {prospect.status_reason}" if prospect.status_reason else "")
    pros.add_event(db, prospect, "STATUS", detail=label, actor=me.full_name)
    await db.commit()
    return await prospect_detail(prospect.id, admin, db)


@router.post("/{prospect_id}/note")
async def add_note(prospect_id: UUID, payload: NoteIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                   db: AsyncSession = Depends(get_db)) -> dict:
    me = await _me(db, admin)
    prospect = await _get(db, prospect_id)
    pros.add_event(db, prospect, "NOTE", detail=payload.note.strip(), actor=me.full_name)
    await db.commit()
    return await prospect_detail(prospect.id, admin, db)


@router.delete("/{prospect_id}", status_code=204)
async def delete_prospect(prospect_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                          db: AsyncSession = Depends(get_db)) -> Response:
    """Effacement à la demande du prospect (ses données et son historique)."""
    await _me(db, admin)
    prospect = await _get(db, prospect_id)
    from sqlalchemy import delete

    await db.execute(delete(ProspectEvent).where(ProspectEvent.prospect_id == prospect.id))
    await db.delete(prospect)
    await db.commit()
    return Response(status_code=204)
