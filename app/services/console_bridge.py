"""
Lot 120 — interrupteur entre les Super Admin de Bob et de Naomy (contrat 1.5, section « console »).

Décisions du 10/11 : l'équipe AgenC'AI se gère une seule fois, chez Bob (page « Équipe AgenC'AI ») ; mêmes rôles
partout ; un clic suffit pour passer d'une console à l'autre ; même structure, couleurs de chaque agent.

- Jeton de passage : `<charge>.<signature>`, charge = JSON en base64url {v, iss, aud, jti, iat, exp, membre}, signature
  = HMAC-SHA256(secret de plateforme, "console." + charge) en base64url. Valable 60 secondes, usage unique (jti gardé
  jusqu'à expiration), dans les deux sens.
- Entrée chez Bob : Bob fait foi. Le membre est retrouvé par son e-mail ; il doit être actif et avoir rejoint
  l'équipe ; ses rôles sont ceux de Bob (jamais ceux du jeton).
- Bob → Naomy : chaque changement d'un membre (a rejoint, rôles, désactivé, réactivé) part signé vers
  {naomy_base_url}/api/bob/console/membres, avec les mêmes renvois que les événements des boutiques.
  Naomy peut aussi relire toute l'équipe : GET /api/v1/naomy/console/membres (signature de plateforme).
"""
import base64
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select

logger = logging.getLogger(__name__)

TOKEN_TTL = 60
CLOCK_SKEW = 30
RETRY_MINUTES = [1, 5, 30, 120, 720]
AGENTS = {"bob": {"nom": "Bob", "couleur": "#40916C"}, "naomy": {"nom": "Naomy", "couleur": "#C9A84C"}}


class ConsoleError(Exception):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sig(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode(), f"console.{payload}".encode(), hashlib.sha256).digest())


def member_view(member) -> dict:
    from app.services import sa_roles

    return {"id": str(member.id), "email": member.email, "nom": member.full_name, "roles": sa_roles.clean(member.roles),
            "actif": bool(member.active and member.invite_token_hash is None)}


def make_token(secret: str, member, audience: str, now: datetime | None = None, issuer: str = "bob") -> str:
    now = now or datetime.now(timezone.utc)
    payload = {"v": 1, "iss": issuer, "aud": audience, "jti": uuid.uuid4().hex, "iat": int(now.timestamp()),
               "exp": int(now.timestamp()) + TOKEN_TTL, "membre": member_view(member)}
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    return f"{body}.{_sig(secret, body)}"


def read_token(secret: str, token: str, audience: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not secret or not isinstance(token, str) or len(token) > 4000 or token.count(".") != 1:
        raise ConsoleError("jeton illisible")
    body, signature = token.split(".")
    if not hmac.compare_digest(signature, _sig(secret, body)):
        raise ConsoleError("signature invalide")
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        raise ConsoleError("jeton illisible") from None
    stamp = int(now.timestamp())
    if payload.get("v") != 1 or payload.get("aud") != audience or not isinstance(payload.get("jti"), str) \
            or not isinstance(payload.get("membre"), dict):
        raise ConsoleError("jeton non destiné à cette console")
    if not (payload.get("iat", 0) - CLOCK_SKEW <= stamp <= payload.get("exp", 0) + CLOCK_SKEW) \
            or payload["exp"] - payload["iat"] > TOKEN_TTL:
        raise ConsoleError("jeton expiré")
    return payload


async def spend(db, payload: dict, now: datetime | None = None) -> None:
    """Usage unique : le jti est gardé jusqu'à expiration (et un peu plus)."""
    from app.models.console import ConsoleJump

    now = now or datetime.now(timezone.utc)
    jti = payload["jti"][:64]
    if await db.get(ConsoleJump, jti) is not None:
        raise ConsoleError("jeton déjà servi")
    db.add(ConsoleJump(jti=jti, expires_at=now + timedelta(seconds=TOKEN_TTL + 2 * CLOCK_SKEW)))
    await db.flush()


def console_url(agent: str) -> str | None:
    from app.core.config import get_settings

    settings = get_settings()
    if agent != "naomy" or not settings.naomy_platform_secret:
        return None
    return settings.naomy_console_url or f"{settings.naomy_base_url.rstrip('/')}/superadmin/"


def agents(current: str = "bob") -> list[dict]:
    rows = [{"code": "bob", **AGENTS["bob"], "actuel": current == "bob", "disponible": True}]
    rows.append({"code": "naomy", **AGENTS["naomy"], "actuel": current == "naomy", "disponible": console_url("naomy") is not None})
    return rows


def queue_member(db, member, reason: str) -> None:
    """Un changement de membre à transmettre à Naomy (seulement si la passerelle est allumée)."""
    from app.core.config import get_settings
    from app.models.console import ConsoleEvent

    if not get_settings().naomy_platform_secret:
        return
    db.add(ConsoleEvent(agent="naomy", data={"id": uuid.uuid4().hex, "type": "membre.mis_a_jour", "raison": reason,
                                             "date": datetime.now(timezone.utc).isoformat(), "membre": member_view(member)}))


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def dispatch(db, now: datetime | None = None, sender=None) -> dict:
    from app.core.config import get_settings
    from app.models.console import ConsoleEvent
    from app.services.naomy_bridge import http_post, sign

    now = now or datetime.now(timezone.utc)
    sender = sender or http_post
    settings = get_settings()
    report = {"sent": 0, "retry": 0, "failed": 0}
    if not settings.naomy_platform_secret:
        return report
    url = f"{settings.naomy_base_url.rstrip('/')}/api/bob/console/membres"
    due = (await db.execute(select(ConsoleEvent).where(
        ConsoleEvent.status == "PENDING", or_(ConsoleEvent.next_attempt_at.is_(None), ConsoleEvent.next_attempt_at <= now))
        .order_by(ConsoleEvent.created_at).limit(100))).scalars().all()
    for evt in due:
        body = json.dumps(evt.data, ensure_ascii=False, separators=(",", ":")).encode()
        stamp = str(int(now.timestamp()))
        headers = {"Content-Type": "application/json", "X-Platform-Timestamp": stamp, "User-Agent": "Bob-Console/1",
                   "X-Platform-Signature": sign(settings.naomy_platform_secret, stamp, body)}
        try:
            status = await sender(url, body, headers)
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
            logger.warning("Changement de membre non transmis à Naomy, abandonné : %s", error)
        else:
            evt.next_attempt_at, evt.last_error = now + timedelta(minutes=RETRY_MINUTES[evt.attempts - 1]), error
            report["retry"] += 1
    await db.commit()
    return report


async def purge(db, now: datetime | None = None) -> dict:
    from app.models.console import ConsoleEvent, ConsoleJump

    now = now or datetime.now(timezone.utc)
    jumps = await db.execute(delete(ConsoleJump).where(ConsoleJump.expires_at < now)
                             .execution_options(synchronize_session=False))
    events = await db.execute(delete(ConsoleEvent).where(ConsoleEvent.status.in_(("SENT", "FAILED")),
                                                         ConsoleEvent.created_at < now - timedelta(days=30))
                              .execution_options(synchronize_session=False))
    await db.commit()
    return {"jumps": jumps.rowcount or 0, "events": events.rowcount or 0}
