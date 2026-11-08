"""
Lot 114 — « Réserver une table » en formulaire WhatsApp (Flow), sans serveur de formulaire.

Le nombre de personnes est choisi avant (liste habituelle). Bob envoie alors un message « Choisir l'heure » qui ouvre
un formulaire d'un seul écran : le jour (7 prochains jours avec de la place pour ce groupe), l'heure (seulement les heures
libres du jour choisi : un composant Switch montre la liste du jour), une précision facultative, « Réserver ».
Les jours et les heures sont calculés à l'envoi et glissés dans le formulaire (flow_action « navigate ») : aucun
point d'accès chiffré n'est nécessaire. La réponse arrive dans le webhook (nfm_reply) et la réservation passe par
le même outil que les boutons (couverts revérifiés sous verrou).

Un formulaire par compte WhatsApp du restaurant, créé et publié chez Meta au premier besoin (table whatsapp_flows).
Éteint par défaut : interrupteur de la plateforme (Super Admin) et réglage par restaurant (essai / éteint). Si Meta
refuse quoi que ce soit, le parcours à boutons du lot 107 continue : la réservation ne bloque jamais.
"""
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import get_settings
from app.integrations.whatsapp.client import GRAPH_BASE_URL, MetaGraphError, _raise_for_meta

logger = logging.getLogger(__name__)

KEY = "booking"
VERSION = 1  # changer le formulaire = nouvelle version, publiée sous un nouveau nom
NAME = f"bob_reservation_v{VERSION}"
SCREEN = "RESERVER"
# Lot 115 — fin de commande du restaurant (mode, commune, adresse, table, heure, paiement), mêmes réglages.
ORDER_KEY, ORDER_VERSION, ORDER_SCREEN = "order", 1, "COMMANDE"
ORDER_NAME = f"bob_commande_v{ORDER_VERSION}"
ORDER_CTA = "Finaliser"
ORDER_TOKEN_PREFIX = "bobc"
DAYS = 7
MAX_TIMES = 40
RETRY_AFTER = timedelta(hours=1)
CTA = "Choisir l'heure"
TOKEN_PREFIX = "bobt"


def _options_schema(example: list) -> dict:
    return {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}}},
            "__example__": example}


def flow_json() -> dict:
    """Le formulaire (Flow JSON) : un écran, jour → heures du jour (Switch), précision, « Réserver »."""
    day_example = [{"id": "d0", "title": "Aujourd'hui"}]
    time_example = [{"id": "202611131930", "title": "19 h 30"}]
    data = {"intro": {"type": "string", "__example__": "Table pour 4 personnes"},
            "days": _options_schema(day_example)}
    cases = {}
    for i in range(DAYS):
        data[f"t{i}"] = _options_schema(time_example)
        cases[f"d{i}"] = [{"type": "Dropdown", "name": f"time_d{i}", "label": "Heure", "required": True,
                           "data-source": f"${{data.t{i}}}"}]
    payload = {"day": "${form.day}", "note": "${form.note}"}
    payload.update({f"t{i}": f"${{form.time_d{i}}}" for i in range(DAYS)})
    return {
        "version": "7.0",
        "screens": [{
            "id": SCREEN, "title": "Réserver une table", "terminal": True, "data": data,
            "layout": {"type": "SingleColumnLayout", "children": [{
                "type": "Form", "name": "reservation", "children": [
                    {"type": "TextBody", "text": "${data.intro}"},
                    {"type": "Dropdown", "name": "day", "label": "Jour", "required": True, "data-source": "${data.days}"},
                    {"type": "Switch", "value": "${form.day}", "cases": cases},
                    {"type": "TextArea", "name": "note", "label": "Précision (facultatif)", "required": False,
                     "max-length": 200, "helper-text": "Anniversaire, chaise bébé, terrasse…"},
                    {"type": "Footer", "label": "Réserver", "on-click-action": {"name": "complete", "payload": payload}},
                ]}]},
        }],
    }


def _choices(example_id: str, example_title: str) -> dict:
    return _options_schema([{"id": example_id, "title": example_title}])


def order_flow_json() -> dict:
    """Lot 115 — un écran : rappel du panier, mode, puis selon le mode commune + adresse / table ; heure ; paiement."""
    data = {"intro": {"type": "string", "__example__": "Poulet braisé × 2 — 9 000 XOF"},
            "modes": _choices("l", "Livraison"), "areas": _choices("0", "Cocody · 1 000 XOF"),
            "times": _choices("now", "Dès que possible"), "pay_l": _choices("d", "À la livraison"),
            "pay_o": _choices("r", "Au restaurant")}
    delivery = [
        {"type": "Dropdown", "name": "area", "label": "Commune", "required": True, "data-source": "${data.areas}"},
        {"type": "TextArea", "name": "address", "label": "Adresse (quartier, rue, repère)", "required": False, "max-length": 300,
         "helper-text": "Laissez vide pour envoyer votre position ensuite."},
        {"type": "RadioButtonsGroup", "name": "pay_l", "label": "Paiement", "required": True, "data-source": "${data.pay_l}"},
    ]
    other = [
        {"type": "If", "condition": "${form.mode} == 'p'",
         "then": [{"type": "TextInput", "name": "table", "label": "Numéro de table", "required": True, "max-chars": 8}]},
        {"type": "RadioButtonsGroup", "name": "pay_o", "label": "Paiement", "required": True, "data-source": "${data.pay_o}"},
    ]
    payload = {key: f"${{form.{key}}}" for key in ("mode", "area", "address", "table", "time", "pay_l", "pay_o")}
    return {
        "version": "7.0",
        "screens": [{
            "id": ORDER_SCREEN, "title": "Ma commande", "terminal": True, "data": data,
            "layout": {"type": "SingleColumnLayout", "children": [{
                "type": "Form", "name": "commande", "children": [
                    {"type": "TextBody", "text": "${data.intro}"},
                    {"type": "RadioButtonsGroup", "name": "mode", "label": "Comment recevoir la commande ?", "required": True,
                     "data-source": "${data.modes}"},
                    {"type": "If", "condition": "${form.mode} == 'l'", "then": delivery, "else": other},
                    {"type": "Dropdown", "name": "time", "label": "Heure", "required": True, "data-source": "${data.times}"},
                    {"type": "Footer", "label": "Continuer", "on-click-action": {"name": "complete", "payload": payload}},
                ]}]},
        }],
    }


FLOWS = {KEY: (VERSION, NAME, "APPOINTMENT_BOOKING", flow_json), ORDER_KEY: (ORDER_VERSION, ORDER_NAME, "OTHER", order_flow_json)}


# ---------------------------------------------------------------- interrupteurs

async def enabled(db) -> bool:
    from app.models.preregistration import PlatformSettings

    row = await db.get(PlatformSettings, 1)
    return bool(row and getattr(row, "wa_flows_enabled", False))


async def set_enabled(db, on: bool) -> None:
    from app.services.preregistration import settings_row

    row = await settings_row(db)
    row.wa_flows_enabled = bool(on)
    await db.commit()


def tenant_mode(tenant) -> str:
    """AUTO (suit la plateforme), ON (essai sur ce restaurant), OFF (jamais)."""
    mode = (getattr(tenant, "wa_flows", None) or "AUTO").upper()
    return mode if mode in ("AUTO", "ON", "OFF") else "AUTO"


async def active_for(db, tenant) -> bool:
    from app.services.business_type import is_restaurant

    if tenant is None or getattr(tenant, "is_demo", False) or not is_restaurant(tenant):
        return False
    mode = tenant_mode(tenant)
    return mode == "ON" or (mode == "AUTO" and await enabled(db))


# ---------------------------------------------------------------- création chez Meta

class MetaFlowsClient:
    """Formulaires (Flows) du compte WhatsApp Business d'un restaurant : création + publication en un appel."""

    def __init__(self, waba_id: str, token: str, api_version: str | None = None):
        self.token = token
        self.url = f"{GRAPH_BASE_URL}/{api_version or get_settings().whatsapp_graph_api_version}/{waba_id}/flows"

    async def create(self, name: str, flow: dict, category: str = "APPOINTMENT_BOOKING") -> dict:
        body = {"name": name, "categories": [category], "flow_json": json.dumps(flow, ensure_ascii=False),
                "publish": True}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.url, json=body, headers={"Authorization": f"Bearer {self.token}"})
            _raise_for_meta(response)
            return response.json() or {}


def _errors(result: dict) -> str | None:
    errors = result.get("validation_errors") or []
    if not errors:
        return None
    return "; ".join(str(e.get("message") or e.get("error") or e)[:120] for e in errors[:3])[:300]


async def _row(db, tenant_id, key: str = KEY):
    from sqlalchemy import select

    from app.models.whatsapp_flow import WhatsAppFlow

    return (await db.execute(select(WhatsAppFlow).where(WhatsAppFlow.tenant_id == tenant_id, WhatsAppFlow.key == key))
            ).scalar_one_or_none()


async def ensure(db, tenant, client=None, force: bool = False, key: str = KEY):
    """L'identifiant du formulaire publié (réservation ou commande), créé au premier besoin ; None si indisponible.
    Après un refus, nouvel essai au plus une fois par heure (sauf force : bouton du Super Admin)."""
    from sqlalchemy import select

    from app.models.whatsapp_account import WhatsAppAccount
    from app.models.whatsapp_flow import WhatsAppFlow

    version, name, category, build = FLOWS[key]
    row = await _row(db, tenant.id, key)
    if row is not None and row.status == "PUBLISHED" and row.version == version and row.meta_id:
        return row.meta_id
    now = datetime.now(timezone.utc)
    if row is not None and not force and row.version == version and row.updated_at is not None:
        last = row.updated_at if row.updated_at.tzinfo else row.updated_at.replace(tzinfo=timezone.utc)
        if now - last < RETRY_AFTER:
            return None
    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))).scalar_one_or_none()
    if row is None:
        row = WhatsAppFlow(tenant_id=tenant.id, key=key, version=version, status="NOT_CREATED")
        db.add(row)
    row.version, row.name, row.updated_at = version, name, now
    if account is None or not account.waba_id:
        row.status, row.reason = "ERROR", "Aucun numéro WhatsApp connecté"
        await db.flush()
        return None
    client = client or MetaFlowsClient(account.waba_id, account.system_user_token)
    try:
        result = await client.create(name, build(), category)
    except (MetaGraphError, httpx.HTTPError) as exc:
        row.status, row.reason = "ERROR", str(exc)[:300]
        logger.warning("Formulaire WhatsApp %s refusé par Meta (boutique %s) : %s", key, tenant.id, row.reason)
        await db.flush()
        return None
    row.meta_id = str(result.get("id") or "") or None
    problem = _errors(result)
    if problem or not row.meta_id:
        row.status, row.reason = "ERROR", problem or "Meta n'a pas renvoyé d'identifiant"
        logger.warning("Formulaire WhatsApp %s non publié (boutique %s) : %s", key, tenant.id, row.reason)
        await db.flush()
        return None
    row.status, row.reason = "PUBLISHED", None
    await db.flush()
    return row.meta_id


# ---------------------------------------------------------------- envoi et réponse

def _time_title(slot: datetime) -> str:
    from app.services.resto_flow import _hour, _service_name

    return f"{_hour(slot)} ({_service_name(slot.hour * 60 + slot.minute).lower()})"


async def screen_data(db, tenant, party: int) -> dict | None:
    """Jours (7 au plus, avec de la place pour ce groupe) et heures libres de chacun ; None s'il n'y a rien."""
    from app.services import booking
    from app.services.resto_flow import _booking, _day_label, _now, _zone

    settings = await _booking(db, tenant)
    if settings is None:
        return None
    zone, now = _zone(tenant), _now()
    today = now.astimezone(zone).date()
    days, data = [], {}
    for offset in range(booking.HORIZON_DAYS + 1):
        if len(days) == DAYS:
            break
        day = today + timedelta(days=offset)
        free = await booking.free_slots(db, tenant, settings, zone, now, day=day, party=party, limit=100)
        if not free:
            continue
        index = len(days)
        days.append({"id": f"d{index}", "title": _day_label(day, today)[:30]})
        data[f"t{index}"] = [{"id": s.strftime("%Y%m%d%H%M"), "title": _time_title(s)} for s in free[:MAX_TIMES]]
    if not days:
        return None
    for index in range(len(days), DAYS):  # jours non proposés : jamais affichés, mais la liste ne doit pas être vide
        data[f"t{index}"] = [{"id": "none", "title": "—"}]
    people = f"{party} personne{'s' if party > 1 else ''}"
    return {"intro": f"Table pour {people} chez {tenant.name}. Choisissez le jour puis l'heure.", "days": days, **data}


def flow_action(flow_id: str, token: str, body: str, data: dict) -> dict:
    return {"type": "flow", "body": body, "flow_id": flow_id, "flow_token": token, "cta": CTA, "screen": SCREEN, "data": data}


def new_token(party: int) -> str:
    return f"{TOKEN_PREFIX}:{party}:{secrets.token_urlsafe(9)}"


def party_of(token: str | None) -> int | None:
    parts = (token or "").split(":")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1].isdigit():
        return None
    party = int(parts[1])
    return party if 1 <= party <= 50 else None


def chosen_slot(answer: dict) -> str | None:
    """L'heure choisie (« AAAAMMJJHHMM ») dans la réponse du formulaire, ou None."""
    day = str(answer.get("day") or "")
    if not (day.startswith("d") and day[1:].isdigit()):
        return None
    stamp = str(answer.get(f"t{day[1:]}") or "")
    try:
        datetime.strptime(stamp, "%Y%m%d%H%M")
    except ValueError:
        return None
    return stamp


def note_of(answer: dict) -> str | None:
    note = " ".join(str(answer.get("note") or "").split())
    return note[:200] or None


# ---------------------------------------------------------------- lot 115 : fin de commande

def new_order_token() -> str:
    return f"{ORDER_TOKEN_PREFIX}:{secrets.token_urlsafe(9)}"


def is_order_token(token: str | None) -> bool:
    return str(token or "").startswith(f"{ORDER_TOKEN_PREFIX}:")


def order_action(flow_id: str, token: str, body: str, data: dict) -> dict:
    return {"type": "flow", "body": body, "flow_id": flow_id, "flow_token": token, "cta": ORDER_CTA, "screen": ORDER_SCREEN,
            "data": data}


def clean_text(value, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] or None
