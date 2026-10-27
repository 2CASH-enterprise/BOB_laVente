"""
Lot 89 — connecteur Naomy.

- /api/v1/naomy/*            : l'API lue par Naomy (clé de la boutique), au format du contrat 1.2
                               (docs/naomy/bob-naomy-openapi-v1.2.yaml) ; erreurs {"erreur", "message"}.
- /api/v1/naomy-connection*  : la carte « Naomy » du tableau de bord (connecter, déconnecter, rappels par e-mail).
- /api/v1/naomy-rappels/desinscription : lien de désinscription en un clic des rappels par e-mail.

Lot 92 (contrat 1.3.1) : POST /api/v1/naomy/appairage/demande (signée par la plateforme, toujours 202),
POST /api/v1/naomy/assistant/messages (« Bob, votre assistant »), et dans la carte : portée, interrupteur de
l'assistant, bilan du soir, demande de Naomy à accepter ou refuser.
"""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.core.security import CurrentUser, get_current_user, require_role
from app.services import naomy_bridge as nb
from app.services.naomy_bridge import NaomyError

router = APIRouter(tags=["naomy"])
API = "/api/v1/naomy"


def naomy_error_response(_request, exc: NaomyError) -> JSONResponse:
    return JSONResponse({"erreur": exc.code, "message": exc.message}, status_code=exc.status, headers=exc.headers)


async def _json(request: Request) -> dict:
    try:
        data = json.loads(await request.body() or b"null")
    except (ValueError, UnicodeDecodeError) as exc:
        raise NaomyError(400, "requete_invalide", "Corps JSON attendu.") from exc
    if not isinstance(data, dict):
        raise NaomyError(400, "requete_invalide", "Corps JSON attendu (objet).")
    return data


class NaomyContext:
    def __init__(self, connection, tenant):
        self.connection, self.tenant = connection, tenant


async def naomy_context(request: Request, db: AsyncSession = Depends(get_db),
                        limiter: RateLimiter = Depends(get_rate_limiter)) -> NaomyContext:
    from app.models.tenant import Tenant

    header = request.headers.get("authorization") or ""
    key = header[7:].strip() if header.lower().startswith("bearer ") else None
    connection = await nb.connection_for_key(db, key)
    tenant = await db.get(Tenant, connection.tenant_id) if connection is not None else None
    if connection is None or tenant is None:
        raise NaomyError(401, "cle_revoquee", "Clé inconnue ou connexion coupée.")
    if not await limiter.is_allowed(f"naomy:{tenant.id}", nb.RATE_LIMIT_PER_MINUTE, 60):
        raise NaomyError(429, "trop_de_requetes", "Réessayez dans quelques secondes.", {"Retry-After": "60"})
    return NaomyContext(connection, tenant)


def _limit(value: str | None) -> int:
    if value in (None, ""):
        return 100
    if not value.isdigit() or not 1 <= int(value) <= nb.MAX_PAGE:
        raise NaomyError(400, "requete_invalide", f"limite : de 1 à {nb.MAX_PAGE}.")
    return int(value)


# --- Connexion -------------------------------------------------------------------------------------------------------

@router.post(f"{API}/appairage")
async def naomy_pair(request: Request, db: AsyncSession = Depends(get_db)):
    from app.core.config import get_settings

    secret = get_settings().naomy_platform_secret
    if not secret:
        raise NaomyError(503, "indisponible", "La connexion à Naomy n'est pas encore activée sur Bob.")
    body = await request.body()
    if not nb.platform_signature_ok(secret, request.headers.get("x-platform-timestamp"),
                                    request.headers.get("x-platform-signature"), body):
        raise NaomyError(401, "signature_invalide", "Signature de plateforme invalide.")
    return await nb.pair(db, await _json(request))


@router.post(f"{API}/appairage/demande", status_code=202)
async def naomy_pairing_request(request: Request, db: AsyncSession = Depends(get_db),
                                limiter: RateLimiter = Depends(get_rate_limiter)):
    from app.core.config import get_settings

    secret = get_settings().naomy_platform_secret
    if not secret:
        raise NaomyError(503, "indisponible", "La connexion à Naomy n'est pas encore activée sur Bob.")
    body = await request.body()
    if not nb.platform_signature_ok(secret, request.headers.get("x-platform-timestamp"),
                                    request.headers.get("x-platform-signature"), body):
        raise NaomyError(401, "signature_invalide", "Signature de plateforme invalide.")
    await nb.receive_request(db, await _json(request), limiter)
    return JSONResponse({"statut": "recue"}, status_code=202)  # toujours : ne dit jamais si la boutique existe


@router.post(f"{API}/assistant/messages", status_code=202)
async def naomy_assistant_message(request: Request, ctx: NaomyContext = Depends(naomy_context),
                                  db: AsyncSession = Depends(get_db)):
    from app.services import merchant_assistant

    connection = await db.get(type(ctx.connection), ctx.connection.id)
    result = await merchant_assistant.receive(db, connection, ctx.tenant, await _json(request))
    return JSONResponse(result, status_code=202)


@router.delete(f"{API}/connexion", status_code=204)
async def naomy_disconnect(ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    connection = await db.get(type(ctx.connection), ctx.connection.id)
    nb.close(db, connection, "NAOMY")
    await db.commit()
    return Response(status_code=204)


# --- Boutique et catalogue -------------------------------------------------------------------------------------------

@router.get(f"{API}/boutique")
async def naomy_shop(ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    return await nb.shop_view(db, ctx.tenant)


async def _categories(db, tenant_id) -> dict:
    from app.models.category import Category

    return {c.id: c.name for c in (await db.execute(select(Category).where(Category.tenant_id == tenant_id))).scalars()}


@router.get(f"{API}/produits")
async def naomy_products(request: Request, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    from app.models.product import Product
    from app.services.business_type import is_insurance

    q = request.query_params
    limit = _limit(q.get("limite"))
    after = nb.decode_cursor(q.get("curseur"))
    stmt = select(Product).where(Product.tenant_id == ctx.tenant.id)
    since = q.get("modifies_depuis")
    if since:
        try:
            moment = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NaomyError(400, "requete_invalide", "modifies_depuis : date ISO 8601 attendue.") from exc
        stmt = stmt.where(func.coalesce(Product.updated_at, Product.created_at) >= moment)
    else:
        stmt = stmt.where(Product.active.is_(True))
    if after:
        try:
            stmt = stmt.where(Product.id > uuid.UUID(after))
        except ValueError as exc:
            raise NaomyError(400, "curseur_invalide", "Curseur invalide.") from exc
    rows = (await db.execute(stmt.order_by(Product.id).limit(limit + 1))).scalars().all()
    categories = await _categories(db, ctx.tenant.id)
    insurance = is_insurance(ctx.tenant)
    page = rows[:limit]
    return {"elements": [nb.product_view(p, insurance, categories.get(p.category_id)) for p in page],
            "curseur_suivant": nb.encode_cursor(str(page[-1].id)) if len(rows) > limit else None}


@router.get(API + "/produits/{produit_id}")
async def naomy_product(produit_id: str, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    from app.models.product import Product
    from app.services.business_type import is_insurance

    try:
        pid = uuid.UUID(produit_id)
    except ValueError as exc:
        raise NaomyError(404, "introuvable", "Produit introuvable dans cette boutique.") from exc
    product = await db.get(Product, pid)
    if product is None or product.tenant_id != ctx.tenant.id:
        raise NaomyError(404, "introuvable", "Produit introuvable dans cette boutique.")
    categories = await _categories(db, ctx.tenant.id)
    return nb.product_view(product, is_insurance(ctx.tenant), categories.get(product.category_id))


@router.get(f"{API}/stock")
async def naomy_stock(request: Request, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    from app.models.product import Product
    from app.services.business_type import is_insurance

    raw = [i.strip() for i in (request.query_params.get("ids") or "").split(",") if i.strip()]
    if not raw or len(raw) > 50:
        raise NaomyError(400, "requete_invalide", "ids : de 1 à 50 identifiants séparés par des virgules.")
    try:
        ids = [uuid.UUID(i) for i in raw]
    except ValueError as exc:
        raise NaomyError(400, "requete_invalide", "ids : identifiants invalides.") from exc
    found = {p.id: p for p in (await db.execute(select(Product).where(Product.tenant_id == ctx.tenant.id,
                                                                      Product.id.in_(ids)))).scalars()}
    insurance = is_insurance(ctx.tenant)
    elements = []
    for pid in ids:
        p = found.get(pid)
        if p is None:
            elements.append({"produit_id": str(pid), "existe": False})
        else:
            elements.append({"produit_id": str(pid), "existe": True, "actif": bool(p.active),
                             "stock": None if insurance else int(p.stock_quantity or 0),
                             "prix": None if insurance else nb.money(p.price, p.currency)})
    return {"elements": elements}


# --- Liens suivis (source « NaomyIA ») -------------------------------------------------------------------------------

@router.post(f"{API}/liens")
async def naomy_create_link(request: Request, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    from app.api.contact_points.routes import _generate_code
    from app.models.contact_point import ContactPoint
    from app.models.product import Product

    data = await _json(request)
    name, message, reference, product_id = data.get("nom"), data.get("message"), data.get("reference_naomy"), data.get("produit_id")
    if not (isinstance(name, str) and 0 < len(name.strip()) <= 80 and isinstance(message, str) and 0 < len(message.strip()) <= 300
            and isinstance(reference, str) and 0 < len(reference) <= 64
            and set(data) <= {"nom", "message", "produit_id", "reference_naomy"}):
        raise NaomyError(400, "requete_invalide", "Champs attendus : nom (80), message (300), reference_naomy (64), produit_id.")
    existing = (await db.execute(select(ContactPoint).where(ContactPoint.tenant_id == ctx.tenant.id,
                                                            ContactPoint.created_by == "NAOMY",
                                                            ContactPoint.naomy_reference == reference))).scalar_one_or_none()
    if existing is not None:
        return JSONResponse(nb.link_view(existing), status_code=200)
    pid = None
    if product_id is not None:
        try:
            pid = uuid.UUID(str(product_id))
        except ValueError as exc:
            raise NaomyError(400, "requete_invalide", "produit_id invalide.") from exc
        product = await db.get(Product, pid)
        if product is None or product.tenant_id != ctx.tenant.id:
            raise NaomyError(404, "introuvable", "Produit introuvable dans cette boutique.")
    for _ in range(5):
        code = _generate_code()
        if not (await db.execute(select(ContactPoint.id).where(ContactPoint.code == code))).first():
            break
    else:
        raise NaomyError(503, "indisponible", "Code de lien impossible à créer, réessayez.")
    cp = ContactPoint(tenant_id=ctx.tenant.id, code=code, name=" ".join(name.split())[:80],
                      greeting=" ".join(message.split())[:300], created_by="NAOMY", naomy_reference=reference,
                      product_id=pid, active=True, click_count=0, created_at=datetime.now(timezone.utc))
    db.add(cp)
    await db.commit()
    return JSONResponse(nb.link_view(cp), status_code=201)


@router.get(f"{API}/liens")
async def naomy_links(request: Request, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    from app.models.contact_point import ContactPoint

    limit = _limit(request.query_params.get("limite"))
    after = nb.decode_cursor(request.query_params.get("curseur"))
    stmt = select(ContactPoint).where(ContactPoint.tenant_id == ctx.tenant.id, ContactPoint.created_by == "NAOMY")
    if after:
        try:
            stmt = stmt.where(ContactPoint.id > uuid.UUID(after))
        except ValueError as exc:
            raise NaomyError(400, "curseur_invalide", "Curseur invalide.") from exc
    rows = (await db.execute(stmt.order_by(ContactPoint.id).limit(limit + 1))).scalars().all()
    page = rows[:limit]
    return {"elements": [nb.link_view(cp) for cp in page],
            "curseur_suivant": nb.encode_cursor(str(page[-1].id)) if len(rows) > limit else None}


@router.patch(API + "/liens/{code}")
async def naomy_update_link(code: str, request: Request, ctx: NaomyContext = Depends(naomy_context),
                            db: AsyncSession = Depends(get_db)):
    from app.models.contact_point import ContactPoint

    data = await _json(request)
    if set(data) != {"actif"} or not isinstance(data["actif"], bool):
        raise NaomyError(400, "requete_invalide", "Champ attendu : actif (vrai ou faux).")
    cp = (await db.execute(select(ContactPoint).where(ContactPoint.tenant_id == ctx.tenant.id, ContactPoint.code == code,
                                                      ContactPoint.created_by == "NAOMY"))).scalar_one_or_none()
    if cp is None:
        raise NaomyError(404, "introuvable", "Lien inconnu, ou lien qui n'a pas été créé par Naomy.")
    cp.active = data["actif"]
    await db.commit()
    return nb.link_view(cp)


# --- Résultats, ventes, rappels --------------------------------------------------------------------------------------

@router.get(f"{API}/resultats")
async def naomy_results(request: Request, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    return await nb.results(db, ctx.tenant, request.query_params.get("du"), request.query_params.get("au"))


@router.get(f"{API}/ventes")
async def naomy_sales(request: Request, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    q = request.query_params
    return await nb.sales(db, ctx.tenant, q.get("du"), q.get("au"), q.get("produit_id"))


@router.post(f"{API}/rappels", status_code=202)
async def naomy_reminder(request: Request, ctx: NaomyContext = Depends(naomy_context), db: AsyncSession = Depends(get_db)):
    connection = await db.get(type(ctx.connection), ctx.connection.id)
    result = await nb.send_reminder(db, ctx.tenant, connection, await _json(request))
    return JSONResponse(result, status_code=202)


# --- Tableau de bord de Bob : carte « Naomy » ------------------------------------------------------------------------

class RemindersSetting(BaseModel):
    enabled: bool


class PairingChoice(BaseModel):  # lot 92 : portée choisie par le commerçant
    scope: str = "complete"


class RequestDecision(BaseModel):
    accept: bool
    scope: str = "complete"


async def _tenant(db, current_user):
    from app.models.tenant import Tenant

    return await db.get(Tenant, current_user.tenant_id)


@router.get("/api/v1/naomy-connection")
async def naomy_connection_status(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await nb.dashboard_view(db, await _tenant(db, current_user))


@router.post("/api/v1/naomy-connection/pairing", dependencies=[Depends(require_role("ADMIN"))])
async def naomy_connection_pairing(payload: PairingChoice | None = None, current_user: CurrentUser = Depends(get_current_user),
                                   db: AsyncSession = Depends(get_db)):
    tenant = await _tenant(db, current_user)
    scope = (payload or PairingChoice()).scope
    refusal = nb.pairing_refusal(tenant, scope)
    if refusal:
        raise HTTPException(status_code=403, detail=refusal)
    return {"url": await nb.create_pairing(db, tenant, scope=scope)}


@router.post("/api/v1/naomy-connection/request/{request_id}", dependencies=[Depends(require_role("ADMIN"))])
async def naomy_connection_request(request_id: str, payload: RequestDecision,
                                   current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Lot 92 — demande venue de Naomy : accepter (lien de connexion habituel, portée choisie) ou refuser."""
    tenant = await _tenant(db, current_user)
    if payload.accept:
        refusal = nb.pairing_refusal(tenant, payload.scope)
        if refusal:
            raise HTTPException(status_code=403, detail=refusal)
    try:
        url = await nb.decide_request(db, tenant, request_id, payload.accept, payload.scope)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"url": url, "status": await nb.dashboard_view(db, tenant)}


class AssistantSetting(BaseModel):
    enabled: bool | None = None
    summary: bool | None = None


@router.put("/api/v1/naomy-connection/assistant", dependencies=[Depends(require_role("ADMIN"))])
async def naomy_connection_assistant(payload: AssistantSetting, current_user: CurrentUser = Depends(get_current_user),
                                     db: AsyncSession = Depends(get_db)):
    """Lot 92 — interrupteur de « Bob, votre assistant » pour la boutique, et bilan du soir par e-mail."""
    from app.core.config import get_settings

    tenant = await _tenant(db, current_user)
    connection = await nb.active_connection(db, tenant.id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Naomy n'est pas reliée à votre boutique.")
    if payload.enabled is not None:
        if payload.enabled and not get_settings().assistant_enabled:
            raise HTTPException(status_code=403, detail="L'assistant sur WhatsApp arrive bientôt.")
        if payload.enabled and not connection.assistant_enabled:
            connection.assistant_enabled_at = datetime.now(timezone.utc)  # aucune alerte sur ce qui précède
        connection.assistant_enabled = payload.enabled
    if payload.summary is not None:
        connection.summary_enabled = payload.summary
    await db.commit()
    return await nb.dashboard_view(db, tenant)


@router.delete("/api/v1/naomy-connection", dependencies=[Depends(require_role("ADMIN"))])
async def naomy_connection_close(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tenant = await _tenant(db, current_user)
    connection = await nb.active_connection(db, tenant.id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Naomy n'est pas reliée à votre boutique.")
    nb.close(db, connection, "BOB")
    await db.commit()
    return await nb.dashboard_view(db, tenant)


@router.put("/api/v1/naomy-connection/reminders", dependencies=[Depends(require_role("ADMIN"))])
async def naomy_connection_reminders(payload: RemindersSetting, current_user: CurrentUser = Depends(get_current_user),
                                     db: AsyncSession = Depends(get_db)):
    tenant = await _tenant(db, current_user)
    connection = await nb.active_connection(db, tenant.id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Naomy n'est pas reliée à votre boutique.")
    connection.reminders_enabled = payload.enabled
    if payload.enabled:
        connection.reminders_unsubscribed_at = None  # le commerçant les rallume lui-même
    await db.commit()
    return await nb.dashboard_view(db, tenant)


_UNSUB_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rappels de Naomy</title><style>body{{font-family:system-ui,sans-serif;background:#F5F8F6;color:#13241A;margin:0;padding:48px 16px}}
main{{max-width:480px;margin:0 auto;background:#fff;border:1px solid #E3EBE6;border-radius:16px;padding:28px}}
@media (prefers-color-scheme:dark){{body{{background:#0D1511;color:#E4EEE8}}main{{background:#15201A;border-color:#24342B}}}}</style></head>
<body><main><h1 style="font-size:20px;margin:0 0 12px">{title}</h1><p style="margin:0;line-height:1.6">{text}</p></main></body></html>"""


@router.api_route("/api/v1/naomy-rappels/desinscription", methods=["GET", "POST"], response_class=HTMLResponse)
async def naomy_reminders_unsubscribe(c: str = "", t: str = "", db: AsyncSession = Depends(get_db)):
    if await nb.unsubscribe(db, c, t):
        return HTMLResponse(_UNSUB_PAGE.format(
            title="C'est noté", text="Vous ne recevrez plus d'e-mails de rappel pour les propositions de Naomy. "
                                     "Vous pouvez les rallumer dans Bob, onglet Intégrations, carte « Naomy »."))
    return HTMLResponse(_UNSUB_PAGE.format(title="Lien invalide", text="Ce lien de désinscription n'est pas valide."),
                        status_code=404)
