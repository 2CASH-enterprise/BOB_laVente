"""
Lot 75 — dernier nettoyage d'une réponse ÉCRITE PAR L'IA, juste avant l'envoi (jamais les messages fixes) :

- les indications techniques entre crochets (« 📸 *[Photo envoyée séparément]* ») : test réel du 16/10 ;
- un seul emoji par message (consigne souvent ignorée : 😊 dans presque chaque message).
- lot 73 h : « l’*InfinX Fatbike* » — WhatsApp n'affiche pas le gras collé à une lettre ou une apostrophe (les étoiles
  restent visibles). Le mot qui précède entre dans le gras : « *l’InfinX Fatbike* ».
"""
import re

# Ligne qui n'est qu'une indication entre crochets autour d'une photo / image / pièce jointe.
_PLACEHOLDER_LINE = re.compile(
    r"^[^\w\n]*\[[^\]\n]*(photo|image|pi[eè]ce jointe|visuel)[^\]\n]*\][^\w\n]*$", re.IGNORECASE | re.MULTILINE)
_PLACEHOLDER_INLINE = re.compile(r"\s*[*_]*\[[^\]\n]*(photo|image)[^\]\n]*envoy[ée]e?s?[^\]\n]*\][*_]*", re.IGNORECASE)

# Un emoji : pictogramme (+ variante, teinte, séquences ZWJ) ou drapeau.
_EMOJI = re.compile(
    "(?:[\U0001F1E6-\U0001F1FF]{2}"
    "|[\U0001F300-\U0001FAFF☀-➿⭐⭕⌚⌛⏩-⏳⤴⤵]"
    "[️\U0001F3FB-\U0001F3FF]*(?:‍[\U0001F300-\U0001FAFF☀-➿][️]*)*)"
)
_SPACES = re.compile(r"(?<=\S)[ \t]{2,}")
_TRIPLE_BLANK = re.compile(r"\n{3,}")


# Gras WhatsApp (*…*) ou Markdown (**…**) collé à un mot qui finit par une apostrophe ou une lettre.
_GLUED_BOLD = re.compile(r"(?<![\w*])([\w]+['’])(\*{1,2})(?=\S)([^*\n]+?)(?<=\S)\2(?![\w*])")
_GLUED_LETTER_BOLD = re.compile(r"(?<=[^\W\d_])(\*{1,2})(?=\S)([^*\n]+?)(?<=\S)\1(?![\w*])")


def unglue_bold(text: str) -> str:
    text = _GLUED_BOLD.sub(lambda m: f"{m.group(2)}{m.group(1)}{m.group(3)}{m.group(2)}", text)
    return _GLUED_LETTER_BOLD.sub(lambda m: m.group(2), text)  # « du*mot* » : rien à sauver, le gras est retiré


_TRACE_LINE = re.compile(r"^[^\w\n]*(cartes envoy[ée]es|photo envoy[ée]e)\s*:[^\n]*$", re.IGNORECASE | re.MULTILINE)


def remove_placeholders(text: str) -> str:
    text = _TRACE_LINE.sub("", text)  # lot 76 : traces du tableau de bord recopiées par l'IA
    text = _PLACEHOLDER_LINE.sub("", text)
    return _PLACEHOLDER_INLINE.sub("", text)


def keep_one_emoji(text: str) -> str:
    seen = False

    def repl(match: re.Match) -> str:
        nonlocal seen
        if seen:
            return ""
        seen = True
        return match.group(0)

    return _EMOJI.sub(repl, text)


def clean_ai_reply(text: str | None) -> str:
    if not text:
        return text or ""
    text = unglue_bold(keep_one_emoji(remove_placeholders(text)))
    lines = [_SPACES.sub(" ", line).rstrip() for line in text.split("\n")]
    return _TRIPLE_BLANK.sub("\n\n", "\n".join(lines)).strip()
