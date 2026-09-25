import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.dependency import get_llm_client
from app.agents.llm_client import LLMClient
from app.agents.orchestrator import generate_ai_reply
from app.core.config import get_settings
from app.core.database import get_db
from app.integrations.whatsapp.client import WhatsAppClient, parse_whatsapp_message, verify_whatsapp_signature
from app.models.conversation import ConversationStatus, Message, MessageSender
from app.models.product import Product
from app.models.contact_point import ContactPoint
from app.models.product_qr_code import ProductQrCode
from app.models.tenant import Tenant
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.customer_repository import CustomerRepository
from app.repositories.whatsapp_account_repository import WhatsAppAccountRepository
from app.services.messaging_guard import OutboundDenied, Permission, check_and_log_outbound

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])
settings = get_settings()

HISTORY_LIMIT = 20  # section 13 — mémoire conversationnelle, fenêtre raisonnable
# Référence technique cachée en fin de message : [QR:code] (QR produit) ou [W:code]
# (lien/widget traçable). Toujours retirée ; utilisée pour l'attribution au premier contact.
TRACKING_REFERENCE_PATTERN = re.compile(r"\s*\[(QR|W):([A-Za-z0-9_-]+)\]\s*$")


@router.get("")
async def verify_webhook(request: Request):
    """
    Section 7 — vérification du webhook exigée par Meta lors de sa configuration.
    Un seul verify_token pour toute la plateforme (l'app Meta du Tech Provider, section 59.3).
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_webhook_verify_token:
        return int(challenge) if challenge and challenge.isdigit() else challenge

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Vérification du webhook échouée")


@router.post("", status_code=status.HTTP_200_OK)
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    llm_client: LLMClient | None = Depends(get_llm_client),
) -> dict:
    """
    Section 7 — doit répondre rapidement (< 1s, section 42) pour éviter les timeouts Meta.
    Section 9 — pipeline complet : identification tenant/client, historique, agent IA.
    Section 32 — la signature HMAC de Meta est vérifiée AVANT tout traitement du payload.
    L'envoi RÉEL vers WhatsApp (appel à l'API Meta) reste à câbler — la réponse de Bob
    est ici générée et persistée, prête à être envoyée dès que le compte WhatsApp du
    tenant dispose d'un vrai token (section 59).
    """
    raw_body = await request.body()

    if settings.whatsapp_app_secret:
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_whatsapp_signature(settings.whatsapp_app_secret, raw_body, signature):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Signature webhook invalide")
    # Si WHATSAPP_APP_SECRET n'est pas configuré (dev/démo), la vérification est ignorée :
    # c'est un choix délibéré pour ne pas bloquer le développement local, jamais acceptable
    # en production (section 32) — WHATSAPP_APP_SECRET doit être renseigné avant mise en ligne.

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payload JSON invalide") from None

    parsed = parse_whatsapp_message(payload)
    if parsed is None:
        # Accusé de statut (delivered/read) ou payload non pertinent : on accuse réception sans traiter.
        return {"status": "ignored"}

    # Section 59.5 — routage par phone_number_id, AVANT tout traitement.
    wa_repo = WhatsAppAccountRepository(db)
    account = await wa_repo.find_tenant_by_phone_number_id(parsed["phone_number_id"])
    if account is None:
        # Un numéro inconnu de la plateforme ne doit jamais planter le webhook (Meta réessaierait en boucle).
        return {"status": "unknown_phone_number_id"}

    tenant_id = account.tenant_id

    customer_repo = CustomerRepository(db)
    customer, is_new_customer = await customer_repo.get_or_create_with_created_flag(tenant_id, parsed["from"])

    conversation_repo = ConversationRepository(db)
    conversation = await conversation_repo.get_or_create_active(tenant_id, customer.id)

    # Section 23 — un client qui répond n'est plus « abandonné » : on repart de zéro.
    if conversation.followup_stage != 0:
        conversation.followup_stage = 0
        conversation.last_followup_at = None

    incoming_text = parsed.get("text") or ""

    # Section CRM.7 — attribution : uniquement au tout premier contact, jamais réécrite ensuite.
    # La référence technique est retirée du texte avant tout traitement, pour TOUS les clients
    # (y compris un client existant qui rescanne un QR ou clique un lien) : ni l'historique
    # affiché ni l'IA ne voient jamais cette balise. La recherche est toujours limitée au
    # tenant qui reçoit le message : un code d'un autre commerce n'attribue jamais rien.
    match = TRACKING_REFERENCE_PATTERN.search(incoming_text)
    if match:
        incoming_text = TRACKING_REFERENCE_PATTERN.sub("", incoming_text).strip()
        kind, code = match.group(1), match.group(2)
        if is_new_customer and kind == "QR":
            qr_stmt = select(ProductQrCode).where(ProductQrCode.tenant_id == tenant_id, ProductQrCode.code == code)
            qr = (await db.execute(qr_stmt)).scalar_one_or_none()
            if qr is not None:
                product = await db.get(Product, qr.product_id)
                customer.acquisition_source = "QR"
                customer.acquisition_detail = f"Produit scanné : {product.name}" if product else None
        elif is_new_customer and kind == "W":
            cp_stmt = select(ContactPoint).where(ContactPoint.tenant_id == tenant_id, ContactPoint.code == code)
            contact_point = (await db.execute(cp_stmt)).scalar_one_or_none()
            if contact_point is not None:
                customer.acquisition_source = "LINK"
                customer.acquisition_detail = contact_point.name
                customer.acquisition_contact_point_id = contact_point.id

    incoming_message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        sender=MessageSender.CUSTOMER,
        message_type=parsed["type"],
        content=incoming_text,
        message_metadata={"wa_message_id": parsed["wa_message_id"]},
    )
    db.add(incoming_message)

    # Retrait de consentement (STOP...) — toujours enregistré immédiatement, quel que soit
    # l'état de la conversation ou la configuration du LLM. Jamais laissé à l'appréciation
    # de l'IA : la reconnaissance est déterministe (section consentement).
    from app.services.consent_service import WITHDRAWN_VIA_WHATSAPP_KEYWORD, is_opt_out_message, withdraw_marketing_consent

    is_opt_out = is_opt_out_message(incoming_text)
    if is_opt_out:
        withdraw_marketing_consent(customer, source=WITHDRAWN_VIA_WHATSAPP_KEYWORD)

    await db.commit()

    if llm_client is None or conversation.status != ConversationStatus.ACTIVE:
        # Pas de LLM configuré, ou conversation déjà passée en attente d'un humain (section 19/28) :
        # le message reste en base sans réponse automatique. Le retrait de consentement, lui,
        # est déjà enregistré ci-dessus, indépendamment de cette branche.
        return {"status": "received"}

    try:
        await check_and_log_outbound(db, tenant_id=tenant_id, requested_by="IA", permission=Permission.CAN_REPLY_TO_CUSTOMER)
    except OutboundDenied:
        await db.commit()
        return {"status": "received_no_ai_reply"}

    tenant = await db.get(Tenant, tenant_id)

    from app.services.plan_limits import FREEMIUM_QUOTA_MESSAGE, is_conversation_quota_exceeded

    quota_exceeded = not tenant.is_demo and await is_conversation_quota_exceeded(db, tenant_id, tenant.is_paid)

    if is_opt_out:
        reply_text = (
            "Vous avez été désinscrit(e) de nos communications marketing. "
            "Vous pouvez continuer à nous écrire à tout moment pour toute question."
        )
    elif quota_exceeded:
        reply_text = FREEMIUM_QUOTA_MESSAGE.format(company_name=tenant.name)
    else:
        history_stmt = (
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.id != incoming_message.id)
            .order_by(Message.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
        history = list(reversed((await db.execute(history_stmt)).scalars().all()))

        reply_text = await generate_ai_reply(
            db=db,
            tenant=tenant,
            conversation=conversation,
            history=history,
            incoming_text=incoming_message.content,
            llm_client=llm_client,
        )

        # Freemium (jamais en démo) — mention discrète, levier de bouche-à-oreille (section freemium).
        if not tenant.is_paid:
            reply_text = f"{reply_text}\n\n_Propulsé par Bob 🤖_"

    ai_message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        sender=MessageSender.AI,
        message_type="text",
        content=reply_text,
    )
    db.add(ai_message)
    await db.commit()

    # Envoi réel vers WhatsApp (section 3 : ... -> WhatsApp API -> CLIENT). Ne doit jamais
    # faire planter le webhook si Meta est indisponible ou si le token a expiré : on journalise
    # et on continue, la réponse reste de toute façon consultable dans le dashboard (section 27).
    try:
        wa_client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
        await wa_client.send_text_message(to=customer.whatsapp_number, body=reply_text)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception(
            "Échec de l'envoi WhatsApp réel pour la conversation %s", conversation.id
        )

    return {"status": "received", "ai_reply": reply_text}
