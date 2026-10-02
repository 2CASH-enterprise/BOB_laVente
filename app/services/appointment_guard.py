"""
Lot 44 — Bob n'annonce un rendez-vous que s'il existe vraiment.

Incident réel du 02/10/2026 : « Je note votre demande pour un rendez-vous samedi 11 octobre à
10h » … sans avoir appelé request_appointment, puis, un message plus tard, « aucun créneau n'est
disponible samedi 11 octobre ». Le client a cru à un rendez-vous, puis à une annulation.

claims_appointment repère une annonce (« je note votre rendez-vous », « votre essai est confirmé »,
« demande enregistrée »…) ; le code vérifie ensuite qu'un outil de rendez-vous a réellement servi
dans ce même message. Une phrase au conditionnel ou négative n'est pas une annonce.
"""
import re

from app.services.promise_guard import CONDITIONAL as _WHEN

_THING = r"(?:rendez-vous|rdv|essai|visite|estimation|demande|cr[ée]neau)"
_DONE = r"(?:not[ée]e?s?|enregistr[ée]e?s?|confirm[ée]e?s?|r[ée]serv[ée]e?s?|fix[ée]e?s?|bloqu[ée]e?s?|programm[ée]e?s?|cal[ée]e?s?)"
_DAYS = r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|demain)"
_CLAIMS = [
    re.compile(_THING + r"[^.!?\n]{0,80}?\b" + _DONE + r"\b"),
    re.compile(r"\bje (?:vous |te )?(?:note|r[ée]serve|bloque|confirme|fixe|enregistre|programme|cale)\b[^.!?\n]{0,60}?(?:" + _THING + "|" + _DAYS + r")"),
    re.compile(r"\b(?:c'est (?:not[ée]|r[ée]serv[ée]|confirm[ée]|enregistr[ée]))\b[^.!?\n]{0,60}?(?:" + _THING + "|" + _DAYS + r")"),
]
_NEGATION = re.compile(r"\b(?:ne|n')\b[^.!?\n]*\b(?:pas|plus|jamais|aucun)\b|\bpas encore\b|\baucun\b")
_SENTENCE = re.compile(r"[^.!?\n]+")
# « Si vous confirmez, c'est noté pour samedi » n'annonce rien non plus.
_IF = re.compile(r"\bs(?:i |')(?:vous|tu)\b")

FALLBACK = "Pour enregistrer votre rendez-vous, pouvez-vous me confirmer le jour et l'heure qui vous conviennent ?"


def claims_appointment(text: str | None) -> bool:
    if not text:
        return False
    normalized = text.lower().replace("’", "'").replace(" ", " ")
    for sentence in _SENTENCE.findall(normalized):
        if _WHEN.search(sentence) or _IF.search(sentence) or _NEGATION.search(sentence):
            continue
        if any(p.search(sentence) for p in _CLAIMS):
            return True
    return False
