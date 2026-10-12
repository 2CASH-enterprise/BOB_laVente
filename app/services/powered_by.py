"""
Lot 74 — mention « Propulsé par Bob » des réponses WhatsApp au plan gratuit.

WhatsApp ne permet pas de choisir la taille du texte : la mention est donc mise à part (ligne seule,
après une ligne vide, en italique) et le tableau de bord l'affiche en petit et en gris.

Elle est ajoutée UNE seule fois, par le code, en toute fin de message. Bob ne doit jamais la voir dans
sa mémoire : sinon il la recopie dans sa réponse et le client la reçoit en double.
"""
import re

WHATSAPP_POWERED_BY = "_Propulsé par Bob 🤖_"

# Toute ligne qui n'est que la mention, quelle que soit sa forme (italique, gras, emoji, tiret…).
_POWERED_LINE = re.compile(r"^[\s_*~>•\-—–]*propuls[ée]e?\s+par\s+bob\b[^\n]*$", re.IGNORECASE | re.MULTILINE)


def strip_powered_by(text: str | None) -> str:
    """Retire toute ligne « Propulsé par Bob » (et les lignes vides qu'elle laisse en fin de message)."""
    if not text:
        return text or ""
    cleaned = _POWERED_LINE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def add_powered_by(text: str) -> str:
    """La mention, une seule fois, seule sur la dernière ligne."""
    body = strip_powered_by(text)
    return f"{body}\n\n{WHATSAPP_POWERED_BY}" if body else WHATSAPP_POWERED_BY
