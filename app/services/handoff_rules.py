"""
Règles de transmission à un humain (phase 1, lot 13).

Évaluées par le CODE, avant la réponse de Bob, à partir des étiquettes du message (lot 12) et
des réglages du commerce. Fonction pure : mêmes entrées → même décision.

Trois modes :
- TRANSFER_NOW : transfert immédiat, message fixe, SANS appel à l'IA (elle pourrait contredire) ;
- FORBID_TRANSFER : l'outil de transfert est verrouillé pour ce message (garanti par le code) ;
- ALLOW_TRANSFER : Bob juge lui-même (comportement d'avant le lot 13), éventuellement guidé.
"""
from dataclasses import dataclass

from app.models.handoff_settings import (
    COMPLAINT_TRANSFER,
    COMPLAINT_TRY_FIRST,
    DISCOUNT_FIXED_PRICES,
    DISCOUNT_TRANSFER,
    OUTAGE_CALLBACK,
    OUTAGE_RETRY_LATER,
)

TRANSFER_NOW = "TRANSFER_NOW"
FORBID_TRANSFER = "FORBID_TRANSFER"
ALLOW_TRANSFER = "ALLOW_TRANSFER"

TRANSFER_MESSAGE = "Je transmets votre demande à un conseiller, qui vous répondra au plus vite."
MISSING_CONDITIONS_MESSAGE = "Je transmets votre question à la boutique, qui vous répondra au plus vite."
# Lot 38 — versions tutoiement (boutique en ligne qui a choisi « Tu »).
TRANSFER_MESSAGE_TU = "Je transmets ta demande à un conseiller, qui te répondra au plus vite."
MISSING_CONDITIONS_MESSAGE_TU = "Je transmets ta question à la boutique, qui te répondra au plus vite."

# Catégories de la base de connaissances qui répondent à une question sur les conditions de vente.
CONDITIONS_CATEGORIES = frozenset({"RETOUR", "GARANTIE", "CONDITIONS"})

# Panne du service d'IA : messages fixes, jamais une promesse que personne ne tiendra.
OUTAGE_RETRY_LATER_MESSAGE = (
    "Désolé, je rencontre un problème technique momentané. "
    "Pouvez-vous renvoyer votre message dans quelques minutes ?"
)
OUTAGE_CALLBACK_MESSAGE = (
    "Désolé, je rencontre un problème technique momentané. "
    "Un conseiller va vous rappeler au plus vite, au numéro depuis lequel vous nous écrivez."
)
OUTAGE_RETRY_LATER_MESSAGE_TU = (
    "Désolé, je rencontre un problème technique momentané. "
    "Peux-tu renvoyer ton message dans quelques minutes ?"
)
OUTAGE_CALLBACK_MESSAGE_TU = (
    "Désolé, je rencontre un problème technique momentané. "
    "Un conseiller va te rappeler au plus vite, au numéro depuis lequel tu nous écris."
)

# Libellés lisibles par le commerçant (raison du transfert, étiquette dans la conversation).
RULE_LABELS = {
    "HUMAN_REQUEST": "demande d'un humain",
    "REFUND": "demande de remboursement",
    "COMPLAINT_TRANSFER": "réclamation (transfert immédiat)",
    "COMPLAINT_TRY_FIRST": "réclamation (Bob tente d'abord)",
    "DISCOUNT_NEGOTIATE": "remise avec prix proposé : négociation",
    "DISCOUNT_ASK_PRICE": "remise sans prix : Bob demande le prix envisagé",
    "DISCOUNT_FIXED_PRICES": "remise sans négociation : prix fixes",
    "DISCOUNT_TRANSFER": "remise sans négociation : transfert",
    "POLITENESS_ONLY": "politesse seule : pas de transfert",
    "AI_LOOP": "IA bloquée (aucune réponse finale)",
    "AI_OUTAGE_RETRY_LATER": "panne de l'IA : client invité à renvoyer son message",
    "AI_OUTAGE_CALLBACK": "panne de l'IA : client à rappeler",
    "MISSING_CONDITIONS": "conditions de vente non renseignées : question transmise à la boutique",
    "PROMISE_KEPT": "Bob a promis un suivi par un humain : transfert automatique",
    "DEALER_PRICE": "prix ou remise (concession) : à discuter avec un conseiller",
    "DEALER_FINANCING": "paiement ou financement (concession) : présenté par le conseiller lors de la visite",
    "FINANCE_FIGURES": "chiffre de financement ou de reprise retiré de la réponse de Bob",
}

# Message fixe au client selon la règle de transfert immédiat (défaut : TRANSFER_MESSAGE).
TRANSFER_MESSAGES = {"MISSING_CONDITIONS": MISSING_CONDITIONS_MESSAGE}
TRANSFER_MESSAGES_TU = {"MISSING_CONDITIONS": MISSING_CONDITIONS_MESSAGE_TU}


def transfer_message(rule: str | None, tu: bool = False) -> str:
    if tu:
        return TRANSFER_MESSAGES_TU.get(rule, TRANSFER_MESSAGE_TU)
    return TRANSFER_MESSAGES.get(rule, TRANSFER_MESSAGE)


@dataclass
class HandoffSettingsView:
    refund_transfer: bool = True
    complaint_policy: str = COMPLAINT_TRY_FIRST
    discount_policy: str = DISCOUNT_FIXED_PRICES
    ai_outage_policy: str = OUTAGE_RETRY_LATER


@dataclass
class TurnDecision:
    mode: str = ALLOW_TRANSFER
    rule: str | None = None
    instruction: str | None = None
    handoff_blocked: bool = False  # renseigné par l'exécuteur d'outils si Bob tente un transfert interdit

    @property
    def rule_label(self) -> str | None:
        return RULE_LABELS.get(self.rule) if self.rule else None


def _amount(value: float, currency: str) -> str:
    return f"{value:,.0f} {currency}".replace(",", " ")


def evaluate(
    signal: dict | None,
    settings: HandoffSettingsView,
    negotiation_active: bool,
    currency: str = "",
    known_categories: set[str] | None = None,
    dealership: bool = False,
) -> TurnDecision:
    """
    `signal` = {"intents": [...], "objections": [...], "offered_amount": float|None}, ou None.
    `known_categories` = catégories renseignées dans la base de connaissances (None : inconnu,
    la règle des conditions de vente ne s'applique pas).
    """
    if not signal:
        return TurnDecision()  # analyse indisponible : comportement d'avant, inchangé

    intents = set(signal.get("intents") or [])
    objections = set(signal.get("objections") or [])
    offered = signal.get("offered_amount")

    if "DEMANDE_HUMAIN" in intents:
        return TurnDecision(mode=TRANSFER_NOW, rule="HUMAN_REQUEST")

    if "REMBOURSEMENT" in intents and settings.refund_transfer:
        return TurnDecision(mode=TRANSFER_NOW, rule="REFUND")

    # Lot 16 — question sur les retours / échanges / garantie alors que la boutique n'a rien
    # renseigné : Bob ne peut qu'inventer ou promettre de « vérifier ». Le code transmet
    # réellement la question (le commerçant est prévenu), avec un message fixe.
    if "CONDITIONS_VENTE" in intents and known_categories is not None and not (known_categories & CONDITIONS_CATEGORIES):
        return TurnDecision(mode=TRANSFER_NOW, rule="MISSING_CONDITIONS")

    if "RECLAMATION" in intents:
        if settings.complaint_policy == COMPLAINT_TRANSFER:
            return TurnDecision(mode=TRANSFER_NOW, rule="COMPLAINT_TRANSFER")
        return TurnDecision(
            mode=ALLOW_TRANSFER,
            rule="COMPLAINT_TRY_FIRST",
            instruction=(
                "Le client exprime une réclamation : essaie d'abord de comprendre et de résoudre "
                "(vérifie sa commande avec check_order_status si besoin). Ne transfère à un humain "
                "que si tu ne peux vraiment pas l'aider."
            ),
        )

    if dealership and "PAIEMENT" in intents and "DEMANDE_REMISE" not in intents:
        # Lot 24b — incident du 29/09 : « Je peux payer en plusieurs fois ? » → Bob transférait
        # aussitôt. En concession, c'est une étape vers le rendez-vous, pas un motif de transfert.
        return TurnDecision(
            mode=ALLOW_TRANSFER,
            rule="DEALER_FINANCING",
            instruction=(
                "Le client pose une question de paiement ou de financement. Ne donne aucun chiffre et "
                "n'affirme pas quelles solutions existent : réponds que son conseiller pourra lui présenter "
                "les possibilités de financement lors de sa visite, note son intérêt et propose-lui de venir "
                "(get_available_slots pour lui proposer un créneau). Ne transfère pas pour cette question : seulement s'il insiste "
                "pour avoir des chiffres tout de suite."
            ),
        )

    if "DEMANDE_REMISE" in intents:
        if dealership:
            # Lot 24 — en concession, Bob ne négocie pas et n'accorde rien : le prix se discute
            # avec un conseiller. Il oriente vers le rendez-vous (qui transmet de lui-même).
            return TurnDecision(
                mode=ALLOW_TRANSFER,
                rule="DEALER_PRICE",
                instruction=(
                    "Le client parle de remise ou de prix final. N'accorde aucune remise et n'annonce aucun "
                    "prix négocié : explique que le prix se discute avec un conseiller lors de la visite, et "
                    "propose-lui de venir (get_available_slots, puis request_appointment). S'il "
                    "insiste pour avoir une réponse maintenant, utilise handoff_to_human."
                ),
            )
        if negotiation_active and offered:
            return TurnDecision(
                mode=FORBID_TRANSFER,
                rule="DISCOUNT_NEGOTIATE",
                instruction=(
                    f"Le client propose {_amount(offered, currency)}. Utilise l'outil negotiate_price avec ce "
                    "montant pour le produit concerné (demande-lui de quel produit il s'agit si ce n'est pas "
                    "clair). Ne transfère pas à un humain : la négociation le fait elle-même si aucun accord "
                    "n'est possible."
                ),
            )
        if negotiation_active:
            return TurnDecision(
                mode=FORBID_TRANSFER,
                rule="DISCOUNT_ASK_PRICE",
                instruction=(
                    "Le client demande une remise sans proposer de prix. Demande-lui poliment quel prix il "
                    "envisage pour le produit concerné. Ne transfère pas à un humain."
                ),
            )
        if settings.discount_policy == DISCOUNT_TRANSFER:
            return TurnDecision(mode=TRANSFER_NOW, rule="DISCOUNT_TRANSFER")
        return TurnDecision(
            mode=FORBID_TRANSFER,
            rule="DISCOUNT_FIXED_PRICES",
            instruction=(
                "Cette boutique ne négocie pas ses prix. Réponds poliment que les prix sont fixes ; tu peux "
                "proposer un produit moins cher s'il en existe (search_products ou recommend_products). "
                "N'utilise pas negotiate_price et ne transfère pas à un humain."
            ),
        )

    if intents == {"SALUTATION"} and not objections:
        return TurnDecision(
            mode=FORBID_TRANSFER,
            rule="POLITENESS_ONLY",
            instruction="Le client fait simplement preuve de politesse : réponds-lui brièvement. Ne transfère pas à un humain.",
        )

    return TurnDecision()


async def load_handoff_settings(db, tenant_id) -> HandoffSettingsView:
    """Réglages du commerce, ou valeurs par défaut s'il n'a jamais rien enregistré."""
    from sqlalchemy import select

    from app.models.handoff_settings import TenantHandoffSettings

    row = (await db.execute(
        select(TenantHandoffSettings).where(TenantHandoffSettings.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if row is None:
        return HandoffSettingsView()
    return HandoffSettingsView(
        refund_transfer=row.refund_transfer,
        complaint_policy=row.complaint_policy,
        discount_policy=row.discount_policy,
        ai_outage_policy=row.ai_outage_policy or OUTAGE_RETRY_LATER,
    )


async def decide_turn(db, tenant, signal: dict | None) -> TurnDecision:
    """Charge les réglages du commerce (défauts si absents) et évalue les règles."""
    from sqlalchemy import select

    from app.models.negotiation_settings import TenantNegotiationSettings

    view = await load_handoff_settings(db, tenant.id)
    negotiation = (await db.execute(
        select(TenantNegotiationSettings).where(TenantNegotiationSettings.tenant_id == tenant.id)
    )).scalar_one_or_none()
    # Mêmes conditions que l'outil negotiate_price : activée ET plan payant.
    from app.services.business_type import is_dealership

    dealership = is_dealership(tenant)
    # Lot 24 : jamais de négociation par Bob en concession, quel que soit le réglage enregistré.
    negotiation_active = negotiation is not None and negotiation.enabled and tenant.is_paid and not dealership
    from app.services.strategy_service import knowledge_categories

    known = await knowledge_categories(db, tenant.id)
    return evaluate(
        signal, view, negotiation_active, currency=tenant.currency or "", known_categories=known, dealership=dealership,
    )


def outage_message(settings: HandoffSettingsView, tu: bool = False) -> tuple[str, str]:
    """(message au client, règle tracée) selon le choix du commerçant."""
    if settings.ai_outage_policy == OUTAGE_CALLBACK:
        return (OUTAGE_CALLBACK_MESSAGE_TU if tu else OUTAGE_CALLBACK_MESSAGE), "AI_OUTAGE_CALLBACK"
    return (OUTAGE_RETRY_LATER_MESSAGE_TU if tu else OUTAGE_RETRY_LATER_MESSAGE), "AI_OUTAGE_RETRY_LATER"
