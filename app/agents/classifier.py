"""
Classificateur de messages (phase 1, lot 12) : un appel SÉPARÉ, avec un petit modèle rapide,
qui étiquette chaque message client selon la taxonomie fixe — avant la réponse de Bob.

Garanties :
- jamais bloquant : délai maximal, et toute erreur donne « pas de classification » ; Bob
  répond alors exactement comme sans classificateur ;
- réponse du modèle VALIDÉE : seules les étiquettes de la taxonomie sont conservées, un
  montant n'est gardé que s'il est un nombre positif ;
- seules les étiquettes sont stockées, jamais une copie du message.
"""
import asyncio
import json
import logging
from abc import ABC, abstractmethod

from app.agents.taxonomy import INTENTS, OBJECTIONS, objections_for

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1500


def build_classifier_prompt(business_type: str | None = None) -> str:
    if business_type == "CAR_DEALERSHIP":
        return _dealership_prompt()
    intents = "\n".join(f"- {code} : {definition}" for code, (_, definition) in INTENTS.items())
    objections = "\n".join(f"- {code} : {definition}" for code, (_, definition) in OBJECTIONS.items())
    return (
        "Tu analyses UN message qu'un client a envoyé sur WhatsApp à une boutique en ligne.\n"
        "Tu ne réponds jamais au client : tu classes seulement son message.\n\n"
        "Réponds UNIQUEMENT par un objet JSON de la forme :\n"
        '{"intents": ["CODE", ...], "objections": ["CODE", ...], "offered_amount": nombre ou null}\n\n'
        f"Intentions possibles (une ou plusieurs) :\n{intents}\n\n"
        f"Objections possibles (zéro, une ou plusieurs) :\n{objections}\n\n"
        "Règles :\n"
        "- n'utilise que les codes ci-dessus, en majuscules ;\n"
        "- une objection = tout ce qui freine ou retarde l'achat, y compris une hésitation sans "
        "raison donnée ; s'il n'y a aucun frein, liste vide ;\n"
        "- offered_amount = le montant que le client PROPOSE de payer ou annonce comme budget "
        "(ex. « je vous le prends à 250 000 » → 250000) ; null s'il n'en donne aucun ;\n"
        "- une question sur la politique de retour, d'échange ou de garantie, sans problème réel avec "
        "une commande, est CONDITIONS_VENTE, jamais REMBOURSEMENT ni RECLAMATION ;\n"
        "- le message précédent de la boutique, s'il est fourni, sert seulement à comprendre "
        "une réponse courte (« oui », « combien ? ») ; ne classe que le message du client.\n\n"
        f"Exemples :\n{_examples()}"
    )


# Exemples : la façon la plus efficace de guider un petit modèle. Le premier cas réel mal classé
# (« Je vais réfléchir » → AUTRE sans objection, 26/09/2026) a motivé cette liste.
CLASSIFIER_EXAMPLES: list[tuple[str, dict]] = [
    ("Je vais réfléchir", {"intents": ["AUTRE"], "objections": ["HESITATION"], "offered_amount": None}),
    ("Je dois demander à mon mari d'abord", {"intents": ["AUTRE"], "objections": ["HESITATION"], "offered_amount": None}),
    ("Ok je reviens plus tard", {"intents": ["SALUTATION"], "objections": ["HESITATION"], "offered_amount": None}),
    ("C'est trop cher pour moi", {"intents": ["AUTRE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": None}),
    ("Vous pouvez me le faire à 250000 ?", {"intents": ["DEMANDE_REMISE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": 250000}),
    ("Vous faites un prix ?", {"intents": ["DEMANDE_REMISE"], "objections": [], "offered_amount": None}),
    ("C'est pas une arnaque ? Je paie à la livraison ?", {"intents": ["PAIEMENT"], "objections": ["CONFIANCE"], "offered_amount": None}),
    ("La livraison à 5000 c'est trop", {"intents": ["LIVRAISON"], "objections": ["FRAIS_LIVRAISON"], "offered_amount": None}),
    ("D'accord merci", {"intents": ["SALUTATION"], "objections": [], "offered_amount": None}),
    ("Je prends le noir en taille 42", {"intents": ["INTENTION_ACHAT"], "objections": [], "offered_amount": None}),
    ("Je veux parler au responsable", {"intents": ["DEMANDE_HUMAIN"], "objections": [], "offered_amount": None}),
    # Lot 16 (26/09) : « Vous acceptez les retours ? » était pris pour un remboursement.
    ("Vous acceptez les retours ?", {"intents": ["CONDITIONS_VENTE"], "objections": [], "offered_amount": None}),
    ("Si la taille ne me va pas je peux l'échanger ?", {"intents": ["CONDITIONS_VENTE"], "objections": [], "offered_amount": None}),
    ("Il y a une garantie sur ce téléphone ?", {"intents": ["CONDITIONS_VENTE"], "objections": [], "offered_amount": None}),
    ("La robe est arrivée déchirée, je veux être remboursée", {"intents": ["RECLAMATION", "REMBOURSEMENT"], "objections": [], "offered_amount": None}),
]


def _examples(examples=None) -> str:
    return "\n".join(
        f"« {text} » → {json.dumps(expected, ensure_ascii=False)}" for text, expected in (examples or CLASSIFIER_EXAMPLES)
    )


# Lot 45 — concession automobile : mêmes intentions, objections propres (taxonomie v1.3auto).
DEALERSHIP_CLASSIFIER_EXAMPLES: list[tuple[str, dict]] = [
    ("Je vais réfléchir", {"intents": ["AUTRE"], "objections": ["HESITATION"], "offered_amount": None}),
    ("C'est trop cher pour moi", {"intents": ["AUTRE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": None}),
    ("Vous pouvez me la faire à 12 millions ?", {"intents": ["DEMANDE_REMISE"], "objections": ["PRIX_TROP_ELEVE"], "offered_amount": 12000000}),
    ("Vous faites le crédit ? Je n'ai pas tout en cash", {"intents": ["PAIEMENT"], "objections": ["FINANCEMENT"], "offered_amount": None}),
    ("Je peux payer en plusieurs fois ?", {"intents": ["PAIEMENT"], "objections": ["FINANCEMENT"], "offered_amount": None}),
    ("Vous reprenez ma Corolla de 2012 ?", {"intents": ["AUTRE"], "objections": ["REPRISE"], "offered_amount": None}),
    ("La voiture est dédouanée ? Les papiers sont en règle ?", {"intents": ["AUTRE"], "objections": ["PAPIERS"], "offered_amount": None}),
    ("Le kilométrage est réel ? Elle a déjà eu un accident ?", {"intents": ["AUTRE"], "objections": ["ETAT_VEHICULE"], "offered_amount": None}),
    ("Comment je sais que la voiture existe vraiment ? Je ne paie rien avant de la voir", {"intents": ["AUTRE"], "objections": ["CONFIANCE"], "offered_amount": None}),
    ("Elle est déjà vendue ?", {"intents": ["DISPONIBILITE"], "objections": [], "offered_amount": None}),
    ("Je cherche un SUV diesel", {"intents": ["RECHERCHE_PRODUIT"], "objections": [], "offered_amount": None}),
    ("Je veux parler au responsable", {"intents": ["DEMANDE_HUMAIN"], "objections": [], "offered_amount": None}),
    ("D'accord merci", {"intents": ["SALUTATION"], "objections": [], "offered_amount": None}),
]


def _dealership_prompt() -> str:
    intents = "\n".join(f"- {code} : {definition}" for code, (_, definition) in INTENTS.items())
    objections = "\n".join(f"- {code} : {definition}" for code, (_, definition) in objections_for("CAR_DEALERSHIP").items())
    return (
        "Tu analyses UN message qu'un client a envoyé sur WhatsApp à une concession automobile.\n"
        "Tu ne réponds jamais au client : tu classes seulement son message.\n\n"
        "Réponds UNIQUEMENT par un objet JSON de la forme :\n"
        '{"intents": ["CODE", ...], "objections": ["CODE", ...], "offered_amount": nombre ou null}\n\n'
        "Intentions possibles (une ou plusieurs ; « produit » = véhicule) :\n"
        f"{intents}\n\n"
        f"Objections possibles (zéro, une ou plusieurs) :\n{objections}\n\n"
        "Règles :\n"
        "- n'utilise que les codes ci-dessus, en majuscules ;\n"
        "- une objection = tout ce qui freine ou retarde l'achat, y compris une hésitation sans "
        "raison donnée ; une simple question sans doute exprimé (« elle est encore disponible ? ») "
        "n'est pas une objection ; s'il n'y a aucun frein, liste vide ;\n"
        "- offered_amount = le montant que le client PROPOSE de payer ou annonce comme budget "
        "(ex. « je vous la prends à 12 millions » → 12000000) ; null s'il n'en donne aucun ;\n"
        "- le message précédent de la concession, s'il est fourni, sert seulement à comprendre "
        "une réponse courte (« oui », « combien ? ») ; ne classe que le message du client.\n\n"
        f"Exemples :\n{_examples(DEALERSHIP_CLASSIFIER_EXAMPLES)}"
    )


def normalize_classification(raw, business_type: str | None = None) -> dict | None:
    """Ne garde que ce qui respecte la taxonomie de l'activité. None si la réponse est inexploitable."""
    if not isinstance(raw, dict):
        return None
    allowed = objections_for(business_type)
    intents = [c for c in dict.fromkeys(raw.get("intents") or []) if isinstance(c, str) and c in INTENTS]
    objections = [c for c in dict.fromkeys(raw.get("objections") or []) if isinstance(c, str) and c in allowed]
    amount = raw.get("offered_amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        amount = None
    if not intents:
        intents = ["AUTRE"]
    return {"intents": intents, "objections": objections, "offered_amount": amount}


class MessageClassifier(ABC):
    model_name: str = "inconnu"

    @abstractmethod
    async def _call(self, text: str, previous_shop_message: str | None, system_prompt: str | None = None) -> dict:
        """Renvoie la réponse JSON brute du modèle (consignes : celles de la boutique en ligne par défaut)."""

    async def classify(self, text: str, previous_shop_message: str | None = None, timeout: float = 5.0,
                       business_type: str | None = None) -> dict | None:
        text = (text or "").strip()
        if not text:
            return None
        try:
            if business_type == "CAR_DEALERSHIP":  # lot 45 : consignes et objections de la concession
                call = self._call(text[:MAX_MESSAGE_CHARS], previous_shop_message,
                                  system_prompt=build_classifier_prompt(business_type))
            else:
                call = self._call(text[:MAX_MESSAGE_CHARS], previous_shop_message)
            raw = await asyncio.wait_for(call, timeout=timeout)
        except Exception:  # noqa: BLE001 — jamais bloquant : pas de classification, Bob répond normalement
            logger.warning("Classification du message impossible (délai ou erreur du modèle)", exc_info=True)
            return None
        return normalize_classification(raw, business_type)


class MistralMessageClassifier(MessageClassifier):
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model_name = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            from mistralai.client.sdk import Mistral

            self._client = Mistral(api_key=self.api_key)
        return self._client

    async def _call(self, text: str, previous_shop_message: str | None, system_prompt: str | None = None) -> dict:
        user = f"Message du client : {text}"
        if previous_shop_message:
            user = f"Message précédent de la boutique : {previous_shop_message[:500]}\n\n{user}"
        response = await self._get_client().chat.complete_async(
            model=self.model_name,
            messages=[{"role": "system", "content": system_prompt or build_classifier_prompt()}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        content = response.choices[0].message.content
        if isinstance(content, list):
            content = "".join(getattr(chunk, "text", "") for chunk in content)
        return json.loads(content or "{}")


def get_message_classifier() -> MessageClassifier | None:
    """None si Mistral n'est pas configuré : pas de classification, rien d'autre ne change."""
    from app.core.config import get_settings

    settings = get_settings()
    if settings.llm_provider != "mistral" or not settings.mistral_api_key:
        return None
    return MistralMessageClassifier(api_key=settings.mistral_api_key, model=settings.mistral_classifier_model)
