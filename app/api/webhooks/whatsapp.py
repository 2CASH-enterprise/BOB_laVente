import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.classifier import MessageClassifier, get_message_classifier
from app.services import notifications  # lot 37b
from app.services.prospect import build_fiche, hot_alert_emails  # lot 43
from app.agents.dependency import get_llm_client
from app.agents.llm_client import LLMClient
from app.agents.orchestrator import FAILURE_LOOP, FAILURE_OUTAGE, generate_ai_reply_detailed
from app.core.config import get_settings
from app.core.database import get_db
from app.integrations.whatsapp.client import WhatsAppClient, parse_whatsapp_message, verify_whatsapp_signature
from app.models.conversation import ConversationStatus, Message, MessageSender
from app.models.product import Product
from app.models.contact_point import ContactPoint
from app.integrations.whatsapp.formatting import to_whatsapp
from app.services.acquisition import ad_context_for_ai, attribution_from_referral, parse_referral
from app.services.email_service import send_email
from app.core.rate_limit import RateLimiter
from app.core.rate_limit_dependency import get_rate_limiter
from app.services.handoff_rules import (
    FORBID_TRANSFER,
    RULE_LABELS,
    TRANSFER_NOW,
    decide_turn,
    load_handoff_settings,
    outage_message,
    transfer_message,
)
from app.services.business_type import is_dealership
from app.services.finance_guard import FINANCE_MESSAGE, FINANCE_MESSAGE_NO_TRANSFER, contains_financing_figure
from app.services.promise_guard import contains_human_promise, remove_human_promises
from app.services.signal_service import classify_and_store
from app.services.strategy_service import apply_strategy
from app.services.handoff_service import (
    TRANSFER_MESSAGE_TYPES,
    alert_emails,
    build_callback_alert,
    build_handoff_alert,
    build_outage_alert,
    build_reminder_alert,
    commercial_for_customer,
    history_since_last_transfer,
    reminder_due,
)
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



def with_ad_context(text: str, referral: dict | None) -> str:
    """Texte donné à Bob : le message du client, précédé du contexte de l'annonce s'il y en a une."""
    context = ad_context_for_ai(referral)
    return f"{context}\n\n{text}" if context else text

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


def _signal_payload(signal) -> dict | None:
    if signal is None:
        return None
    return {
        "intents": list(signal.intents or []),
        "objections": list(signal.objections or []),
        "offered_amount": float(signal.offered_amount) if signal.offered_amount is not None else None,
    }


@router.post("", status_code=status.HTTP_200_OK)
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    llm_client: LLMClient | None = Depends(get_llm_client),
    classifier: MessageClassifier | None = Depends(get_message_classifier),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
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
    # Lot 37b — une fois la réponse envoyée à Meta : nouvelle tâche ? pastille + notification.
    background_tasks.add_task(notifications.queue_check, tenant_id)

    customer_repo = CustomerRepository(db)
    customer, is_new_customer = await customer_repo.get_or_create_with_created_flag(tenant_id, parsed["from"])

    conversation_repo = ConversationRepository(db)
    conversation = await conversation_repo.get_or_create_active(tenant_id, customer.id)

    # Section 23 — un client qui répond n'est plus « abandonné » : on repart de zéro.
    if conversation.followup_stage != 0:
        conversation.followup_stage = 0
        conversation.last_followup_at = None

    incoming_text = parsed.get("text") or ""

    # Lot 28 — le client arrive d'une publicité (ou publication) Facebook / Instagram : Meta joint
    # une référence au message. Source notée au tout premier contact seulement ; le contexte de
    # l'annonce est donné à Bob pour ce message, qu'il soit nouveau client ou non.
    referral = parse_referral(parsed.get("referral"))
    if referral is not None and is_new_customer:
        customer.acquisition_source, customer.acquisition_detail = attribution_from_referral(referral)
        customer.acquisition_ad_id = referral["ad_id"]

    # Section CRM.7 — attribution : uniquement au tout premier contact, jamais réécrite ensuite.
    # La référence technique est retirée du texte avant tout traitement, pour TOUS les clients
    # (y compris un client existant qui rescanne un QR ou clique un lien) : ni l'historique
    # affiché ni l'IA ne voient jamais cette balise. La recherche est toujours limitée au
    # tenant qui reçoit le message : un code d'un autre commerce n'attribue jamais rien.
    match = TRACKING_REFERENCE_PATTERN.search(incoming_text)
    if match:
        incoming_text = TRACKING_REFERENCE_PATTERN.sub("", incoming_text).strip()
        kind, code = match.group(1), match.group(2)
        if is_new_customer and referral is None and kind == "QR":
            qr_stmt = select(ProductQrCode).where(ProductQrCode.tenant_id == tenant_id, ProductQrCode.code == code)
            qr = (await db.execute(qr_stmt)).scalar_one_or_none()
            if qr is not None:
                product = await db.get(Product, qr.product_id)
                customer.acquisition_source = "QR"
                customer.acquisition_detail = f"Produit scanné : {product.name}" if product else None
        elif kind == "W":
            cp_stmt = select(ContactPoint).where(ContactPoint.tenant_id == tenant_id, ContactPoint.code == code)
            contact_point = (await db.execute(cp_stmt)).scalar_one_or_none()
            if contact_point is not None and is_new_customer and referral is None:  # la pub Meta prime
                customer.acquisition_source = "LINK"
                customer.acquisition_detail = contact_point.name
                customer.acquisition_contact_point_id = contact_point.id
            # Lot 27 — lien d'un commercial : le client lui est rattaché, même s'il était déjà connu
            # (dernier commercial qui l'a amené). La source d'acquisition, elle, ne change jamais.
            if contact_point is not None and contact_point.owner_email and contact_point.archived_at is None:
                customer.referred_contact_point_id = contact_point.id

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

    # Lot 34 — le client écrit lui-même son email alors que sa fiche n'en a pas : enregistré par le
    # code, sans dépendre de l'IA. Jamais d'écrasement d'un email déjà connu.
    if not customer.email:
        from app.services.contact_capture import SOURCE_MESSAGE, extract_email, record_email

        written = extract_email(incoming_text)
        if written:
            record_email(customer, written, SOURCE_MESSAGE)

    # Lot 34b — accord explicite pour les offres : le client répond « OFFRES » (proposé dans la
    # demande d'email). Reconnaissance déterministe, comme STOP ; jamais laissée à l'IA.
    from app.services.contact_capture import CONSENT_SOURCE_KEYWORD, is_offers_opt_in

    is_opt_in = not is_opt_out and is_offers_opt_in(incoming_text)
    if is_opt_in:
        from app.services.consent_service import grant_marketing_consent

        grant_marketing_consent(customer, source=CONSENT_SOURCE_KEYWORD)

    # Le client relance pendant qu'il attend un humain (transfert fait par Bob) : rappel au
    # commerçant, au plus une fois par heure — sinon le client reste sans réponse sans que
    # personne ne le sache.
    reminder_emails: list[dict] = []
    if not is_opt_out and reminder_due(conversation):
        tenant_for_alert = await db.get(Tenant, tenant_id)
        subject, body = build_reminder_alert(customer, conversation, incoming_text)
        commercial = await commercial_for_customer(db, customer)
        reminder_emails = alert_emails(tenant_for_alert.email, commercial, subject, body)
        conversation.human_alert_sent_at = datetime.now(timezone.utc)

    # Lot 40 — le client vient de donner son email : récapitulatif de sa commande en cours (promis par Bob).
    from app.services.order_emails import recap_to_send

    recap = await recap_to_send(db, await db.get(Tenant, tenant_id), customer)
    if recap is not None:
        reminder_emails.append(recap)

    await db.commit()
    for email in reminder_emails:
        background_tasks.add_task(send_email, **email)

    # Phase 1 (lot 12) — étiquettes du message (intentions, objections, prix proposé), pour
    # TOUS les messages texte du client, même en attente d'un humain. Jamais bloquant, et
    # sans effet sur la réponse de Bob dans ce lot.
    # Lot 45 : la concession a ses propres objections (financement, reprise, papiers, état du véhicule).
    signal = await classify_and_store(db, classifier, incoming_message,
                                      business_type=getattr(await db.get(Tenant, tenant_id), "business_type", None))

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

    from app.services.address_form import uses_tu
    from app.services.plan_limits import FREEMIUM_QUOTA_MESSAGE, FREEMIUM_QUOTA_MESSAGE_TU, is_conversation_quota_exceeded

    tu = uses_tu(tenant)  # lot 38 : messages fixes au tutoiement si la boutique l'a choisi

    quota_exceeded = not tenant.is_demo and await is_conversation_quota_exceeded(db, tenant_id, tenant.is_paid)

    outage_emails: list[dict] = []
    change_outbox: list = []  # lot 35 : rendez-vous déplacés ou annulés par le client
    message_outbox: list[str] = []  # lot 34b : messages fixes après la réponse (demande d'email)
    booking_outbox: list = []  # lot 29 : rendez-vous réservés par Bob, confirmés au client après sa réponse
    image_outbox: list[dict] = []  # lot 26c : photos de produits demandées par Bob, envoyées après sa réponse
    if is_opt_out:
        from app.services.contact_capture import OPT_OUT_REPLY, OPT_OUT_REPLY_TU

        reply_text = OPT_OUT_REPLY_TU if tu else OPT_OUT_REPLY
    elif is_opt_in:
        from app.services.contact_capture import opt_in_reply

        reply_text = opt_in_reply(bool(customer.email), tu=tu)
    elif quota_exceeded:
        reply_text = (FREEMIUM_QUOTA_MESSAGE_TU if tu else FREEMIUM_QUOTA_MESSAGE).format(company_name=tenant.name)
    else:
        history_stmt = (
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.id != incoming_message.id)
            .order_by(Message.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
        history = list(reversed((await db.execute(history_stmt)).scalars().all()))
        # Mémoire de l'IA : uniquement ce qui suit le dernier transfert vers un humain (une
        # demande déjà transmise ne doit jamais être retransmise après la reprise par Bob).
        history = history_since_last_transfer(history)

        # Lot 13 — règles de transmission, évaluées par le code AVANT la réponse de Bob.
        turn = await decide_turn(db, tenant, _signal_payload(signal))
        # Lot 15 — stratégie de réponse à l'objection (après les règles, jamais contre elles).
        strategy = await apply_strategy(db, tenant_id, turn, _signal_payload(signal), business_type=tenant.business_type)
        if turn.mode == TRANSFER_NOW:
            # Transfert immédiat, message fixe, sans appel à l'IA (elle pourrait le contredire).
            conversation.status = ConversationStatus.WAITING_HUMAN
            db.add(Message(
                tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                message_type="handoff", content=f"Transfert vers un humain : Règle — {turn.rule_label}",
            ))
            reply_text = transfer_message(turn.rule, tu=tu)
        else:
            reply_text, failure = await generate_ai_reply_detailed(
                db=db,
                tenant=tenant,
                conversation=conversation,
                history=history,
                incoming_text=with_ad_context(incoming_message.content, referral),
                llm_client=llm_client,
                turn=turn,
                image_outbox=image_outbox,
                booking_outbox=booking_outbox,
                message_outbox=message_outbox,
                change_outbox=change_outbox,
            )
            if failure == FAILURE_LOOP:
                # Anomalie (Bob tourne en rond) : un humain doit regarder → vrai transfert + alerte.
                conversation.status = ConversationStatus.WAITING_HUMAN
                db.add(Message(
                    tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                    message_type="handoff", content=f"Transfert vers un humain : Règle — {RULE_LABELS['AI_LOOP']}",
                ))
                reply_text = transfer_message(None, tu=tu)
                turn.rule = "AI_LOOP"
            elif failure == FAILURE_OUTAGE:
                # Panne du service d'IA (après les nouveaux essais) : jamais de promesse que personne
                # ne tiendra, et pas de transfert (tous les clients seraient bloqués après la panne).
                handoff_view = await load_handoff_settings(db, tenant_id)
                reply_text, turn.rule = outage_message(handoff_view, tu=tu)
                db.add(Message(
                    tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                    message_type="ai_outage", content=f"Panne du service d'IA — {RULE_LABELS[turn.rule]}",
                ))
                if turn.rule == "AI_OUTAGE_CALLBACK":
                    if await rate_limiter.is_allowed(f"ai-outage-callback:{tenant_id}:{customer.id}", limit=1, window_seconds=3600):
                        subject, body = build_callback_alert(customer, conversation, incoming_text)
                        outage_emails = alert_emails(tenant.email, await commercial_for_customer(db, customer), subject, body)
                elif await rate_limiter.is_allowed(f"ai-outage:{tenant_id}", limit=1, window_seconds=3600):
                    subject, body = build_outage_alert(tenant.name)
                    outage_emails = [{"to": tenant.email, "subject": subject, "body": body}]
            elif is_dealership(tenant) and contains_financing_figure(reply_text):
                # Lot 24 — concession : jamais de mensualité, de taux, d'apport ni de valeur de reprise
                # annoncés par Bob. La réponse entière est remplacée ; le client est transmis à un
                # conseiller, sauf si une règle interdit le transfert pour ce message.
                db.add(Message(
                    tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                    message_type="finance_removed",
                    content="Réponse de Bob remplacée : elle contenait un chiffre de financement ou de reprise.",
                ))
                if conversation.status == ConversationStatus.WAITING_HUMAN:
                    reply_text = FINANCE_MESSAGE_NO_TRANSFER  # déjà transmis (rendez-vous, transfert)
                elif turn.mode == FORBID_TRANSFER:
                    reply_text = FINANCE_MESSAGE_NO_TRANSFER
                else:
                    reply_text = FINANCE_MESSAGE
                    conversation.status = ConversationStatus.WAITING_HUMAN
                    db.add(Message(
                        tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                        message_type="handoff", content=f"Transfert vers un humain : Règle — {RULE_LABELS['FINANCE_FIGURES']}",
                    ))
                    turn.rule = "FINANCE_FIGURES"
            elif conversation.status == ConversationStatus.ACTIVE and contains_human_promise(reply_text):
                # Lot 16 — Bob promet un suivi par un humain SANS avoir transféré : le code tient
                # la promesse (vrai transfert, commerçant prévenu), ou la retire si une règle
                # interdit le transfert pour ce message.
                if turn.mode == FORBID_TRANSFER:
                    reply_text = remove_human_promises(reply_text, tu=tu)
                    db.add(Message(
                        tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                        message_type="promise_removed",
                        content=(
                            "Promesse d'un suivi par un humain retirée de la réponse de Bob "
                            f"(transfert non autorisé — Règle : {turn.rule_label})"
                        ),
                    ))
                else:
                    conversation.status = ConversationStatus.WAITING_HUMAN
                    db.add(Message(
                        tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                        message_type="handoff", content=f"Transfert vers un humain : Règle — {RULE_LABELS['PROMISE_KEPT']}",
                    ))
                    turn.rule = "PROMISE_KEPT"
        if signal is not None:
            signal.applied_rule = turn.rule
            signal.handoff_blocked = turn.handoff_blocked if turn.mode == FORBID_TRANSFER else None
            # En cas de panne ou de boucle, la stratégie n'a pas réellement été appliquée.
            signal.strategy = strategy.code if strategy is not None and turn.rule is None else None

        # Freemium (jamais en démo) — mention discrète, levier de bouche-à-oreille (section freemium).
        if not tenant.is_paid:
            reply_text = f"{reply_text}\n\n_Propulsé par Bob 🤖_"

    # Lot 31 — texte enregistré tel qu'il part sur WhatsApp (gras *…*, puces •), pour que le tableau
    # de bord montre exactement ce que le client a reçu.
    reply_text = to_whatsapp(reply_text)
    ai_message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        sender=MessageSender.AI,
        message_type="text",
        content=reply_text,
    )
    db.add(ai_message)

    # Bob vient de transmettre la conversation (outil de transfert ou négociation sans accord) :
    # le commerçant est prévenu immédiatement par email.
    handoff_emails: list[dict] = []
    if conversation.status == ConversationStatus.WAITING_HUMAN:
        reason_stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.sender == MessageSender.SYSTEM,
                Message.message_type.in_(TRANSFER_MESSAGE_TYPES),
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        transfer = (await db.execute(reason_stmt)).scalar_one_or_none()
        if transfer is None:
            reason = None
        elif transfer.message_type == "handoff":
            reason = transfer.content.split(" : ", 1)[-1]  # « Transfert vers un humain : <motif> »
        else:
            reason = transfer.content  # négociation : le texte complet (offre, plancher) est utile

        subject, body = build_handoff_alert(customer, conversation, reason, await build_fiche(db, tenant, customer))
        handoff_emails = alert_emails(tenant.email, await commercial_for_customer(db, customer), subject, body)
        conversation.human_alert_sent_at = datetime.now(timezone.utc)

    # Lot 29 — Bob a réservé un créneau : la boutique et le commercial du client sont prévenus.
    booking_emails: list[dict] = []
    booking_messages: list[str] = []
    if booking_outbox:
        from app.services import appointment_service
        from app.services.handoff_service import conversation_link, customer_display_name
        from app.services.local_time import tenant_zone

        zone = tenant_zone(tenant)
        commercial = await commercial_for_customer(db, customer)
        for appointment in booking_outbox:
            subject, body = appointment_service.booking_alert_email(
                appointment, customer_display_name(customer), zone, conversation_link(conversation),
                await build_fiche(db, tenant, customer),
            )
            booking_emails += alert_emails(tenant.email, commercial, subject, body)
            booking_messages.append(appointment_service.confirmation_message(appointment, tenant.name, zone))
    if change_outbox:
        # Lot 35 — le prospect a déplacé ou annulé son rendez-vous : boutique et commercial prévenus.
        from app.services import appointment_service
        from app.services.handoff_service import conversation_link, customer_display_name
        from app.services.local_time import tenant_zone

        commercial = await commercial_for_customer(db, customer)
        for appointment, previous in change_outbox:
            subject, body = appointment_service.staff_change_email(
                appointment, customer_display_name(customer), tenant_zone(tenant), conversation_link(conversation), previous,
            )
            booking_emails += alert_emails(tenant.email, commercial, subject, body)

    # Lot 40 — email donné pendant la réponse de Bob (outil record_customer_email) : même récapitulatif.
    recap_emails = [r for r in [await recap_to_send(db, tenant, customer)] if r is not None]
    # Lot 43 — concession : prospect devenu chaud sans rendez-vous → fiche au commercial (une fois).
    recap_emails += await hot_alert_emails(db, tenant, customer, conversation)
    await db.commit()
    for email in handoff_emails + outage_emails + booking_emails + recap_emails:
        background_tasks.add_task(send_email, **email)

    # Envoi réel vers WhatsApp (section 3 : ... -> WhatsApp API -> CLIENT). Ne doit jamais
    # faire planter le webhook si Meta est indisponible ou si le token a expiré : on journalise
    # et on continue, la réponse reste de toute façon consultable dans le dashboard (section 27).
    wa_client = WhatsAppClient(phone_number_id=account.phone_number_id, system_user_token=account.system_user_token)
    try:
        await wa_client.send_text_message(to=customer.whatsapp_number, body=reply_text)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception(
            "Échec de l'envoi WhatsApp réel pour la conversation %s", conversation.id
        )

    # Lot 29 — confirmation FIXE (jamais rédigée par l'IA) du rendez-vous réservé, après la réponse ;
    # tracée seulement si Meta l'accepte.
    for text in booking_messages:
        try:
            await wa_client.send_text_message(to=customer.whatsapp_number, body=text)
        except Exception:  # noqa: BLE001 — le rendez-vous reste confirmé et visible dans la page Rendez-vous
            logging.getLogger(__name__).warning("Confirmation de rendez-vous non envoyée (conversation %s)", conversation.id)
            continue
        db.add(Message(tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                       message_type="appointment_confirmed", content=text, message_metadata={"sent_by": "BOB"}))
    if booking_messages:
        await db.commit()

    # Lot 26c — photos des produits, après le texte ; chacune n'est tracée que si Meta l'accepte.
    images_sent = 0
    for image in image_outbox:
        try:
            await wa_client.send_image_message(to=customer.whatsapp_number, link=image["link"], caption=image["caption"])
        except Exception:  # noqa: BLE001 — une photo refusée ne bloque jamais la conversation
            logging.getLogger(__name__).warning(
                "Photo de produit non envoyée (conversation %s, produit %s)", conversation.id, image["product_id"]
            )
            continue
        db.add(Message(
            tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.AI, message_type="image",
            content=f"📷 Photo envoyée : {image['caption']}",
            message_metadata={"image_url": image["link"], "product_id": image["product_id"]},
        ))
        images_sent += 1
    if images_sent:
        await db.commit()

    # Lot 34b/35 — messages fixes (demande d'email, confirmation de report ou d'annulation), en dernier.
    for text in message_outbox:
        try:
            await wa_client.send_text_message(to=customer.whatsapp_number, body=text)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("Demande d'email non envoyée (conversation %s)", conversation.id)
            continue
        db.add(Message(tenant_id=tenant_id, conversation_id=conversation.id, sender=MessageSender.AI,
                       message_type="auto_message", content=text))
    if message_outbox:
        await db.commit()

    return {"status": "received", "ai_reply": reply_text, "images_sent": images_sent}
