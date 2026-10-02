"""
Lot 38 — tutoiement ou vouvoiement des clients (boutique en ligne).

- Réglé par le commerçant dans « Réglages de Bob » ; VOUS par défaut (les messages d'avant ce lot
  ne changent pas).
- Concession : toujours VOUS (règle du lot 24), le réglage n'existe pas pour elle et le serveur
  le refuse.
- S'applique à l'IA (consigne claire) ET à chaque message fixe écrit par le code (récapitulatif
  de commande, transfert, panne, demande d'email…) : chacun existe dans les deux versions.
- Les emails suivront ce réglage au lot 40 (emails redessinés).
"""
from app.services.business_type import CAR_DEALERSHIP

VOUS = "VOUS"
TU = "TU"
ADDRESS_FORMS = {VOUS: "Vouvoiement", TU: "Tutoiement"}


def uses_tu(tenant) -> bool:
    return tenant is not None and tenant.business_type != CAR_DEALERSHIP and getattr(tenant, "address_form", VOUS) == TU


def ai_rule(tenant) -> str:
    """Consigne donnée à Bob (boutique en ligne)."""
    if uses_tu(tenant):
        return ("Tutoie TOUJOURS le client, de façon chaleureuse et respectueuse, même s'il te vouvoie "
                "(« tu », « ton », « ta », « tes » ; jamais « vous » pour t'adresser à lui).")
    return "Vouvoie TOUJOURS le client, même s'il te tutoie."


def preview(form: str) -> list[str]:
    """Exemples affichés sous le réglage, avant d'enregistrer."""
    if form == TU:
        return ["Bonjour ! Je suis Bob 😊 Tu cherches quelque chose en particulier ?",
                "Je transmets ta demande à un conseiller, qui te répondra au plus vite."]
    return ["Bonjour ! Je suis Bob 😊 Vous cherchez quelque chose en particulier ?",
            "Je transmets votre demande à un conseiller, qui vous répondra au plus vite."]
