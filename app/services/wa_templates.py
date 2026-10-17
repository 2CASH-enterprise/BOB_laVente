"""
Lot 68 — modèles de message WhatsApp, approuvés par Meta, pour prévenir un client HORS de la fenêtre de 20 h.

Règles (choix 1A à 5A du 11/10) :
- Seulement des messages de service, catégorie « Utilitaire » (UTILITY), jamais de promotion ; les offres et
  relances commerciales restent par email. Un modèle reclassé « Marketing » par Meta n'est plus envoyé.
- Textes fixes validés le 11/10 (CATALOG), en « vous », et aussi en « tu » pour le Commerce (seul secteur où
  le tutoiement existe, lot 38). Chaque boutique n'a que les modèles de son secteur.
- Bob crée les modèles sur le compte WhatsApp de la boutique (à la connexion du numéro, ou par le bouton du
  Super Admin) ; le statut chez Meta suit tout seul (webhook + vérification toutes les heures).
- Fenêtre ouverte : texte libre comme avant ; fermée et modèle approuvé : le modèle ; sinon email / tâche.
- Interrupteur de la plateforme ÉTEINT par défaut : rien n'est créé ni envoyé chez Meta tant que le Super Admin
  ne l'allume pas. Une boutique peut aussi être exclue (compte d'examen Meta).
"""
import logging
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import select

from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE, REAL_ESTATE, normalize  # noqa: F401

logger = logging.getLogger(__name__)

LANGUAGE = "fr"
CATEGORY = "UTILITY"
VOUS, TU = "VOUS", "TU"
FALLBACK_NAME = "cher client"
PAY_PATH = "c/"
PAY_BUTTON = "Payer ma commande"
PAY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O ni 1/I : se lit sans erreur
PAY_CODE_LENGTH = 10

CATALOG = {
    "rdv_rappel": {
        "label": "Rappel de rendez-vous la veille",
        "sectors": (CAR_DEALERSHIP, INSURANCE_BROKER, REAL_ESTATE),
        VOUS: "Bonjour {{1}}, petit rappel de votre rendez-vous chez {{2}} demain, {{3}}, à {{4}}. "
              "Pour le déplacer ou l'annuler, répondez simplement à ce message.",
        TU: "Bonjour {{1}}, petit rappel de ton rendez-vous chez {{2}} demain, {{3}}, à {{4}}. "
            "Pour le déplacer ou l'annuler, réponds simplement à ce message.",
        "example": ["Awa", "Auto Prestige", "mardi 14 octobre", "10 h 30"],
    },
    "rdv_absence": {
        "label": "Absence au rendez-vous",
        "sectors": (CAR_DEALERSHIP, REAL_ESTATE),
        VOUS: "Bonjour {{1}}, nous ne vous avons pas vu à votre rendez-vous chez {{2}} le {{3}}. "
              "Souhaitez-vous en fixer un autre ? Répondez simplement à ce message avec le jour qui vous arrange.",
        TU: "Bonjour {{1}}, on ne t'a pas vu à ton rendez-vous chez {{2}} le {{3}}. "
            "Tu veux en fixer un autre ? Réponds simplement à ce message avec le jour qui t'arrange.",
        "example": ["Awa", "Auto Prestige", "mardi 14 octobre"],
    },
    "commande_paiement": {
        "label": "Commande en attente de paiement",
        "sectors": (ONLINE_STORE,),
        VOUS: "Bonjour {{1}}, votre commande {{2}} chez {{3}} ({{4}}) est bien enregistrée et attend votre paiement. "
              "Une question ? Répondez simplement à ce message.",
        TU: "Bonjour {{1}}, ta commande {{2}} chez {{3}} ({{4}}) est bien enregistrée et attend ton paiement. "
            "Une question ? Réponds simplement à ce message.",
        "example": ["Awa", "3F2A1B4C", "Boutique Awa", "45 000 XOF"],
        "button": True,
    },
    "contrat_echeance": {
        "label": "Échéance de contrat",
        "sectors": (INSURANCE_BROKER,),
        VOUS: "Bonjour {{1}}, votre contrat {{2}} souscrit auprès de {{3}} arrive à échéance le {{4}}. "
              "Pour le renouveler ou poser une question, répondez simplement à ce message.",
        TU: "Bonjour {{1}}, ton contrat {{2}} souscrit auprès de {{3}} arrive à échéance le {{4}}. "
            "Pour le renouveler ou poser une question, réponds simplement à ce message.",
        "example": ["Awa", "Auto", "Cabinet Kouassi", "31/10/2026"],
    },
    "livraison_expediee": {
        "label": "Commande expédiée",
        "sectors": (ONLINE_STORE,),
        VOUS: "Bonjour {{1}}, votre commande {{2}} chez {{3}} est en route. Livraison prévue : {{4}}. "
              "Un souci ? Répondez simplement à ce message.",
        TU: "Bonjour {{1}}, ta commande {{2}} chez {{3}} est en route. Livraison prévue : {{4}}. "
            "Un souci ? Réponds simplement à ce message.",
        "example": ["Awa", "3F2A1B4C", "Boutique Awa", "jeudi 16 octobre"],
    },
    "livraison_livree": {
        "label": "Commande livrée",
        "sectors": (ONLINE_STORE,),
        VOUS: "Bonjour {{1}}, votre commande {{2}} chez {{3}} a bien été livrée. Merci pour votre confiance ! "
              "Un souci avec la commande ? Répondez simplement à ce message.",
        TU: "Bonjour {{1}}, ta commande {{2}} chez {{3}} a bien été livrée. Merci pour ta confiance ! "
            "Un souci avec la commande ? Réponds simplement à ce message.",
        "example": ["Awa", "3F2A1B4C", "Boutique Awa"],
    },
    # Lot 80 — alerte « nouveau bien » : modèle MARKETING (payant), réservé au plan Pro ; seulement aux contacts qui
    # ont accepté d'être prévenus. Hors Pro, l'alerte part par email (ou par WhatsApp gratuit dans les 20 h).
    "nouveau_bien": {
        "label": "Nouveau bien pour un acheteur ou un locataire",
        "sectors": (REAL_ESTATE,),
        "category": "MARKETING",
        "plans": ("PRO", "ENTERPRISE"),
        VOUS: "Bonjour {{1}}, un bien de {{2}} correspond à votre recherche : {{3}}. Pour en savoir plus ou le "
              "visiter, répondez simplement à ce message. Pour ne plus recevoir ces alertes, répondez STOP.",
        "example": ["Claire", "Agence du Parc", "T3 lumineux avec balcon (À louer · 3 pièces · 68 m² · Lyon 3e)"],
    },
}

STATUS_LABELS = {
    "NOT_CREATED": "Pas encore créé", "PENDING": "En attente de Meta", "APPROVED": "Approuvé", "REJECTED": "Refusé",
    "PAUSED": "En pause (qualité)", "DISABLED": "Désactivé par Meta", "IN_APPEAL": "En appel", "ERROR": "Erreur à la création",
}


def template_name(key: str, form: str) -> str:
    return f"bob_{key}_{form.lower()}"


def forms_for(tenant) -> tuple[str, ...]:
    """Le tutoiement n'existe que pour le Commerce (lot 38) : les autres secteurs n'ont que la version « vous »."""
    return (VOUS, TU) if normalize(getattr(tenant, "business_type", None)) == ONLINE_STORE else (VOUS,)


def category_of(key: str) -> str:
    """Lot 80 : « UTILITY » sauf mention contraire (l'alerte « nouveau bien » est un modèle MARKETING)."""
    return CATALOG[key].get("category", CATEGORY)


def _plan_ok(tenant, spec: dict) -> bool:
    plan = getattr(getattr(tenant, "plan", None), "value", getattr(tenant, "plan", None))
    return "plans" not in spec or plan in spec["plans"]


def wanted(tenant) -> list[tuple[str, str]]:
    sector = normalize(getattr(tenant, "business_type", None))
    return [(key, form) for key, spec in CATALOG.items() if sector in spec["sectors"] and _plan_ok(tenant, spec)
            for form in forms_for(tenant)]


def form_of(tenant) -> str:
    from app.services.address_form import uses_tu

    return TU if uses_tu(tenant) else VOUS


def pay_url(code: str) -> str:
    from app.core.config import get_settings

    return f"{get_settings().public_base_url.rstrip('/')}/{PAY_PATH}{code}"


def create_payload(key: str, form: str) -> dict:
    spec = CATALOG[key]
    components = [{"type": "BODY", "text": spec[form], "example": {"body_text": [spec["example"]]}}]
    if spec.get("button"):
        components.append({"type": "BUTTONS", "buttons": [{
            "type": "URL", "text": PAY_BUTTON, "url": pay_url("{{1}}"), "example": [pay_url("K7P2QX9M3D")],
        }]})
    return {"name": template_name(key, form), "language": LANGUAGE, "category": category_of(key), "components": components}


def clean_value(value) -> str:
    """Meta refuse une valeur vide, un retour à la ligne, une tabulation ou plus de 4 espaces d'affilée."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return text[:200] or "—"


def render(key: str, form: str, values: list) -> str:
    text = CATALOG[key][form]
    for i, value in enumerate(values, start=1):
        text = text.replace("{{%d}}" % i, clean_value(value))
    return text


def first_name(customer) -> str:
    return (getattr(customer, "first_name", None) or "").strip() or FALLBACK_NAME


# ---------------------------------------------------------------- interrupteurs

async def enabled(db) -> bool:
    from app.models.preregistration import PlatformSettings

    row = await db.get(PlatformSettings, 1)
    return bool(row and row.wa_templates_enabled)


async def set_enabled(db, on: bool) -> None:
    from app.services.preregistration import settings_row

    row = await settings_row(db)
    row.wa_templates_enabled = bool(on)
    await db.commit()


async def active_for(db, tenant) -> bool:
    return tenant is not None and bool(getattr(tenant, "wa_templates_allowed", True)) and await enabled(db)


# ---------------------------------------------------------------- création et statut chez Meta

async def _account(db, tenant_id):
    from app.models.whatsapp_account import WhatsAppAccount

    return (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))).scalar_one_or_none()


async def rows_of(db, tenant_id) -> dict:
    from app.models.whatsapp_template import WhatsAppTemplate

    return {r.name: r for r in (await db.execute(select(WhatsAppTemplate).where(WhatsAppTemplate.tenant_id == tenant_id))).scalars()}


def _apply_remote(row, remote: dict) -> None:
    row.meta_id = str(remote.get("id") or row.meta_id or "")[:64] or None
    row.status = str(remote.get("status") or row.status or "PENDING")[:16]
    default = category_of(row.key) if getattr(row, "key", None) in CATALOG else CATEGORY  # lot 80 : modèle marketing
    row.category = str(remote.get("category") or row.category or default)[:16]
    reason = remote.get("rejected_reason")
    row.reason = None if not reason or reason == "NONE" else str(reason)[:300]


async def sync(db, tenant, create: bool = True, client=None) -> dict:
    """
    Crée chez Meta les modèles manquants de la boutique (si create) et met à jour leur statut.
    Ne fait RIEN si l'interrupteur est éteint ou la boutique exclue. Renvoie un compte rendu.
    """
    from app.integrations.whatsapp.client import MetaGraphError, MetaTemplatesClient
    from app.models.whatsapp_template import WhatsAppTemplate
    from app.services import monitoring

    report = {"ok": False, "created": 0, "updated": 0, "errors": 0, "error": None}
    if not await enabled(db):
        report["error"] = "Les modèles WhatsApp sont éteints pour toute la plateforme."
        return report
    if not getattr(tenant, "wa_templates_allowed", True):
        report["error"] = "Les modèles WhatsApp sont désactivés pour cette boutique."
        return report
    account = await _account(db, tenant.id)
    if account is None:
        report["error"] = "Aucun numéro WhatsApp n'est connecté à cette boutique."
        return report
    client = client or MetaTemplatesClient(account.waba_id, account.system_user_token)
    try:
        remote = {(t.get("name"), t.get("language")): t for t in await client.list_templates()}
    except Exception as exc:  # noqa: BLE001 — jamais le texte brut d'une erreur réseau
        report["error"] = monitoring.mask(str(exc) if isinstance(exc, MetaGraphError) else "Meta est injoignable pour le moment")[:300]
        logger.warning("Modèles WhatsApp : lecture impossible (boutique %s) : %s", tenant.id, report["error"])
        return report
    rows = await rows_of(db, tenant.id)
    for key, form in wanted(tenant):
        name = template_name(key, form)
        found = remote.get((name, LANGUAGE))
        row = rows.get(name)
        if row is None:
            if found is None and not create:
                continue  # simple vérification : rien n'est ajouté pour un modèle jamais créé
            row = WhatsAppTemplate(tenant_id=tenant.id, key=key, form=form, name=name, status="NOT_CREATED")
            db.add(row)
        if found is not None:
            _apply_remote(row, found)
            report["updated"] += 1
        elif create:
            try:
                _apply_remote(row, {**(await client.create_template(create_payload(key, form))), "rejected_reason": None})
                row.status = row.status if row.status != "NOT_CREATED" else "PENDING"
                report["created"] += 1
            except Exception as exc:  # noqa: BLE001
                row.status = "ERROR"
                row.reason = monitoring.mask(str(exc) if isinstance(exc, MetaGraphError) else "Meta est injoignable")[:300]
                report["errors"] += 1
                logger.warning("Modèle WhatsApp %s : création impossible (boutique %s) : %s", name, tenant.id, row.reason)
    await db.commit()
    report["ok"] = report["errors"] == 0
    return report


async def sync_in_background(tenant_id) -> dict:
    """Après la connexion d'un numéro : création des modèles dans une session à part (la requête est déjà finie)."""
    from app.core.database import AsyncSessionLocal
    from app.models.tenant import Tenant

    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tenant_id)
            return await sync(db, tenant) if tenant else {"ok": False}
    except Exception:  # noqa: BLE001
        logger.exception("Modèles WhatsApp : création impossible après la connexion (boutique %s)", tenant_id)
        return {"ok": False}


async def refresh_pending(db, client_factory=None) -> dict:
    """Toutes les heures : statut chez Meta des modèles pas encore définitifs (au cas où un webhook se perd)."""
    from app.models.tenant import Tenant
    from app.models.whatsapp_template import WhatsAppTemplate

    report = {"tenants": 0, "failed": 0}
    if not await enabled(db):
        return report
    tenant_ids = (await db.execute(select(WhatsAppTemplate.tenant_id).where(
        WhatsAppTemplate.status.in_(("PENDING", "IN_APPEAL"))).distinct())).scalars().all()
    for tenant_id in tenant_ids:
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            continue
        client = None
        if client_factory is not None:
            account = await _account(db, tenant_id)
            client = client_factory(account) if account else None
        result = await sync(db, tenant, create=False, client=client)
        report["tenants"] += 1
        report["failed"] += 0 if result["error"] is None else 1
    return report


def parse_status_updates(payload: dict) -> list[dict]:
    """Changements de statut ou de catégorie d'un modèle envoyés par Meta (webhook du compte WhatsApp Business)."""
    updates = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") == "message_template_status_update":
                updates.append({"waba_id": str(entry.get("id") or ""), "name": value.get("message_template_name"),
                                "meta_id": str(value.get("message_template_id") or ""), "status": value.get("event"),
                                "reason": value.get("reason")})
            elif change.get("field") == "template_category_update":
                updates.append({"waba_id": str(entry.get("id") or ""), "name": value.get("message_template_name"),
                                "meta_id": str(value.get("message_template_id") or ""), "category": value.get("new_category")})
    return updates


async def apply_status_updates(db, updates: list[dict]) -> int:
    from app.models.whatsapp_account import WhatsAppAccount
    from app.models.whatsapp_template import WhatsAppTemplate

    done = 0
    for up in updates:
        account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.waba_id == up["waba_id"]))).scalars().first()
        if account is None:
            continue
        query = select(WhatsAppTemplate).where(WhatsAppTemplate.tenant_id == account.tenant_id)
        row = (await db.execute(query.where(WhatsAppTemplate.name == (up.get("name") or "")))).scalar_one_or_none()
        if row is None and up.get("meta_id"):
            row = (await db.execute(query.where(WhatsAppTemplate.meta_id == up["meta_id"]))).scalar_one_or_none()
        if row is None:
            continue
        if up.get("status"):
            row.status = str(up["status"])[:16]
            reason = up.get("reason")
            row.reason = None if not reason or reason == "NONE" else str(reason)[:300]
        if up.get("category"):
            row.category = str(up["category"])[:16]
        done += 1
    await db.commit()
    return done


# ---------------------------------------------------------------- envoi

def new_pay_code() -> str:
    return "".join(secrets.choice(PAY_CODE_ALPHABET) for _ in range(PAY_CODE_LENGTH))


async def ensure_pay_code(db, order) -> str:
    if not order.pay_code:
        order.pay_code = new_pay_code()
        await db.flush()
    return order.pay_code


async def approved(db, tenant, key: str):
    """Le modèle utilisable maintenant pour ce client de la boutique (forme vous / tu de la boutique), ou None."""
    from app.models.whatsapp_template import WhatsAppTemplate

    if not await active_for(db, tenant):
        return None
    row = (await db.execute(select(WhatsAppTemplate).where(
        WhatsAppTemplate.tenant_id == tenant.id, WhatsAppTemplate.name == template_name(key, form_of(tenant))))).scalar_one_or_none()
    if row is None or row.status != "APPROVED" or (row.category or category_of(key)) != category_of(key):
        return None
    if not _plan_ok(tenant, CATALOG[key]):  # lot 80 : modèle payant réservé à certains plans
        return None
    return row


async def send(db, tenant, customer, conversation, key: str, values: list, button: str | None = None,
               message_type: str | None = None) -> bool:
    """
    Envoie le modèle s'il est approuvé. Renvoie True seulement si Meta l'a accepté ; le message est alors
    ajouté à la conversation (texte complet, comme le client le voit). Ne lève jamais d'exception.
    """
    from app.integrations.whatsapp.client import WhatsAppClient
    from app.models.conversation import Message, MessageSender
    from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound

    try:
        if customer is None or not getattr(customer, "whatsapp_number", None) or key not in CATALOG:
            return False
        row = await approved(db, tenant, key)
        if row is None:
            return False
        account = await _account(db, tenant.id)
        if account is None:
            return False
        try:
            await check_and_log_outbound(db, tenant_id=tenant.id, requested_by="BOB",
                                         permission=Permission.CAN_REPLY_TO_CUSTOMER, human_in_control=True)
        except OutboundDenied:
            return False
        values = [clean_value(v) for v in values]
        components = [{"type": "body", "parameters": [{"type": "text", "text": v} for v in values]}]
        if CATALOG[key].get("button") and button:
            components.append({"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": button}]})
        client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
        await client.send_template_message(to=customer.whatsapp_number, template_name=row.name, language=LANGUAGE,
                                           components=components)
    except Exception:  # noqa: BLE001 — refus de Meta (déjà remonté au monitoring), réseau : l'email prend le relais
        return False
    if conversation is not None:
        content = render(key, row.form, values)
        if CATALOG[key].get("button") and button:
            content += f"\n[{PAY_BUTTON}] {pay_url(button)}"
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                       message_type=message_type or f"template_{key}", content=content,
                       message_metadata={"sent_by": "BOB", "template": row.name}))
    return True


# ---------------------------------------------------------------- affichage

async def overview(db, tenant) -> dict:
    """Ce que voient la boutique (Intégrations) et le Super Admin (fiche) : un modèle par ligne, dans la forme utile."""
    rows = await rows_of(db, tenant.id)
    items = []
    for key, form in wanted(tenant):
        row = rows.get(template_name(key, form))
        status = row.status if row else "NOT_CREATED"
        items.append({"key": key, "form": form, "label": CATALOG[key]["label"], "name": template_name(key, form),
                      "status": status, "status_label": STATUS_LABELS.get(status, status),
                      "category": row.category if row else None, "reason": row.reason if row else None,
                      "text": CATALOG[key][form], "preview": render(key, form, CATALOG[key]["example"]),
                      "used": form == form_of(tenant)})
    return {"platform_enabled": await enabled(db), "allowed": bool(getattr(tenant, "wa_templates_allowed", True)),
            "connected": await _account(db, tenant.id) is not None, "form": form_of(tenant), "items": items,
            "approved": sum(1 for i in items if i["status"] == "APPROVED" and i["used"]),
            "checked_at": datetime.now(timezone.utc).isoformat()}
