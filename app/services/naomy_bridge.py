"""
Lot 89 — connecteur Naomy : ce que Bob expose à Naomy et ce qu'il lui envoie (contrat « Passerelle Bob ↔ Naomy » 1.2,
docs/naomy/bob-naomy-openapi-v1.2.yaml).

Principes (contrat, section 1) : Bob reste la source de vérité ; Naomy lit, et n'écrit que ses liens suivis et ses
demandes de rappel ; aucune donnée personnelle d'un client ne sort (seulement des produits et des chiffres agrégés) ;
rien ne change pour une boutique qui ne relie pas Naomy.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import string
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from sqlalchemy import and_, case, event, func, or_, select, update
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

KEY_PREFIX, SECRET_PREFIX = "bnk_", "bns_"
CODE_LENGTH = 32
CODE_TTL = timedelta(minutes=15)
TIMESTAMP_TOLERANCE = 300  # secondes
RETRY_MINUTES = [1, 5, 30, 120, 720]
PER_TENANT_PER_PASS = 50
RATE_LIMIT_PER_MINUTE = 120
MAX_PAGE = 200
MAX_PERIOD_DAYS = 92
PLANS_WITH_NAOMY = {"INDEPENDANT", "STARTER", "PRO", "ENTERPRISE"}  # décision du 26/10 : à partir d'Indépendant
EVENT_TYPES = {"connexion.ouverte", "connexion.fermee", "boutique.formule_changee", "produit.cree",
               "produit.photo_prete", "produit.prix_baisse", "produit.prix_barre_ajoute", "produit.rupture",
               "produit.reapprovisionne", "produit.dormant", "produit.meilleure_vente"}
_ALNUM = string.ascii_letters + string.digits


class NaomyError(Exception):
    """Erreur au format du contrat : {"erreur": code, "message": phrase}."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


# --- Petits outils ---------------------------------------------------------------------------------------------------

def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(CODE_LENGTH))


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def platform_signature_ok(secret: str, timestamp: str | None, signature: str | None, body: bytes,
                          now: datetime | None = None) -> bool:
    if not secret or not timestamp or not signature or not timestamp.isdigit():
        return False
    now_s = int((now or datetime.now(timezone.utc)).timestamp())
    if abs(now_s - int(timestamp)) > TIMESTAMP_TOLERANCE:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def events_url_ok(url: str, host: str, path: str) -> bool:
    """HTTPS, le domaine de Naomy ET son chemin (Bob et Naomy partagent agenc-ai.com), sans port, identifiant ni « .. »."""
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and (parts.hostname or "").lower() == host.lower() and port is None
            and not parts.username and not parts.password and parts.path.startswith(path)
            and ".." not in parts.path and "%2e" not in parts.path.lower() and "\\" not in url and not parts.fragment)


def mask_number(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 6:
        return "•••"
    return f"+{digits[:5]} •• •• {digits[-2:]}"


def plan_code(tenant) -> str:
    plan = getattr(tenant, "plan", None)
    return str(getattr(plan, "value", plan) or "FREE")


def amount_text(value) -> str:
    try:
        number = Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        number = Decimal(0)
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def money(value, currency: str) -> dict:
    return {"montant": amount_text(value), "devise": (currency or "XOF").upper()}


def absolute(url: str | None) -> str | None:
    from app.core.config import get_settings

    if not url:
        return None
    if url.startswith("/"):
        return get_settings().public_base_url.rstrip("/") + url
    return url


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except Exception as exc:  # noqa: BLE001
        raise NaomyError(400, "curseur_invalide", "Curseur invalide.") from exc


# --- Vues exposées à Naomy -------------------------------------------------------------------------------------------

async def shop_view(db, tenant) -> dict:
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.wa_account_info import profile_url

    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalars().first()
    return {"id": str(tenant.id), "nom": tenant.name, "secteur": tenant.business_type or "ONLINE_STORE",
            "pays": (tenant.country or "").upper(), "devise": (tenant.currency or "XOF").upper(),
            "tutoiement": (tenant.address_form or "VOUS") == "TU", "logo": absolute(profile_url(tenant.id)),
            "formule": plan_code(tenant),
            "whatsapp_affiche": account.display_phone_number if account is not None else None}


def photos_view(product) -> dict:
    return {"originale": absolute(product.image_original_url or product.image_url),
            "studio": absolute(product.image_studio_url) if product.image_status == "DONE" else None,
            "portee": absolute(product.worn_image_url) if product.worn_status == "ACCEPTED" else None,
            "carte": absolute(product.image_url)}


def product_view(product, insurance: bool, category_name: str | None = None) -> dict:
    sheet = dict(product.sheet or {})
    if product.vehicle:
        sheet.update(product.vehicle)
    compare_at = sheet.pop("compare_at_price", None)
    view = {"id": str(product.id), "reference": product.sku, "nom": product.name, "description": product.description,
            "categorie_bob": product.bob_category, "rayon": category_name, "actif": bool(product.active),
            "fiche": sheet, "photos": photos_view(product), "modifie_le": iso(product.updated_at or product.created_at)}
    if insurance:
        view.update(prix=None, prix_barre=None, stock=None)
    else:
        barre = None
        try:
            if compare_at not in (None, "") and Decimal(str(compare_at)) > Decimal(str(product.price)):
                barre = money(compare_at, product.currency)
        except InvalidOperation:
            barre = None
        view.update(prix=money(product.price, product.currency), prix_barre=barre, stock=int(product.stock_quantity or 0))
    return view


def link_view(cp) -> dict:
    from app.core.config import get_settings

    return {"code": cp.code, "url": f"{get_settings().public_base_url.rstrip('/')}/w/{cp.code}", "actif": bool(cp.active),
            "clics": int(cp.click_count or 0), "nom": cp.name, "produit_id": str(cp.product_id) if cp.product_id else None,
            "reference_naomy": cp.naomy_reference, "cree_le": iso(cp.created_at)}


# --- Liaison ---------------------------------------------------------------------------------------------------------

async def active_connection(db, tenant_id):
    from app.models.naomy import NaomyConnection

    return (await db.execute(select(NaomyConnection).where(NaomyConnection.tenant_id == tenant_id,
                                                           NaomyConnection.status == "ACTIVE")
                             .order_by(NaomyConnection.connected_at.desc()))).scalars().first()


def add_event(session, tenant_id, kind: str, data: dict | None = None, product_id=None, connection_id=None) -> None:
    from app.models.naomy import NaomyEvent

    session.add(NaomyEvent(id=uuid.uuid4(), tenant_id=tenant_id, type=kind, data=data or {}, product_id=product_id,
                           connection_id=connection_id, status="PENDING", attempts=0,
                           created_at=datetime.now(timezone.utc)))


def pairing_refusal(tenant) -> str | None:
    from app.core.config import get_settings

    if not get_settings().naomy_platform_secret:
        return "La connexion à Naomy sera bientôt disponible."
    if tenant.is_demo:
        return "La connexion à Naomy n'est pas disponible pour une démonstration."
    if plan_code(tenant) not in PLANS_WITH_NAOMY:
        return "Naomy est incluse à partir de la formule Indépendant."
    return None


async def create_pairing(db, tenant, now: datetime | None = None) -> str:
    from app.core.config import get_settings
    from app.models.naomy import NaomyPairingCode

    now = now or datetime.now(timezone.utc)
    code = new_code()
    db.add(NaomyPairingCode(tenant_id=tenant.id, code_hash=digest(code), expires_at=now + CODE_TTL))
    await db.commit()
    return f"{get_settings().naomy_base_url.rstrip('/')}/connexion/bob?code={code}"


def close(session, connection, by: str, now: datetime | None = None) -> None:
    connection.status, connection.closed_by = "CLOSED", by
    connection.closed_at = now or datetime.now(timezone.utc)
    add_event(session, connection.tenant_id, "connexion.fermee", {"par": by}, connection_id=connection.id)


async def pair(db, payload: dict, now: datetime | None = None) -> dict:
    """Échange le code contre la clé (une seule fois). Le corps est déjà authentifié par la signature de plateforme."""
    from app.core.config import get_settings
    from app.models.naomy import NaomyConnection, NaomyPairingCode
    from app.models.tenant import Tenant

    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    code = payload.get("code")
    account = payload.get("compte_naomy")
    number = payload.get("numero_confirmation")
    url = payload.get("url_evenements")
    if not (isinstance(code, str) and len(code) == CODE_LENGTH and isinstance(account, str) and 0 < len(account) <= 64
            and isinstance(number, str) and re.fullmatch(r"\+[1-9][0-9]{7,14}", number) and isinstance(url, str)):
        raise NaomyError(400, "requete_invalide", "Champs attendus : code (32), compte_naomy, numero_confirmation (+…), url_evenements.")
    record = (await db.execute(select(NaomyPairingCode).where(NaomyPairingCode.code_hash == digest(code)))).scalar_one_or_none()
    expires = record.expires_at.replace(tzinfo=timezone.utc) if record is not None and record.expires_at.tzinfo is None \
        else (record.expires_at if record is not None else None)
    if record is None or (record.used_at is None and expires < now):
        raise NaomyError(404, "code_inconnu", "Code d'appairage inconnu ou expiré : relancez la connexion depuis Bob.")
    if record.used_at is not None:
        raise NaomyError(409, "code_deja_utilise", "Ce code d'appairage a déjà servi.")
    tenant = await db.get(Tenant, record.tenant_id)
    if tenant is None or tenant.is_demo:
        raise NaomyError(404, "code_inconnu", "Code d'appairage inconnu ou expiré : relancez la connexion depuis Bob.")
    if plan_code(tenant) not in PLANS_WITH_NAOMY:
        raise NaomyError(403, "formule_insuffisante", "Naomy est incluse à partir de la formule Indépendant.")
    if not events_url_ok(url, settings.naomy_allowed_host, settings.naomy_allowed_path):
        raise NaomyError(422, "url_evenements_refusee",
                         f"L'adresse doit commencer par https://{settings.naomy_allowed_host}{settings.naomy_allowed_path}.")
    record.used_at = now
    previous = await active_connection(db, tenant.id)
    if previous is not None:  # un seul compte Naomy par boutique (phase 1) : le nouveau remplace l'ancien
        close(db, previous, "BOB", now)
    key, secret = KEY_PREFIX + secrets.token_urlsafe(32), SECRET_PREFIX + secrets.token_urlsafe(32)
    connection = NaomyConnection(id=uuid.uuid4(), tenant_id=tenant.id, status="ACTIVE", naomy_account=account,
                                 confirmation_number=number, events_url=url, api_key_hash=digest(key),
                                 events_secret=secret, connected_at=now)
    db.add(connection)
    add_event(db, tenant.id, "connexion.ouverte", {}, connection_id=connection.id)
    await db.commit()
    return {"boutique_id": str(tenant.id), "cle_api": key, "secret_evenements": secret, "boutique": await shop_view(db, tenant)}


async def connection_for_key(db, key: str | None):
    from app.models.naomy import NaomyConnection

    if not key or not key.startswith(KEY_PREFIX):
        return None
    return (await db.execute(select(NaomyConnection).where(NaomyConnection.api_key_hash == digest(key),
                                                           NaomyConnection.status == "ACTIVE"))).scalar_one_or_none()


# --- Événements : détectés à l'enregistrement, quel que soit le chemin (tableau de bord, import, Shopify, Meta, IA) ---

def _change(state, name):
    history = state.attrs[name].history
    if not history.has_changes():
        return None
    old = history.deleted[0] if history.deleted else None
    new = history.added[0] if history.added else None
    return old, new


def _number(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def collect_events(session) -> None:
    from sqlalchemy import inspect

    from app.models.product import Product
    from app.models.tenant import Tenant

    for obj in list(session.new):
        if isinstance(obj, Product) and obj.active and obj.image_url and obj.tenant_id:
            if obj.id is None:
                obj.id = uuid.uuid4()
            add_event(session, obj.tenant_id, "produit.cree", {"produit_id": str(obj.id)}, product_id=obj.id)
    for obj in list(session.dirty):
        if isinstance(obj, Tenant):
            state = inspect(obj)
            change = _change(state, "plan")
            if change and change[0] is not None and str(getattr(change[0], "value", change[0])) != str(getattr(change[1], "value", change[1])):
                old, new = (str(getattr(v, "value", v)) for v in change)
                add_event(session, obj.id, "boutique.formule_changee", {"ancienne": old, "nouvelle": new})
            continue
        if not isinstance(obj, Product) or not session.is_modified(obj, include_collections=False):
            continue
        state = inspect(obj)
        pid, tid = obj.id, obj.tenant_id

        def emit(kind, data=None):
            add_event(session, tid, kind, {"produit_id": str(pid), **(data or {})}, product_id=pid)

        price = _change(state, "price")
        if price and _number(price[0]) is not None and _number(price[1]) is not None and _number(price[1]) < _number(price[0]):
            emit("produit.prix_baisse", {"ancien_prix": money(price[0], obj.currency), "nouveau_prix": money(price[1], obj.currency)})
        stock, active = _change(state, "stock_quantity"), _change(state, "active")
        was_selling = ((stock[0] if stock else obj.stock_quantity) or 0) > 0 and (active[0] if active else obj.active)
        selling = (obj.stock_quantity or 0) > 0 and obj.active
        if (stock or active) and was_selling and not selling:
            emit("produit.rupture")
        elif (stock or active) and not was_selling and selling:
            emit("produit.reapprovisionne", {"stock": int(obj.stock_quantity)})
        image = _change(state, "image_url")
        if image and not image[0] and image[1] and obj.active:
            emit("produit.cree")  # première photo d'un produit créé sans photo (doublon écarté à l'envoi)
        sheet = _change(state, "sheet")
        if sheet:
            before = _number((sheet[0] or {}).get("compare_at_price")) if isinstance(sheet[0], dict) else None
            after = _number((sheet[1] or {}).get("compare_at_price")) if isinstance(sheet[1], dict) else None
            if before is None and after is not None:
                emit("produit.prix_barre_ajoute", {"prix_barre": money(after, obj.currency)})
        studio, worn = _change(state, "image_status"), _change(state, "worn_status")
        if studio and studio[1] == "DONE" and studio[0] != "DONE":
            emit("produit.photo_prete", {"type": "studio"})
        if worn and worn[1] == "ACCEPTED" and worn[0] != "ACCEPTED":
            emit("produit.photo_prete", {"type": "portee"})


@event.listens_for(Session, "before_flush")
def _naomy_before_flush(session, flush_context, instances) -> None:
    try:
        collect_events(session)
    except Exception:  # noqa: BLE001 — jamais un enregistrement de produit bloqué par Naomy
        logger.warning("Événements Naomy non préparés", exc_info=True)


# --- Envoi des événements (tâche chaque minute) ---------------------------------------------------------------------

def event_body(evt) -> bytes:
    return json.dumps({"id": str(evt.id), "type": evt.type, "boutique_id": str(evt.tenant_id), "date": iso(evt.created_at),
                       "donnees": evt.data or {}}, ensure_ascii=False, separators=(",", ":")).encode()


async def http_post(url: str, body: bytes, headers: dict) -> int:
    import httpx

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.post(url, content=body, headers=headers)
    return response.status_code


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def dispatch(db, now: datetime | None = None, sender=None) -> dict:
    from app.models.naomy import NaomyConnection, NaomyEvent

    now = now or datetime.now(timezone.utc)
    sender = sender or http_post
    report = {"sent": 0, "failed": 0, "retry": 0, "skipped": 0}
    active = select(NaomyConnection.tenant_id).where(NaomyConnection.status == "ACTIVE")
    skipped = await db.execute(update(NaomyEvent).where(
        NaomyEvent.status == "PENDING", NaomyEvent.connection_id.is_(None), NaomyEvent.tenant_id.not_in(active))
        .values(status="SKIPPED"))
    report["skipped"] += skipped.rowcount or 0
    due = (await db.execute(select(NaomyEvent).where(
        NaomyEvent.status == "PENDING", or_(NaomyEvent.next_attempt_at.is_(None), NaomyEvent.next_attempt_at <= now))
        .order_by(NaomyEvent.created_at).limit(500))).scalars().all()
    per_tenant: dict = {}
    connections: dict = {}
    for evt in due:
        if per_tenant.get(evt.tenant_id, 0) >= PER_TENANT_PER_PASS:
            continue
        if evt.connection_id is not None:
            connection = await db.get(NaomyConnection, evt.connection_id)
        else:
            if evt.tenant_id not in connections:
                connections[evt.tenant_id] = await active_connection(db, evt.tenant_id)
            connection = connections[evt.tenant_id]
            if connection is not None and _aware(evt.created_at) < _aware(connection.connected_at):
                connection = None  # antérieur à la liaison : Naomy relit le catalogue de toute façon
        if connection is None:
            evt.status = "SKIPPED"
            report["skipped"] += 1
            continue
        if evt.type == "produit.cree" and evt.product_id is not None:
            already = (await db.execute(select(NaomyEvent.id).where(
                NaomyEvent.tenant_id == evt.tenant_id, NaomyEvent.type == "produit.cree", NaomyEvent.product_id == evt.product_id,
                NaomyEvent.id != evt.id, NaomyEvent.status.in_(("SENT", "PENDING")),
                NaomyEvent.created_at <= evt.created_at).limit(1))).first()
            if already:
                evt.status = "SKIPPED"
                report["skipped"] += 1
                continue
        per_tenant[evt.tenant_id] = per_tenant.get(evt.tenant_id, 0) + 1
        body = event_body(evt)
        stamp = str(int(now.timestamp()))
        headers = {"Content-Type": "application/json", "X-Bob-Timestamp": stamp,
                   "X-Bob-Signature": sign(connection.events_secret, stamp, body), "User-Agent": "Bob-Naomy/1"}
        try:
            status = await sender(connection.events_url, body, headers)
            error = None if 200 <= status < 300 else f"Naomy a répondu {status}"
        except Exception as exc:  # noqa: BLE001
            status, error = None, f"Naomy injoignable ({type(exc).__name__})"
        evt.attempts = (evt.attempts or 0) + 1
        if error is None:
            evt.status, evt.sent_at, evt.last_error = "SENT", now, None
            report["sent"] += 1
        elif status == 401 or evt.attempts > len(RETRY_MINUTES):
            evt.status, evt.last_error = "FAILED", error
            report["failed"] += 1
            logger.warning("Envoi d'un événement à Naomy en échec, abandonné (%s, boutique %s) : %s",
                           evt.type, evt.tenant_id, error)
        else:
            evt.next_attempt_at, evt.last_error = now + timedelta(minutes=RETRY_MINUTES[evt.attempts - 1]), error
            report["retry"] += 1
    await db.commit()
    return report


async def purge(db, now: datetime | None = None) -> dict:
    from sqlalchemy import delete

    from app.models.naomy import NaomyEvent, NaomyPairingCode

    now = now or datetime.now(timezone.utc)
    done = await db.execute(delete(NaomyEvent).where(or_(
        and_(NaomyEvent.status == "SKIPPED", NaomyEvent.created_at < now - timedelta(days=2)),
        and_(NaomyEvent.status.in_(("SENT", "FAILED")), NaomyEvent.created_at < now - timedelta(days=30)))))
    codes = await db.execute(delete(NaomyPairingCode).where(NaomyPairingCode.expires_at < now - timedelta(days=1)))
    await db.commit()
    return {"events": done.rowcount or 0, "codes": codes.rowcount or 0}


# --- Résultats et ventes (chiffres agrégés seulement) ----------------------------------------------------------------

def period(tenant, du: str | None, au: str | None) -> tuple[date, date, datetime, datetime]:
    from app.services.local_time import tenant_zone

    try:
        start, end = date.fromisoformat(du or ""), date.fromisoformat(au or "")
    except ValueError as exc:
        raise NaomyError(400, "requete_invalide", "Dates attendues : du et au (AAAA-MM-JJ).") from exc
    if end < start or (end - start).days >= MAX_PERIOD_DAYS:
        raise NaomyError(400, "requete_invalide", "Période de 92 jours au plus, « au » après « du ».")
    zone = tenant_zone(tenant)
    since = datetime.combine(start, time.min, zone).astimezone(timezone.utc)
    until = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start, end, since, until


async def _counters(db, tenant, key, since, until, extra=None) -> dict:
    """Compteurs par source d'acquisition (clé = colonne du client : lien ou publicité)."""
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    from app.models.order import Order, OrderStatus

    base = [Customer.tenant_id == tenant.id, key.is_not(None)] + (extra or [])
    out: dict = {}

    def slot(k):
        return out.setdefault(k, {"conversations": 0, "nouveaux_clients": 0, "commandes": 0, "commandes_payees": 0,
                                  "ventes_payees": Decimal(0)})

    for k, n in (await db.execute(select(key, func.count(Customer.id)).where(
            *base, Customer.created_at >= since, Customer.created_at < until).group_by(key))).all():
        slot(k)["nouveaux_clients"] = int(n)
    for k, n in (await db.execute(select(key, func.count(Conversation.id)).join(Customer, Customer.id == Conversation.customer_id)
                                  .where(*base, Conversation.created_at >= since, Conversation.created_at < until)
                                  .group_by(key))).all():
        slot(k)["conversations"] = int(n)
    paid = Order.status == OrderStatus.PAID
    rows = (await db.execute(select(key, func.count(Order.id), func.sum(case((paid, 1), else_=0)),
                                    func.sum(case((paid, Order.total_amount), else_=0)))
                             .join(Customer, Customer.id == Order.customer_id)
                             .where(*base, Order.created_at >= since, Order.created_at < until).group_by(key))).all()
    for k, n, n_paid, total in rows:
        entry = slot(k)
        entry.update(commandes=int(n), commandes_payees=int(n_paid or 0), ventes_payees=Decimal(str(total or 0)))
    return out


def _counter_view(entry: dict | None, currency: str) -> dict:
    entry = entry or {"conversations": 0, "nouveaux_clients": 0, "commandes": 0, "commandes_payees": 0,
                      "ventes_payees": Decimal(0)}
    return {**{k: v for k, v in entry.items() if k != "ventes_payees"}, "ventes_payees": money(entry["ventes_payees"], currency)}


async def results(db, tenant, du, au) -> dict:
    from app.models.contact_point import ContactPoint
    from app.models.customer import Customer

    start, end, since, until = period(tenant, du, au)
    links = (await db.execute(select(ContactPoint).where(ContactPoint.tenant_id == tenant.id,
                                                         ContactPoint.created_by == "NAOMY")
                              .order_by(ContactPoint.created_at))).scalars().all()
    by_link = await _counters(db, tenant, Customer.acquisition_contact_point_id, since, until,
                              [Customer.acquisition_contact_point_id.in_([cp.id for cp in links] or [uuid.uuid4()])])
    by_ad = await _counters(db, tenant, Customer.acquisition_ad_id, since, until)
    currency = tenant.currency or "XOF"
    return {"du": start.isoformat(), "au": end.isoformat(),
            "liens": [{"code": cp.code, "reference_naomy": cp.naomy_reference, "clics": int(cp.click_count or 0),
                       **_counter_view(by_link.get(cp.id), currency)} for cp in links],
            "publicites": [{"id_publicite_meta": ad, **_counter_view(entry, currency)} for ad, entry in sorted(by_ad.items())]}


async def sales(db, tenant, du, au, product_id: str | None = None) -> dict:
    from app.models.order import Order, OrderItem, OrderStatus

    start, end, since, until = period(tenant, du, au)
    when = func.coalesce(Order.paid_at, Order.created_at)
    filters = [Order.tenant_id == tenant.id, Order.status == OrderStatus.PAID, when >= since, when < until]
    if product_id:
        try:
            filters.append(OrderItem.product_id == uuid.UUID(product_id))
        except ValueError as exc:
            raise NaomyError(400, "requete_invalide", "produit_id invalide.") from exc
    total = func.sum(OrderItem.subtotal)
    rows = (await db.execute(select(OrderItem.product_id, func.count(func.distinct(Order.id)), func.sum(OrderItem.quantity), total)
                             .join(Order, Order.id == OrderItem.order_id).where(*filters)
                             .group_by(OrderItem.product_id).order_by(total.desc()))).all()
    currency = tenant.currency or "XOF"
    return {"du": start.isoformat(), "au": end.isoformat(),
            "elements": [{"produit_id": str(p), "commandes_payees": int(n), "quantite": int(q or 0),
                          "ventes_payees": money(s, currency)} for p, n, q, s in rows]}


# --- Rappels par e-mail (Naomy envoie des faits ; Bob écrit l'e-mail) ------------------------------------------------

def unsubscribe_token(connection_id) -> str:
    from app.core.config import get_settings

    return hmac.new(get_settings().secret_key.encode(), f"naomy-rappels:{connection_id}".encode(), hashlib.sha256).hexdigest()[:32]


def unsubscribe_url(connection) -> str:
    from app.core.config import get_settings

    return (f"{get_settings().public_base_url.rstrip('/')}/api/v1/naomy-rappels/desinscription"
            f"?c={connection.id}&t={unsubscribe_token(connection.id)}")


def _names(names: list[str]) -> str:
    quoted = [f"« {n} »" for n in names]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " et " + quoted[-1]


def reminder_email(tenant, connection, count: int, names: list[str], number: str) -> tuple[str, str, dict]:
    from app.services.email_layout import FOOTER_SEPARATOR

    digits = re.sub(r"\D", "", number)
    plural = "s" if count > 1 else ""
    subject = f"Naomy a préparé {count} proposition{plural} pour vous"
    about = f" pour vos produits {_names(names)}" if len(names) > 1 else (f" pour votre produit {_names(names)}" if names else "")
    unsub = unsubscribe_url(connection)
    body = (f"Bonjour,\n\nNaomy a préparé {count} proposition{plural} de publication{about}.\n\n"
            f"Pour les voir, écrivez « posts » à Naomy sur WhatsApp : https://wa.me/{digits}?text=posts\n\n"
            f"{FOOTER_SEPARATOR}\nVous recevez cet e-mail car {tenant.name} est reliée à Naomy dans Bob.\n"
            f"Ne plus recevoir ces e-mails : {unsub}")
    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return subject, body, headers


async def send_reminder(db, tenant, connection, payload: dict, now: datetime | None = None, mailer=None) -> dict:
    import asyncio

    from app.core.config import get_settings
    from app.models.naomy import NaomyReminder
    from app.models.product import Product
    from app.services.local_time import tenant_zone

    now = now or datetime.now(timezone.utc)
    reference, count, ids = payload.get("reference_naomy"), payload.get("nombre_propositions"), payload.get("produit_ids") or []
    if not (isinstance(reference, str) and 0 < len(reference) <= 64 and isinstance(count, int) and not isinstance(count, bool)
            and 1 <= count <= 50 and isinstance(ids, list) and len(ids) <= 3 and set(payload) <= {"reference_naomy", "nombre_propositions", "produit_ids"}):
        raise NaomyError(400, "requete_invalide", "Champs attendus : reference_naomy, nombre_propositions (1 à 50), produit_ids (3 au plus).")
    try:
        product_ids = [uuid.UUID(str(i)) for i in ids]
    except ValueError as exc:
        raise NaomyError(400, "requete_invalide", "produit_ids : identifiants invalides.") from exc
    done = (await db.execute(select(NaomyReminder).where(NaomyReminder.tenant_id == tenant.id,
                                                         NaomyReminder.reference == reference))).scalar_one_or_none()
    if done is not None:
        return {"reference_naomy": reference, "statut": "deja_envoye"}
    if not (tenant.email or "").strip():
        raise NaomyError(409, "sans_email", "Cette boutique n'a pas d'adresse e-mail vérifiée.")
    if connection.reminders_unsubscribed_at is not None:
        raise NaomyError(409, "desinscrit", "Le commerçant s'est désinscrit de ces e-mails.")
    if not connection.reminders_enabled:
        raise NaomyError(409, "rappels_desactives", "Le commerçant a désactivé ces e-mails.")
    number = get_settings().naomy_whatsapp_number
    if not re.sub(r"\D", "", number or ""):
        raise NaomyError(503, "rappels_indisponibles", "Les rappels par e-mail ne sont pas encore activés.")
    zone = tenant_zone(tenant)
    today = now.astimezone(zone).date()
    if (await db.execute(select(NaomyReminder.id).where(NaomyReminder.tenant_id == tenant.id, NaomyReminder.sent_on == today,
                                                        NaomyReminder.sent.is_(True)).limit(1))).first():
        tomorrow = datetime.combine(today + timedelta(days=1), time.min, zone)
        raise NaomyError(429, "rappel_deja_envoye_aujourd_hui", "Un e-mail a déjà été envoyé aujourd'hui.",
                         {"Retry-After": str(max(60, int((tomorrow - now).total_seconds())))})
    names = []
    if product_ids:
        found = {p.id: p.name for p in (await db.execute(select(Product).where(
            Product.tenant_id == tenant.id, Product.id.in_(product_ids), Product.active.is_(True)))).scalars()}
        names = [found[i] for i in product_ids if i in found]
    subject, body, headers = reminder_email(tenant, connection, count, names, number)
    if mailer is None:
        from app.services.email_service import send_email as mailer
    ok = await asyncio.to_thread(mailer, to=tenant.email, subject=subject, body=body, extra_headers=headers)
    if not ok:
        raise NaomyError(503, "envoi_impossible", "L'e-mail n'a pas pu partir, réessayez plus tard.")
    db.add(NaomyReminder(tenant_id=tenant.id, reference=reference, sent_on=today, sent=True))
    await db.commit()
    return {"reference_naomy": reference, "statut": "accepte"}


async def unsubscribe(db, connection_id: str, token: str) -> bool:
    from app.models.naomy import NaomyConnection

    try:
        cid = uuid.UUID(connection_id)
    except (ValueError, TypeError):
        return False
    if not hmac.compare_digest(unsubscribe_token(cid), token or ""):
        return False
    connection = await db.get(NaomyConnection, cid)
    if connection is None:
        return False
    if connection.reminders_unsubscribed_at is None:
        connection.reminders_unsubscribed_at = datetime.now(timezone.utc)
        await db.commit()
    return True


# --- Ce que le tableau de bord de Bob montre ---------------------------------------------------------------------------

async def dashboard_view(db, tenant) -> dict:
    connection = await active_connection(db, tenant.id)
    refusal = pairing_refusal(tenant)
    return {"available": refusal is None or connection is not None, "refusal": refusal if connection is None else None,
            "connected": connection is not None,
            "number": mask_number(connection.confirmation_number) if connection else None,
            "connected_at": iso(connection.connected_at) if connection else None,
            "reminders_enabled": bool(connection.reminders_enabled and connection.reminders_unsubscribed_at is None)
            if connection else None,
            "suspended": connection is not None and plan_code(tenant) not in PLANS_WITH_NAOMY}
