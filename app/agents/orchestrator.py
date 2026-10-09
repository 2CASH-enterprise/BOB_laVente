"""
Orchestrateur de l'agent IA (section 13).

Le LLM ne fait que : comprendre l'intention, décider quel outil appeler, générer la
réponse (section 50). Toute donnée factuelle (prix, stock, existence d'un produit)
passe obligatoirement par ToolExecutor — jamais par la mémoire du modèle.
"""
import asyncio
import re
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_client import LLMClient
from app.agents.prompts import build_system_prompt
from app.agents.tool_definitions import TOOL_DEFINITIONS
from app.agents.tools import ToolExecutor, tool_result_to_text
from app.core.config import get_settings
from app.models.conversation import Conversation, Message, MessageSender
from app.models.tenant import Tenant
from app.repositories.knowledge_entry_repository import KnowledgeEntryRepository
from app.services.customer_memory_service import build_customer_memory
from app.services.business_type import tools_for

logger = logging.getLogger(__name__)

# Lot 16 : ne promet plus « un conseiller va prendre le relais » (personne n'était prévenu).
# Le webhook le remplace de toute façon (vrai transfert en cas de boucle, message de panne).
FALLBACK_MESSAGE = (
    "Désolé, je rencontre un problème technique momentané. "
    "Pouvez-vous renvoyer votre message dans quelques minutes ?"
)


# Lot 22 : ajoutée au prompt pour le second essai quand la première réponse était vide.
# Lot 26d — « je n'ai pas de photo », « aucune image disponible », « je ne peux pas vous envoyer de photo ».
NO_PHOTO_CLAIM = re.compile(
    r"(?:pas|aucune?|plus)\s+(?:de\s+|d['’])?(?:photos?|images?|visuels?)"
    r"|(?:ne\s+(?:peux|pourrai|suis\s+pas\s+en\s+mesure)|impossible)[^.!?\n]{0,40}(?:photos?|images?)",
    re.IGNORECASE,
)

EMPTY_REPLY_RETRY_INSTRUCTION = (
    "\n\nCONSIGNE (ta réponse précédente était vide) : réponds maintenant au DERNIER message du client, "
    "même s'il est très court (un chiffre, « oui », « ok ») : relis tes messages précédents pour comprendre "
    "à quoi il répond. Si tu ne comprends pas, demande-lui poliment de préciser. Ne laisse jamais ta réponse vide."
)

# Issue d'un échec : panne du service d'IA (après les nouveaux essais) ou boucle d'outils.
FAILURE_OUTAGE = "OUTAGE"
FAILURE_LOOP = "LOOP"

_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504, 529}


def is_transient_llm_error(exc: Exception) -> bool:
    """
    Erreur passagère (le service peut répondre dans quelques secondes) : surcharge, limite de
    débit, indisponibilité, coupure réseau, délai dépassé. Une erreur définitive (requête
    invalide, clé refusée) n'est jamais réessayée : ça ne ferait que retarder la réponse.
    """
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException, httpx.NetworkError)):
        return True
    if type(exc).__name__ in {"NoResponseError", "APIConnectionError", "APITimeoutError"}:
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        raw = getattr(exc, "raw_response", None)
        status = getattr(raw, "status_code", None)
    return status in _TRANSIENT_STATUS


async def _create_with_retries(llm_client: LLMClient, **kwargs) -> dict:
    """Jusqu'à len(llm_retry_delays) nouveaux essais, uniquement sur erreur passagère."""
    delays = list(get_settings().llm_retry_delays)
    attempt = 0
    while True:
        try:
            return await llm_client.create_message(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if attempt >= len(delays) or not is_transient_llm_error(exc):
                raise
            logger.warning("Erreur passagère du service d'IA (%s), nouvel essai dans %ss", type(exc).__name__, delays[attempt])
            await asyncio.sleep(delays[attempt])
            attempt += 1


def record_usage(db, tenant_id, conversation_id, kind: str, model: str | None, response: dict | None) -> None:
    """Lot 50 — tokens d'un appel (jamais le texte). Sans effet si le fournisseur ne les donne pas."""
    usage = (response or {}).get("usage") if isinstance(response, dict) else None
    if not usage:
        return
    from app.models.llm_usage import LlmUsage

    try:
        db.add(LlmUsage(tenant_id=tenant_id, conversation_id=conversation_id, kind=kind, model=(model or "inconnu")[:64],
                        prompt_tokens=int(usage.get("prompt_tokens") or 0), cached_tokens=int(usage.get("cached_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0)))
    except Exception:  # noqa: BLE001 — la mesure ne doit jamais empêcher Bob de répondre
        logger.warning("Consommation de l'IA non enregistrée (conversation %s)", conversation_id)


def _history_to_anthropic_messages(history: list[Message]) -> list[dict]:
    """
    Mémoire conversationnelle (section 13) : convertit l'historique en base au format
    attendu par l'API Anthropic (rôles user/assistant en alternance).
    """
    messages = []
    for msg in history:
        if msg.sender == MessageSender.CUSTOMER:
            messages.append({"role": "user", "content": msg.content})
        elif msg.sender in (MessageSender.AI, MessageSender.HUMAN):
            messages.append({"role": "assistant", "content": msg.content})
        # SYSTEM : non inclus dans l'historique de dialogue
    return messages


def _local_today(tenant, now=None):
    from datetime import datetime, timezone

    from app.services.local_time import tenant_zone

    return (now or datetime.now(timezone.utc)).astimezone(tenant_zone(tenant)).date()


def _appointment_check(text, tenant, executor, date_retry_done: bool, claim_retry_done: bool):
    """
    Lot 44 — (correction à demander, texte fixe de remplacement). Une seule correction par sujet ;
    si Bob se trompe encore, sa réponse est remplacée par un message fixe (jamais une erreur envoyée).
    """
    from app.services import appointment_guard, calendar_check

    wrong = calendar_check.mismatches(text, _local_today(tenant))
    if wrong:
        return "DATE", (calendar_check.clarification(wrong[0]) if date_retry_done else None)
    if appointment_guard.claims_appointment(text) and not executor.appointment_checked:
        return "CLAIM", (appointment_guard.FALLBACK if claim_retry_done else None)
    return None, None


def _date_correction(text, tenant) -> str:
    from app.services import calendar_check

    wrong = calendar_check.mismatches(text, _local_today(tenant))[0]
    return ("\n\nCONSIGNE (correction) : ta réponse contient un jour qui ne correspond pas à la date "
            f"(le {wrong['date'].day} {calendar_check.MONTHS[wrong['date'].month - 1]} est un {wrong['real_day']}). "
            "Ne répète jamais une date incohérente et n'enregistre rien : demande au client de préciser, avec "
            f"exactement cette question : « {calendar_check.clarification(wrong)} »")


def _claim_correction(text, tenant) -> str:
    return ("\n\nCONSIGNE (correction) : tu as annoncé un rendez-vous (noté, enregistré, confirmé…) sans l'avoir "
            "enregistré avec un outil dans ce message. S'il a choisi un créneau proposé par get_available_slots, "
            "appelle request_appointment avec ce slot ; sinon propose-lui des créneaux (get_available_slots) ou "
            "demande-lui quand il peut venir. Ne dis JAMAIS qu'un rendez-vous est noté ou confirmé sans l'outil.")


_CORRECTIONS = {"DATE": _date_correction, "CLAIM": _claim_correction}


async def generate_ai_reply(*args, **kwargs) -> str:
    """Compatibilité : seulement le texte (voir generate_ai_reply_detailed pour l'issue)."""
    text, _ = await generate_ai_reply_detailed(*args, **kwargs)
    return text


async def generate_ai_reply_detailed(
    db: AsyncSession,
    tenant: Tenant,
    conversation: Conversation,
    history: list[Message],
    incoming_text: str,
    llm_client: LLMClient,
    turn=None,
    image_outbox: list | None = None,
    booking_outbox: list | None = None,
    message_outbox: list | None = None,
    change_outbox: list | None = None,
) -> tuple[str, str | None]:
    """
    Retourne le texte de la réponse de Bob. Ne lève jamais d'exception vers l'appelant :
    en cas d'échec (LLM indisponible, boucle d'outils trop longue), retourne un message
    de repli et laisse la conversation en l'état pour reprise par un humain (section 34).
    """
    settings = get_settings()
    knowledge_repo = KnowledgeEntryRepository(db)
    knowledge_entries = await knowledge_repo.list_active(tenant.id)
    customer_memory = await build_customer_memory(db, tenant.id, conversation.customer_id)
    system_prompt = build_system_prompt(tenant, knowledge_entries, customer_memory)
    business_type = getattr(tenant, "business_type", None)
    executor = ToolExecutor(db, tenant.id, conversation, business_type=business_type)
    if booking_outbox is not None:
        # Lot 29 : un rendez-vous réservé est confirmé au client même si la réponse de Bob échoue ensuite.
        executor.booking_outbox = booking_outbox
    if message_outbox is not None:
        executor.message_outbox = message_outbox  # lot 34b : messages fixes après la réponse
    # Lot 35c : le dernier message du client, pour les verrous qui en dépendent (annulation explicite).
    executor.incoming_text = incoming_text
    if change_outbox is not None:
        executor.change_outbox = change_outbox  # lot 35 : rendez-vous déplacés ou annulés par le client
    tools = tools_for(business_type, TOOL_DEFINITIONS)
    # Lot 13 : consigne des règles de transmission pour CE message, et verrou du transfert.
    if turn is not None:
        executor.turn = turn
        if turn.instruction:
            system_prompt += f"\n\nCONSIGNE POUR CE MESSAGE (prioritaire) :\n{turn.instruction}"

    messages = _history_to_anthropic_messages(history)
    messages.append({"role": "user", "content": incoming_text})

    # Lot 50 — cache de Mistral : une clé par boutique (consignes et outils identiques d'un client à
    # l'autre). Les fakes de test et Anthropic n'en reçoivent pas.
    from app.services.llm_costs import reply_cache_key

    cache_key = reply_cache_key(tenant.id) if getattr(llm_client, "supports_cache_key", False) else None

    def call_kwargs(system: str) -> dict:
        kwargs = {"system": system, "messages": messages, "tools": tools}
        if cache_key:
            kwargs["cache_key"] = cache_key
        return kwargs

    empty_replies = 0
    photo_retry_done = False
    lookup_retry_done = False
    # Lot 44 — concession : dates cohérentes et rendez-vous annoncé seulement s'il existe.
    from app.services.business_type import is_appointment_sector, is_insurance

    dealership = is_appointment_sector(tenant)  # lot 53 : concession et courtier
    insurance = is_insurance(tenant)
    date_retry_done = False
    claim_retry_done = False
    amount_retry_done = False
    try:
        for _ in range(settings.max_tool_iterations):
            response = await _create_with_retries(llm_client, **call_kwargs(system_prompt))
            record_usage(db, tenant.id, conversation.id, "REPLY", getattr(llm_client, "model", None), response)
            content_blocks = response.get("content", [])
            stop_reason = response.get("stop_reason")

            if stop_reason != "tool_use":
                text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
                text = "\n".join(text_parts).strip()
                if text and not lookup_retry_done and not executor.pending_images and not executor.looked_up_products \
                        and NO_PHOTO_CLAIM.search(text):
                    # Lot 26e — incident réel : Bob recopie « pas de photo » de ses réponses précédentes,
                    # sans rien vérifier. Un seul nouvel essai : consulter le catalogue d'abord.
                    lookup_retry_done = True
                    system_prompt += (
                        "\n\nCONSIGNE (correction) : tu as dit qu'il n'y avait pas de photo sans consulter le "
                        "catalogue. Ne te fie pas à tes réponses précédentes : cherche le produit avec "
                        "search_products, puis, s'il a une photo, envoie-la avec send_product_images."
                    )
                    logger.warning("Bob a nié une photo sans vérifier (conversation %s) : nouvel essai", conversation.id)
                    continue
                if text and not photo_retry_done and not executor.pending_images and executor.products_with_photo \
                        and NO_PHOTO_CLAIM.search(text):
                    # Lot 26d — Bob affirme qu'il n'y a pas de photo alors qu'un produit vu dans ce
                    # message en a une : un seul nouvel essai, avec les produits nommés.
                    photo_retry_done = True
                    names = ", ".join(f"{name} (product_id {pid})" for pid, name in list(executor.products_with_photo.items())[:3])
                    system_prompt += (
                        "\n\nCONSIGNE (correction) : tu as dit qu'il n'y avait pas de photo, mais ces produits en "
                        f"ont une : {names}. Si le client veut voir l'un d'eux, appelle send_product_images, puis "
                        "réponds-lui sans dire qu'il n'y a pas de photo."
                    )
                    logger.warning("Bob a nié une photo existante (conversation %s) : nouvel essai", conversation.id)
                    continue
                if text and insurance:
                    # Lot 53 — courtier : jamais de montant. Une correction, puis un message fixe.
                    from app.services import insurance as insurance_service

                    if insurance_service.contains_amount(text):
                        if amount_retry_done:
                            logger.warning("Réponse de Bob remplacée (AMOUNT, conversation %s)", conversation.id)
                            text = insurance_service.AMOUNT_FALLBACK
                        else:
                            amount_retry_done = True
                            system_prompt += insurance_service.amount_correction()
                            logger.warning("Réponse de Bob à corriger (AMOUNT, conversation %s) : nouvel essai", conversation.id)
                            continue
                if text and dealership:
                    correction, fixed = _appointment_check(text, tenant, executor, date_retry_done, claim_retry_done)
                    if correction == "DATE":
                        date_retry_done = True
                    elif correction == "CLAIM":
                        claim_retry_done = True
                    if fixed is not None:
                        logger.warning("Réponse de Bob remplacée (%s, conversation %s)", correction, conversation.id)
                        text = fixed
                    elif correction is not None:
                        system_prompt += _CORRECTIONS[correction](text, tenant)
                        logger.warning("Réponse de Bob à corriger (%s, conversation %s) : nouvel essai", correction, conversation.id)
                        continue
                if text:
                    if image_outbox is not None:
                        image_outbox.extend(executor.pending_images)  # lot 26c : seulement si Bob a répondu
                    return text, None
                # Lot 22 — réponse vide (cas réel du 27/09 : un client répond « 1 » à un choix,
                # Mistral ne renvoie rien, le client était transféré). Un seul nouvel essai, avec
                # une consigne explicite ; transfert seulement si le second est vide aussi.
                logger.warning(
                    "Réponse vide du service d'IA (conversation %s, essai %s) : %s",
                    conversation.id, empty_replies + 1, response.get("diagnostic") or {"stop_reason": stop_reason},
                )
                if empty_replies >= 1:
                    return FALLBACK_MESSAGE, FAILURE_LOOP
                empty_replies += 1
                system_prompt += EMPTY_REPLY_RETRY_INSTRUCTION
                continue

            # Le modèle veut utiliser un ou plusieurs outils : on les exécute réellement (section 33)
            messages.append({"role": "assistant", "content": content_blocks})
            tool_results = []
            for block in content_blocks:
                if block.get("type") != "tool_use":
                    continue
                result = await executor.execute(block["name"], block.get("input", {}))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": tool_result_to_text(result),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        # Trop d'itérations d'outils sans réponse finale : on ne laisse jamais tourner indéfiniment.
        logger.warning(
            "Boucle d'outils IA interrompue (max_tool_iterations atteint) — conversation %s", conversation.id
        )
        return FALLBACK_MESSAGE, FAILURE_LOOP

    except Exception:  # noqa: BLE001 — on protège systématiquement l'appelant (section 34)
        logger.exception("Échec de l'appel LLM pour la conversation %s", conversation.id)
        return FALLBACK_MESSAGE, FAILURE_OUTAGE
