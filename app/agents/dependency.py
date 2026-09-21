from app.agents.llm_client import AnthropicLLMClient, LLMClient, MistralLLMClient
from app.core.config import get_settings


def get_llm_client() -> LLMClient | None:
    """
    Retourne None tant qu'aucune clé n'est configurée pour le fournisseur choisi : l'IA
    ne répond simplement pas encore, plutôt que de planter le webhook. Surchargeable en
    test via app.dependency_overrides.
    """
    settings = get_settings()

    if settings.llm_provider == "mistral":
        if not settings.mistral_api_key:
            return None
        return MistralLLMClient(api_key=settings.mistral_api_key, model=settings.mistral_model)

    if not settings.anthropic_api_key:
        return None
    return AnthropicLLMClient(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
