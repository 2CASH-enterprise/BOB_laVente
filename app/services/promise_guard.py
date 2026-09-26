"""
Filet de sécurité « promesses tenues » (lot 16).

Constat du 26/09/2026 : Bob a écrit « Je transmets ta préoccupation à un conseiller, qui te
contactera rapidement » ou « Je vais vérifier auprès de la boutique » SANS appeler l'outil de
transfert. Le client attendait une réponse que personne ne savait devoir donner.

Une consigne ne suffit pas : c'est le CODE qui vérifie la réponse de Bob avant l'envoi.
- promesse d'un humain sans transfert réel → le code fait le vrai transfert (commerçant prévenu) ;
- promesse alors qu'une règle interdit le transfert → les phrases de promesse sont retirées.

Détection volontairement étroite : une personne (conseiller, boutique, équipe…) ET une action
de suivi (contacter, répondre, reprendre…) dans la même phrase, ou une transmission explicite.
« Je vous transmets le lien de paiement » ou « notre conseiller en image vous le recommande »
ne sont PAS des promesses.
"""
import re

_HUMAN = (
    r"(?:(?:un|une|le|la|les|nos|notre|mon|ma|mes|votre|ton|ta)\s+(?:\w+\s+)?conseill[eè]re?s?"
    r"|la boutique|le magasin|notre [ée]quipe|l'[ée]quipe|mon [ée]quipe|nos [ée]quipes"
    r"|un membre de (?:l'|notre |mon )?[ée]quipe|quelqu'un|un (?:vendeur|responsable|coll[eè]gue|humain|agent)"
    r"|une (?:personne|vendeuse|responsable|coll[eè]gue)|le (?:responsable|g[ée]rant|vendeur)|la (?:responsable|g[ée]rante|vendeuse))"
)
_FOLLOW_UP = (
    r"(?:va|vont|pourra|pourront|devrait|devraient)\s+(?:(?:te|vous)\s+)?(?:re)?(?:contacter|r[ée]pondre|rappeler|[ée]crire|revenir)"
    r"|(?:te|vous)\s+(?:re)?contactera|(?:te|vous)\s+(?:re)?contacteront|(?:te|vous)\s+r[ée]pondra|(?:te|vous)\s+r[ée]pondront"
    r"|(?:te|vous)\s+rappellera|(?:te|vous)\s+rappelleront|(?:te|vous)\s+[ée]crira|(?:te|vous)\s+reviendra"
    r"|reviendra vers|reviendront vers|(?:va|vont)\s+revenir vers"
    r"|(?:va|vont)\s+reprendre|reprendra|reprendront|reprend(?:re)? contact|prendra le relais|prendront le relais"
    r"|(?:va|vont)\s+prendre le relais|(?:va|vont)\s+s'occuper|s'occupera|s'occuperont|(?:va|vont)\s+(?:te|vous)\s+recontacter"
)
_TRANSFER_VERB = r"(?:transm(?:ets|et|ettre|ettrai|ettons|is|ise)|transf[eè]re|transf[ée]rer|transf[ée]rerai|passe le relais)"
_PATTERNS = [
    # « Un conseiller va te contacter », « La boutique vous répondra dans la journée »
    re.compile(_HUMAN + r"[^.!?\n]{0,60}?\b(?:" + _FOLLOW_UP + r")"),
    # « Je transmets ta demande à un conseiller », « je transfère à la boutique »
    # (la cible doit suivre « à » : « je vous transmets le lien de paiement de la boutique » n'en est pas une)
    re.compile(
        _TRANSFER_VERB + r"[^.!?\n]{0,60}?\b(?:à|au|aux)\s+(?:[\w']+\s+){0,2}?(?:l')?"
        r"(?:conseill|boutique|magasin|[ée]quipe|humain|responsable|vendeu|coll[eè]gue|g[ée]rant|quelqu'un|personne)"
    ),
    # « Je transmets votre question » (la cible est sous-entendue)
    re.compile(_TRANSFER_VERB + r"\s+(?:ta|votre|ton|vos|tes|cette|sa)\s+(?:demande|question|pr[ée]occupation|requ[êe]te|r[ée]clamation|message|remarque|souci|probl[èe]me)"),
    # « Je vais vérifier auprès de la boutique », « je me renseigne auprès de l'équipe »
    re.compile(
        r"(?:v[ée]rifi\w*|renseign\w*|demander|consulter|confirmer)[^.!?\n]{0,40}?aupr[èe]s"
        r"\s+(?:de la boutique|du magasin|de l'[ée]quipe|de mon [ée]quipe|de notre [ée]quipe|de nos [ée]quipes|du responsable|de la responsable|du g[ée]rant|d'un conseiller|de mes coll[eè]gues)"
    ),
]
# Phrases d'attente qui n'ont plus de sens une fois la promesse retirée.
_WAITING_FILLER = re.compile(r"^(?:un (?:instant|moment|petit instant|petit moment)|patiente[zr]?|merci de patienter|merci pour (?:ta|votre) patience)\b")

NEUTRAL_REPLACEMENT = "Je reste à votre disposition si vous avez d'autres questions."

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[ \t]+")


def _normalize(text: str) -> str:
    return (
        text.lower()
        .replace("\u2019", "'").replace("\u02bc", "'")
        .replace("\u00a0", " ").replace("\u202f", " ")
    )


def contains_human_promise(text: str | None) -> bool:
    if not text:
        return False
    normalized = _normalize(text)
    return any(p.search(normalized) for p in _PATTERNS)


def remove_human_promises(text: str) -> str:
    """Retire les phrases de promesse (et l'attente qui les accompagne) ; jamais de texte vide."""
    lines = [[s for s in _SENTENCE_SPLIT.split(line) if s.strip()] for line in (text or "").split("\n")]
    removed = False
    kept_lines = []
    for sentences in lines:
        kept = [s for s in sentences if not contains_human_promise(s)]
        removed = removed or len(kept) != len(sentences)
        kept_lines.append(kept)
    if not removed:
        return text
    rebuilt = []
    for kept in kept_lines:
        kept = [s.strip() for s in kept if not _WAITING_FILLER.match(_normalize(s.strip()))]
        if kept:
            rebuilt.append(" ".join(kept))
        elif rebuilt and rebuilt[-1] != "":
            rebuilt.append("")  # garde la séparation des paragraphes
    result = "\n".join(rebuilt).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result or NEUTRAL_REPLACEMENT
