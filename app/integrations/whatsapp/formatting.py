"""
Lot 31 — mise en forme WhatsApp.

Le modèle d'IA écrit en Markdown (`**gras**`, `### titre`, `- puce`, `[lien](url)`) ; WhatsApp a sa
propre syntaxe (`*gras*`, `_italique_`, `~barré~`) et affiche le reste tel quel, étoiles comprises.
La conversion est faite par le CODE, juste avant l'envoi : aucune consigne à l'IA, aucun risque
qu'elle l'oublie. Une réponse déjà au format WhatsApp ressort inchangée.
"""
import re

_FENCE = re.compile(r"```")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_BOLD_WITH_SPACES = re.compile(r"(\*\*|__)[ \t]*([^*_\n]+?)[ \t]*\1")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HR = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)
_TRIPLE_BLANK = re.compile(r"\n{3,}")


def _bold(text: str) -> str:
    inner = text.strip().strip("*").strip()
    return f"*{inner}*" if inner else ""


def to_whatsapp(text: str | None) -> str | None:
    if not text:
        return text
    # Les blocs de code (```) sont laissés tels quels : WhatsApp les affiche en police fixe.
    parts = _FENCE.split(text)
    for i in range(0, len(parts), 2):
        parts[i] = _convert(parts[i])
    return "```".join(parts).strip("\n")


def _convert(text: str) -> str:
    text = _HR.sub("", text)
    text = _HEADING.sub(lambda m: _bold(m.group(1)), text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD.sub(lambda m: _bold(m.group(2)), text)
    text = _BOLD_WITH_SPACES.sub(lambda m: _bold(m.group(2)), text)  # « ** Prix ** » écrit avec espaces
    text = _STRIKE.sub(lambda m: f"~{m.group(1)}~", text)
    text = _LINK.sub(lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    return _TRIPLE_BLANK.sub("\n\n", text)
