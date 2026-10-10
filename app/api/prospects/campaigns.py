"""
Lot 62 — emails de prospection (Super Admin → Prospection) : réglages d'envoi, campagnes, aperçu, essai, lancement.
Préfixe à part (« /prospection ») : « /prospects/{id} » prendrait sinon « campaigns » pour un identifiant.
"""
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import CurrentSuperAdmin, get_current_superadmin
from app.models.prospect import Prospect, ProspectCampaign, ProspectEmail
from app.models.superadmin_user import SuperAdminUser
from app.services import prospect_mailer as mailer
from app.services import prospection as pros

router = APIRouter(prefix="/api/v1/superadmin/prospection", tags=["prospection"])


class CampaignIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    sector: str | None = None
    source: str | None = Field(None, max_length=120)
    country: str | None = Field(None, max_length=2)
    subject_1: str = Field(..., min_length=1, max_length=200)
    body_1: str = Field(..., min_length=1, max_length=5000)
    subject_2: str | None = Field(None, max_length=200)
    body_2: str | None = Field(None, max_length=5000)
    subject_3: str | None = Field(None, max_length=200)
    body_3: str | None = Field(None, max_length=5000)


class SettingsIn(BaseModel):
    daily_limit: int | None = None
    sending_enabled: bool | None = None


async def _me(db, admin: CurrentSuperAdmin) -> SuperAdminUser:
    me = await db.get(SuperAdminUser, admin.superadmin_user_id)
    if me is None or not me.active:
        raise HTTPException(status_code=401, detail="Non authentifié")
    return me


async def _campaign(db, campaign_id: UUID) -> ProspectCampaign:
    campaign = await db.get(ProspectCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campagne introuvable")
    return campaign


def _clean(payload: CampaignIn) -> dict:
    data = payload.model_dump()
    if data["sector"] and data["sector"] not in pros.SECTORS:
        raise HTTPException(status_code=422, detail="Secteur inconnu")
    data["sector"] = data["sector"] or None
    data["source"] = " ".join((data["source"] or "").split()) or None
    data["country"] = (data["country"] or "").strip().upper() or None
    data["name"] = " ".join(data["name"].split())
    for i in (2, 3):  # une relance sans objet ou sans texte n'est pas envoyée
        if not (data[f"subject_{i}"] or "").strip() or not (data[f"body_{i}"] or "").strip():
            data[f"subject_{i}"] = data[f"body_{i}"] = None
    if data["subject_3"] and not data["subject_2"]:
        raise HTTPException(status_code=422, detail="La dernière relance suppose une première relance")
    if "{lien}" not in data["body_1"]:
        raise HTTPException(status_code=422, detail="Le premier email doit contenir {lien} (le lien personnel du prospect)")
    return data


async def _view(db, campaign: ProspectCampaign) -> dict:
    eligible = len(await mailer.eligible(db, campaign)) if campaign.status in ("DRAFT", "PAUSED", "RUNNING") else 0
    return {
        "id": str(campaign.id), "name": campaign.name, "sector": campaign.sector,
        "sector_label": pros.SECTORS.get(campaign.sector) if campaign.sector else "Tous les secteurs",
        "source": campaign.source, "country": campaign.country, "status": campaign.status,
        "steps": len(mailer.steps_of(campaign)), "eligible": eligible,
        "subject_1": campaign.subject_1, "body_1": campaign.body_1, "subject_2": campaign.subject_2, "body_2": campaign.body_2,
        "subject_3": campaign.subject_3, "body_3": campaign.body_3,
        "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
        "stats": await mailer.stats(db, campaign),
    }


@router.get("/email")
async def email_overview(admin: CurrentSuperAdmin = Depends(get_current_superadmin), db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    row = await mailer.get_settings_row(db)
    await db.commit()
    now = mailer.now_utc()
    week = (await db.execute(select(ProspectEmail.status, func.count(ProspectEmail.id)).where(
        ProspectEmail.sent_at >= now - timedelta(days=7)).group_by(ProspectEmail.status))).all()
    counts = dict(week)
    sent_7d = counts.get("SENT", 0) + counts.get("BOUNCED", 0)
    campaigns = (await db.execute(select(ProspectCampaign).order_by(ProspectCampaign.created_at.desc()))).scalars().all()
    s = get_settings()
    return {
        "configured": mailer.settings_ok(), "from": f"{s.prospect_from_name} <{s.prospect_smtp_username}>" if mailer.settings_ok() else None,
        "copy_to": bool(s.prospect_copy_to), "daily_limit": row.daily_limit, "limits": mailer.LIMITS,
        "sending_enabled": row.sending_enabled, "sent_24h": await mailer.sent_last_24h(db, now),
        "sent_7d": sent_7d, "bounced_7d": counts.get("BOUNCED", 0),
        "recommendation": mailer.recommendation(row.daily_limit, sent_7d, counts.get("BOUNCED", 0), row.last_error),
        "last_error": row.last_error, "last_error_at": row.last_error_at.isoformat() if row.last_error_at else None,
        "campaigns": [await _view(db, c) for c in campaigns],
    }


@router.put("/email/settings")
async def email_settings(payload: SettingsIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    row = await mailer.get_settings_row(db)
    if payload.daily_limit is not None:
        if payload.daily_limit not in mailer.LIMITS:
            raise HTTPException(status_code=422, detail="Plafond proposé : " + ", ".join(map(str, mailer.LIMITS)))
        row.daily_limit = payload.daily_limit
    if payload.sending_enabled is not None:
        row.sending_enabled = payload.sending_enabled
    if payload.sending_enabled:
        row.last_error = None  # reprise après correction du réglage
    await db.commit()
    return await email_overview(admin, db)


@router.get("/defaults")
async def campaign_defaults(sector: str = "ONLINE_STORE", admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                            db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    steps = mailer.DEFAULTS.get(sector, mailer.DEFAULTS["ONLINE_STORE"])
    return {f"{key}_{i}": value for i, (subject, body) in enumerate(steps, start=1)
            for key, value in (("subject", subject), ("body", body))}


@router.post("/campaigns", status_code=201)
async def create_campaign(payload: CampaignIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    me = await _me(db, admin)
    campaign = ProspectCampaign(status="DRAFT", created_by=me.full_name, **_clean(payload))
    db.add(campaign)
    await db.commit()
    return await _view(db, campaign)


@router.put("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: UUID, payload: CampaignIn, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                          db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    campaign = await _campaign(db, campaign_id)
    if campaign.status == "RUNNING":
        raise HTTPException(status_code=409, detail="Mettez la campagne en pause avant de la modifier")
    for key, value in _clean(payload).items():
        setattr(campaign, key, value)
    await db.commit()
    return await _view(db, campaign)


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                          db: AsyncSession = Depends(get_db)):
    from fastapi.responses import Response

    await _me(db, admin)
    campaign = await _campaign(db, campaign_id)
    if campaign.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Une campagne déjà lancée se met en pause, elle ne s'efface pas")
    await db.delete(campaign)
    await db.commit()
    return Response(status_code=204)


async def _sample(db, campaign) -> Prospect:
    targets = await mailer.eligible(db, campaign)
    if targets:
        return targets[0]
    return Prospect(company="Boutique Awa", contact_name="Awa Koné", city="Cocody", country="CI",
                    sector=campaign.sector or "ONLINE_STORE", code="exemple")


@router.post("/campaigns/{campaign_id}/preview")
async def preview_campaign(campaign_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                           db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    campaign = await _campaign(db, campaign_id)
    sample = await _sample(db, campaign)
    out = []
    for i, (subject, body) in enumerate(mailer.steps_of(campaign), start=1):
        rendered = mailer.render(sample, subject, body)
        out.append({"step": i, "subject": rendered[0], "text": rendered[1], "html": rendered[2],
                    "day": {1: 1, 2: 5, 3: 11}[i]})
    return {"sample": sample.company, "steps": out}


@router.post("/campaigns/{campaign_id}/test")
async def test_campaign(campaign_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                        db: AsyncSession = Depends(get_db)) -> dict:
    """Les emails de la campagne, envoyés à soi seul par le compte de prospection (rien n'est noté)."""
    me = await _me(db, admin)
    campaign = await _campaign(db, campaign_id)
    if not mailer.settings_ok():
        raise HTTPException(status_code=409, detail="Le compte Gmail de prospection n'est pas encore réglé (.env du serveur)")
    sample = await _sample(db, campaign)
    fake = Prospect(company=sample.company, contact_name=sample.contact_name, city=sample.city, country=sample.country,
                    sector=sample.sector, code=sample.code, email=me.email)
    try:
        for subject, body in mailer.steps_of(campaign):
            subject_r, text, html = mailer.render(fake, subject, body)
            mailer.smtp_transport(mailer.build_email(fake, "[Essai] " + subject_r, text, html,
                                                     mailer.make_msgid(domain="essai.bob")))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Envoi d'essai impossible : {str(exc)[:200]}") from None
    return {"sent_to": me.email, "emails": len(mailer.steps_of(campaign))}


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    campaign = await _campaign(db, campaign_id)
    try:
        added = await mailer.start(db, campaign)
    except mailer.MailerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {**await _view(db, campaign), "added": added}


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: UUID, admin: CurrentSuperAdmin = Depends(get_current_superadmin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    await _me(db, admin)
    campaign = await _campaign(db, campaign_id)
    if campaign.status != "RUNNING":
        raise HTTPException(status_code=409, detail="Cette campagne n'est pas en cours")
    campaign.status = "PAUSED"
    await db.commit()
    return await _view(db, campaign)
