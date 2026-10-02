"""
Lot 50 — coût de l'IA : cache de Mistral, mesure des tokens par boutique, coût estimé.

- Cache : Mistral facture 10 % du prix d'entrée les tokens déjà vus récemment, à condition
  d'envoyer la même `prompt_cache_key` et que le DÉBUT de l'envoi soit identique. Les consignes
  de Bob commencent par les parties fixes de la boutique (identité, règles, base de connaissances) :
  une clé par boutique pour les réponses, une par activité pour l'analyse des messages. La clé est
  une empreinte opaque, jamais le nom de la boutique ni d'un client.
- Mesure : chaque appel enregistre boutique, modèle, tokens envoyés / servis par le cache / reçus.
  Jamais le texte de la conversation.
- Coût : calculé à la lecture avec les prix ci-dessous ($ par million de tokens, 02/10/2026).
"""
import hashlib
from dataclasses import dataclass

CACHED_INPUT_RATIO = 0.10  # Mistral : « Cached prompt tokens are billed at 10% of the standard input token price »


@dataclass(frozen=True)
class Price:
    input: float   # $ / million de tokens envoyés
    output: float  # $ / million de tokens reçus


# Le préfixe le plus long l'emporte (« mistral-small-latest », « mistral-small-2603 »…).
PRICES: dict[str, Price] = {
    "mistral-small": Price(0.15, 0.60),   # Small 4
    "mistral-medium": Price(0.40, 2.00),  # Medium 3
    "mistral-large": Price(0.50, 1.50),   # Large 3
    "ministral-3b": Price(0.10, 0.10),
    "ministral-8b": Price(0.15, 0.15),
    "ministral-14b": Price(0.20, 0.20),
}


def price_for(model: str | None) -> Price | None:
    model = (model or "").lower()
    matches = [prefix for prefix in PRICES if model.startswith(prefix)]
    return PRICES[max(matches, key=len)] if matches else None


def cost_usd(model: str | None, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float | None:
    """Coût estimé d'un appel ; None si le modèle n'a pas de prix connu."""
    price = price_for(model)
    if price is None:
        return None
    cached = max(0, min(cached_tokens or 0, prompt_tokens or 0))
    uncached = max(0, (prompt_tokens or 0) - cached)
    return (uncached * price.input + cached * price.input * CACHED_INPUT_RATIO
            + (completion_tokens or 0) * price.output) / 1_000_000


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def reply_cache_key(tenant_id) -> str:
    """Une clé par boutique : ses consignes et ses outils sont identiques d'un client à l'autre."""
    return f"bob-reply-{_digest('tenant:' + str(tenant_id))}"


def classifier_cache_key(business_type: str | None) -> str:
    """Une clé par activité : les consignes de l'analyse des messages ne dépendent que d'elle."""
    activity = business_type or "ONLINE_STORE"
    return f"bob-classifier-{_digest('activity:' + activity)}"


def usage_from_mistral(usage) -> dict | None:
    """Tokens d'une réponse Mistral (objet du SDK ou dict) ; None si absents."""
    if usage is None:
        return None
    get = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
    prompt = get(usage, "prompt_tokens")
    if prompt is None:
        return None
    details = get(usage, "prompt_tokens_details")
    cached = (get(details, "cached_tokens") if details is not None else None) or 0
    return {"prompt_tokens": int(prompt), "cached_tokens": int(cached),
            "completion_tokens": int(get(usage, "completion_tokens") or 0)}
