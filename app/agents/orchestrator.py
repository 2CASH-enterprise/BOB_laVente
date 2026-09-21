"""
Orchestrateur de l'agent IA (section 13).

Le LLM ne fait que : comprendre l'intention, décider quel outil appeler, générer la
réponse (section 50). Toute donnée factuelle (prix, stock, existence d'un produit)
passe obligatoirement par ToolExecutor — jamais par la mémoire du modèle.
"""
import logging

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

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = (
    "Je rencontre une difficulté à traiter votre demande pour le moment. "
    "Un conseiller va prendre le relais dans les plus brefs délais."
)


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


async def generate_ai_reply(
    db: AsyncSession,
    tenant: Tenant,
    conversation: Conversation,
    history: list[Message],
    incoming_text: str,
    llm_client: LLMClient,
) -> str:
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
    executor = ToolExecutor(db, tenant.id, conversation)

    messages = _history_to_anthropic_messages(history)
    messages.append({"role": "user", "content": incoming_text})

    try:
        for _ in range(settings.max_tool_iterations):
            response = await llm_client.create_message(
                system=system_prompt, messages=messages, tools=TOOL_DEFINITIONS
            )
            content_blocks = response.get("content", [])
            stop_reason = response.get("stop_reason")

            if stop_reason != "tool_use":
                text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
                return "\n".join(text_parts).strip() or FALLBACK_MESSAGE

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
        return FALLBACK_MESSAGE

    except Exception:  # noqa: BLE001 — on protège systématiquement l'appelant (section 34)
        logger.exception("Échec de l'appel LLM pour la conversation %s", conversation.id)
        return FALLBACK_MESSAGE
