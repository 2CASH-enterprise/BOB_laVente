"""
Lot 82 — liens de publication (Facebook, Instagram, LinkedIn, groupes…) et page Facebook de Bob.

- Un lien par publication ou publicité : il ouvre la démo du secteur avec ?src=CODE. Le navigateur qui l'ouvre est
  compté une fois (cookie propre à Bob « bob_src », 30 jours ; jamais les aperçus de lien des réseaux) ; la démo puis
  le compte créés ensuite sont rattachés au lien. Aucun pixel ni cookie publicitaire : rien n'est transmis à Facebook.
- La page Facebook de Bob (réglage FACEBOOK_PAGE_URL) est proposée en fin de démo et dans les emails de prospection.
"""
import logging
import re
import secrets
import unicodedata

from sqlalchemy import func, select

from app.services import prospection as pros

logger = logging.getLogger(__name__)

COOKIE = "bob_src"
COOKIE_DAYS = 30
CHANNELS = {
    "FACEBOOK_POST": "Publication Facebook",
    "FACEBOOK_AD": "Publicité Facebook",
    "INSTAGRAM": "Instagram",
    "LINKEDIN": "LinkedIn",
    "GROUP": "Groupe (Facebook, WhatsApp…)",
    "OTHER": "Autre",
}
_CODE = re.compile(r"^[a-z0-9-]{3,48}$")


def facebook_page_url() -> str | None:
    from app.core.config import get_settings

    url = (get_settings().facebook_page_url or "").strip()
    return url if url.startswith("https://") else None


def slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:36] or "lien"


def new_code(name: str) -> str:
    """« Mandataires Lyon — octobre » → « mandataires-lyon-octobre-k7q2 » (lisible, et impossible à deviner)."""
    return f"{slug(name)}-{secrets.token_hex(2)}"


def url(link) -> str:
    base = pros.demo_url(link.sector)
    return f"{base}{'&' if '?' in base else '?'}src={link.code}"


async def record_click(db, code: str | None, user_agent: str | None, already: str | None):
    """Le lien du code (ou None). Compté une fois par navigateur ; jamais pour un robot ni un lien archivé."""
    from app.models.ad_link import AdLink

    code = (code or "").strip().lower()[:48]
    if not _CODE.match(code):
        return None
    link = (await db.execute(select(AdLink).where(AdLink.code == code))).scalar_one_or_none()
    if link is None or link.archived or pros.is_bot(user_agent):
        return None
    if (already or "").strip().lower() != code:
        link.clicks = (link.clicks or 0) + 1
        link.last_click_at = pros.now_utc()
        await db.commit()
    return link


async def attach(db, request, tenant_id, kind: str) -> None:
    """Démo (DEMO) ou compte (SIGNUP) rattaché au lien de publication du navigateur ; ne bloque jamais rien."""
    from app.models.ad_link import AdLink, AdLinkEvent

    try:
        code = (request.cookies.get(COOKIE) or "").strip().lower() if request is not None else ""
        link_id = None
        if _CODE.match(code):
            link_id = (await db.execute(select(AdLink.id).where(AdLink.code == code))).scalar_one_or_none()
        if link_id is None and kind == "SIGNUP":  # compte créé depuis la démo : même boutique que la démo
            link_id = (await db.execute(select(AdLinkEvent.link_id).where(
                AdLinkEvent.kind == "DEMO", AdLinkEvent.tenant_id == tenant_id))).scalars().first()
        if link_id is None:
            return
        exists = (await db.execute(select(AdLinkEvent.id).where(
            AdLinkEvent.link_id == link_id, AdLinkEvent.kind == kind, AdLinkEvent.tenant_id == tenant_id))).first()
        if exists is None:
            db.add(AdLinkEvent(link_id=link_id, kind=kind, tenant_id=tenant_id))
            await db.commit()
    except Exception:  # noqa: BLE001 — le suivi ne bloque jamais une démo ni une inscription
        logger.exception("Rattachement au lien de publication impossible (%s)", kind)
        await db.rollback()


async def overview(db) -> list[dict]:
    from app.models.ad_link import AdLink, AdLinkEvent

    links = (await db.execute(select(AdLink).order_by(AdLink.archived, AdLink.created_at.desc()))).scalars().all()
    counts = {(link_id, kind): n for link_id, kind, n in (await db.execute(
        select(AdLinkEvent.link_id, AdLinkEvent.kind, func.count()).group_by(AdLinkEvent.link_id, AdLinkEvent.kind))).all()}
    return [{"id": str(link.id), "code": link.code, "name": link.name, "channel": link.channel,
             "channel_label": CHANNELS.get(link.channel, link.channel), "sector": link.sector,
             "sector_label": pros.SECTORS.get(link.sector, "Tous secteurs") if link.sector else "Tous secteurs",
             "url": url(link), "clicks": link.clicks or 0, "demos": counts.get((link.id, "DEMO"), 0),
             "signups": counts.get((link.id, "SIGNUP"), 0), "archived": link.archived,
             "last_click_at": link.last_click_at.isoformat() if link.last_click_at else None,
             "created_at": link.created_at.isoformat() if link.created_at else None} for link in links]
