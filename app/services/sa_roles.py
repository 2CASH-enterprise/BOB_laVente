"""
Lot 119 — rôles de l'équipe AgenC'AI dans le Super Admin. Une personne peut cumuler plusieurs rôles : ses droits
s'additionnent. Les droits sont vérifiés par le serveur sur chaque route (security.need) ; l'écran ne fait que cacher
ce qui serait refusé. Mêmes codes de rôles chez Naomy (interrupteur entre les Super Admin, lot suivant).

« Chiffres » d'une boutique = ventes, clients, commissions : seuls Propriétaire et Facturation les voient.
"""
OWNER, SALES, MARKETING, TECH, BILLING, SUPPORT, READONLY = (
    "OWNER", "SALES", "MARKETING", "TECH", "BILLING", "SUPPORT", "READONLY")

ROLES = {
    OWNER: ("Propriétaire", "Tout, y compris l'équipe AgenC'AI."),
    SALES: ("Commercial", "Prospection, relances, pré-inscriptions et ratios. Pas les chiffres des boutiques."),
    MARKETING: ("Marketing", "Campagnes e-mail, liens suivis et ratios de prospection."),
    TECH: ("Technique", "Monitoring, modèles et formulaires WhatsApp, coûts IA. Boutiques sans chiffres."),
    BILLING: ("Facturation", "Formules, échéances, commissions, suspensions, coûts IA."),
    SUPPORT: ("Support", "Fiches des boutiques sans chiffres, suspendre / réactiver, secteur."),
    READONLY: ("Lecture seule", "Voit presque tout, sans rien changer. Pas les chiffres des boutiques."),
}

_ALL = frozenset(ROLES)
PERMS = {
    "overview": _ALL,
    "shops.basic": _ALL - {MARKETING},                       # nom, secteur, pays, statut
    "shops.view": frozenset({OWNER, TECH, BILLING, SUPPORT, READONLY}),  # fiche complète, sans chiffres
    "shops.figures": frozenset({OWNER, BILLING}),
    "shops.billing": frozenset({OWNER, BILLING}),          # formule, payé jusqu'au, commission
    "shops.suspend": frozenset({OWNER, BILLING, SUPPORT}),
    "shops.sector": frozenset({OWNER, SUPPORT}),
    "prospects.view": frozenset({OWNER, SALES, MARKETING, READONLY}),
    "prospects.edit": frozenset({OWNER, SALES}),            # fiches, relances, import, export
    "campaigns.view": frozenset({OWNER, SALES, MARKETING, READONLY}),
    "campaigns.edit": frozenset({OWNER, SALES, MARKETING}),
    "prereg.view": frozenset({OWNER, SALES, SUPPORT, READONLY}),
    "prereg.edit": frozenset({OWNER, SALES}),
    "aicosts.view": frozenset({OWNER, TECH, BILLING, READONLY}),
    "tech.view": frozenset({OWNER, TECH, SUPPORT, READONLY}),
    "tech.edit": frozenset({OWNER, TECH}),
    "plans.view": frozenset({OWNER, BILLING, READONLY}),
    "plans.edit": frozenset({OWNER, BILLING}),
    "team.manage": frozenset({OWNER}),
}


def clean(roles) -> list[str]:
    """Rôles connus, sans doublon, dans l'ordre du tableau."""
    wanted = set(roles or [])
    return [r for r in ROLES if r in wanted]


def perms_for(roles) -> list[str]:
    mine = set(clean(roles))
    return sorted(p for p, allowed in PERMS.items() if mine & allowed)


def can(roles, perm: str) -> bool:
    return bool(set(roles or []) & PERMS[perm])


def catalogue() -> list[dict]:
    return [{"code": code, "label": label, "description": text} for code, (label, text) in ROLES.items()]
