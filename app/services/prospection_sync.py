"""
Lot 123 — liste d'opposition commune et dédoublonnage des prospects entre Bob et Naomy (contrat 1.6, section
« prospection » ; décision de Thérèse du 10/11). Bob tient la liste, comme pour l'équipe AgenC'AI.

Rien ne passe en clair : seulement des empreintes SHA-256 en hexadécimal minuscule,
- d'un e-mail : en minuscules, sans espaces autour ;
- d'un téléphone : chiffres seuls avec l'indicatif du pays (2250701020304), 8 à 15 chiffres.

- Naomy déclare ses oppositions (désinscrit, STOP, adresse invalide, plainte) : elles valent aussitôt chez Bob
  (import, liste du jour, e-mails), comme celles de Bob.
- Naomy relit les oppositions depuis un curseur, et les vérifie avant chaque envoi.
- Avant une campagne, Naomy envoie les empreintes de sa cible ; Bob dit lesquelles exclure et pourquoi :
  opposition, client de Bob, prospect suivi par Bob (tout prospect « en cours » : prévu, en séquence, à relancer),
  pas intéressé (depuis moins de 180 jours).
"""
import base64
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.services.naomy_bridge import NaomyError

HEX64 = re.compile(r"^[0-9a-f]{64}$")
KINDS = {"email", "telephone"}
REASONS = {"desinscrit": "UNSUBSCRIBED", "stop": "UNSUBSCRIBED", "invalide": "BOUNCED", "plainte": "COMPLAINT"}
REASON_OUT = {"UNSUBSCRIBED": "desinscrit", "BOUNCED": "invalide", "COMPLAINT": "plainte"}
MAX_ITEMS = 1000
NOT_INTERESTED_FOR = timedelta(days=180)


def _items(payload: dict, key: str) -> list[dict]:
    items = payload.get(key)
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS or set(payload) - {key}:
        raise NaomyError(400, "requete_invalide", f"`{key}` : une liste de 1 à {MAX_ITEMS} éléments, rien d'autre.")
    for item in items:
        if not isinstance(item, dict) or not HEX64.match(str(item.get("empreinte", ""))) or item.get("type") not in KINDS:
            raise NaomyError(400, "requete_invalide", "Chaque élément : `empreinte` (SHA-256 en hexadécimal minuscule) et "
                                                     "`type` (email ou telephone).")
    return items


async def add_oppositions(db, payload: dict) -> dict:
    from app.models.prospect import ProspectSuppression

    items = _items(payload, "oppositions")
    for item in items:
        if item.get("raison") not in REASONS:
            raise NaomyError(400, "requete_invalide", f"`raison` : {', '.join(REASONS)}.")
    hashes = {i["empreinte"] for i in items}
    known = set((await db.execute(select(ProspectSuppression.email_hash).where(
        ProspectSuppression.email_hash.in_(hashes)))).scalars().all())
    added = 0
    for item in items:
        if item["empreinte"] in known:
            continue
        db.add(ProspectSuppression(email_hash=item["empreinte"], kind=item["type"], reason=REASONS[item["raison"]],
                                   origin="NAOMY"))
        known.add(item["empreinte"])
        added += 1
    await db.commit()
    return {"ajoutees": added, "deja_connues": len(items) - added}


def _cursor(created_at: datetime, row_id) -> str:
    stamp = (created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)).isoformat()
    return base64.urlsafe_b64encode(f"{stamp}|{row_id}".encode()).decode().rstrip("=")


def _read_cursor(cursor: str | None):
    import uuid

    if not cursor:
        return None
    try:
        stamp, row_id = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().split("|")
        return datetime.fromisoformat(stamp), uuid.UUID(row_id)
    except (ValueError, UnicodeDecodeError):
        raise NaomyError(400, "requete_invalide", "Curseur illisible.") from None


async def list_oppositions(db, cursor: str | None, limit: int = MAX_ITEMS) -> dict:
    from app.models.prospect import ProspectSuppression

    limit = max(1, min(int(limit or MAX_ITEMS), MAX_ITEMS))
    stmt = select(ProspectSuppression).order_by(ProspectSuppression.created_at, ProspectSuppression.id)
    after = _read_cursor(cursor)
    if after is not None:
        stamp, row_id = after
        stmt = stmt.where(or_(ProspectSuppression.created_at > stamp,
                              and_(ProspectSuppression.created_at == stamp, ProspectSuppression.id > row_id)))
    rows = (await db.execute(stmt.limit(limit + 1))).scalars().all()
    page = rows[:limit]
    return {"oppositions": [{"empreinte": r.email_hash, "type": r.kind, "raison": REASON_OUT.get(r.reason, "desinscrit"),
                             "origine": "naomy" if r.origin == "NAOMY" else "bob",
                             "date": (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)).isoformat()}
                            for r in page],
            "curseur_suivant": _cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None}


async def _client_hashes(db) -> set:
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.models.whatsapp_account import WhatsAppAccount
    from app.services.insurance_contracts import normalize_phone
    from app.services.prospection import contact_hash

    out = set()
    tenants = (await db.execute(select(Tenant.id, Tenant.email, Tenant.phone, Tenant.country, Tenant.is_demo))).all()
    real = {t.id for t in tenants if not t.is_demo}
    for t in tenants:
        if t.id not in real:
            continue
        out.add(contact_hash(email=t.email))
        out.add(contact_hash(phone=normalize_phone(t.phone, t.country)))
    for tenant_id, email in (await db.execute(select(User.tenant_id, User.email))).all():
        if tenant_id in real:
            out.add(contact_hash(email=email))
    for tenant_id, number in (await db.execute(select(WhatsAppAccount.tenant_id, WhatsAppAccount.display_phone_number))).all():
        if tenant_id in real:
            out.add(contact_hash(phone=number))
    out.discard(None)
    return out


async def verify(db, payload: dict, now: datetime | None = None) -> dict:
    """Les empreintes de la cible à exclure, avec le motif (le premier qui s'applique, dans cet ordre)."""
    from app.models.prospect import Prospect, ProspectSuppression
    from app.services.prospection import contact_hash

    now = now or datetime.now(timezone.utc)
    items = _items(payload, "empreintes")
    wanted = {i["empreinte"] for i in items}
    reasons: dict[str, str] = {}
    for value in (await db.execute(select(ProspectSuppression.email_hash).where(
            ProspectSuppression.email_hash.in_(wanted)))).scalars().all():
        reasons.setdefault(value, "opposition")
    for value in await _client_hashes(db) & wanted:
        reasons.setdefault(value, "client_bob")
    prospects = (await db.execute(select(Prospect).where(Prospect.tenant_id.is_(None)))).scalars().all()
    for p in prospects:
        motive = None
        changed = p.updated_at if getattr(p, "updated_at", None) is not None else p.created_at
        changed = changed if changed is None or changed.tzinfo else changed.replace(tzinfo=timezone.utc)
        if p.status in ("UNSUBSCRIBED", "INVALID"):
            motive = "opposition"  # aussi ceux d'avant la liste par téléphone (lot 122)
        elif p.status == "ACTIVE":
            motive = "prospect_bob"  # prévu, en cours de séquence, ou contacté par Bob
        elif p.status == "NOT_INTERESTED" and (changed is None or changed >= now - NOT_INTERESTED_FOR):
            motive = "pas_interesse"
        if motive is None:
            continue
        for value in (contact_hash(email=p.email), contact_hash(phone=p.phone)):
            if value in wanted:
                reasons.setdefault(value, motive)
    return {"exclure": [{"empreinte": i["empreinte"], "motif": reasons[i["empreinte"]]}
                        for i in items if i["empreinte"] in reasons]}
