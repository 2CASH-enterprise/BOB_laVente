"""
Lot 73 i — « nouvelle visite » : un client qui revient après 6 heures ou plus n'est pas dans la même discussion.

Une seule conversation par client (jusqu'à sa fermeture par la boutique) ; Bob en relit les derniers messages sans leur
date. Retour réel du 20/10 : revenu le lendemain, le client voyait Bob reprendre l'ancien sujet et ne recevait plus la
photo déjà montrée la veille. Le code repère donc le début de la visite en cours :
- retour après VISIT_GAP_HOURS ou plus → nouvelle visite (Bob accueille, demande ce que le client cherche aujourd'hui) ;
- la règle « une carte déjà montrée ne repart pas » ne vaut que dans la visite en cours.
"""
from datetime import datetime, timedelta, timezone

VISIT_GAP_HOURS = 6
MAX_VISIT = timedelta(hours=24)  # au-delà, une carte peut de toute façon repartir


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def visit_info(history, now: datetime | None = None) -> tuple[bool, timedelta | None, datetime]:
    """(nouvelle visite ?, temps écoulé depuis le dernier message, début de la visite en cours)."""
    now = now or datetime.now(timezone.utc)
    gap_limit = timedelta(hours=VISIT_GAP_HOURS)
    # Les échanges repris du téléphone (lot 52) n'ont pas de date d'enregistrement : ils ne comptent pas.
    times = sorted(t for t in (_utc(getattr(m, "created_at", None)) for m in history) if t is not None)
    if not times:
        return False, None, now - MAX_VISIT
    since_last = now - times[-1]
    if since_last >= gap_limit:
        return True, since_last, now
    for previous, current in zip(reversed(times[:-1]), reversed(times[1:])):
        if current - previous >= gap_limit:
            return False, since_last, max(current, now - MAX_VISIT)
    return False, since_last, now - MAX_VISIT


def elapsed_text(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 24:
        return f"{hours} heures"
    days = round(hours / 24)
    if days < 2:
        return "un jour"
    return f"{days} jours" if days < 60 else "plusieurs mois"


def new_visit_instruction(delta: timedelta) -> str:
    return (
        f"\n\nNOUVELLE VISITE : ce client revient après {elapsed_text(delta)} sans écrire. Les messages précédents datent "
        "de sa visite précédente. Accueille-le simplement (« bon retour ») et demande ce que tu peux faire pour lui "
        "aujourd'hui, ou réponds directement à sa demande s'il en a une. Ne reprends pas l'ancien sujet (proposition, "
        "question restée sans réponse) sauf s'il y fait référence ; s'il parle d'un produit sans le nommer (« elle est "
        "toujours dispo ? »), sers-toi de l'historique. Un rendez-vous ou une commande en cours peut être rappelé en une "
        "phrase. Les photos peuvent être montrées de nouveau."
    )
