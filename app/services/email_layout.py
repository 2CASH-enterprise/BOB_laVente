"""
Lot 40 — mise en page commune de tous les emails de Bob (HTML + version texte de secours).

Chaque email reste écrit en texte simple (lisible partout, et c'est la version de secours) ; sa
version HTML est construite à partir de ce texte, avec quelques conventions :
- « Bonjour, » et les paragraphes séparés par une ligne vide → paragraphes ;
- un paragraphe d'au moins deux lignes « Libellé : valeur » → petit tableau (client, raison…) ;
- une ligne seule « Libellé : https://… » → bouton (« Ouvrir la conversation ») ;
- un paragraphe fait d'un seul code de 4 à 8 chiffres → code affiché en grand ;
- « Titre : » suivi de lignes « - … » → liste ;
- après une ligne « — » : pied de page (petits caractères, liens simples).

SÉCURITÉ : tout ce qui vient de l'extérieur (nom et message d'un client, nom de la boutique, texte
d'une campagne) est échappé ; seuls des liens http(s) deviennent cliquables.
"""
import html
import re

from app.core.config import get_settings

_URL = re.compile(r"https?://[^\s<>\"«»]+")
_FIELD = re.compile(r"^([^:\n]{1,40}?) : (.+)$")
_CODE = re.compile(r"^\d{4,8}$")
FOOTER_SEPARATOR = "—"

GREEN = "#1B4332"
TEXT = "#13241A"
MUTED = "#5B6F65"
BORDER = "#E3EBE6"
BG = "#F4FAF6"
FONT = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def _inline(text: str, link_color: str = GREEN) -> str:
    """Texte échappé ; les adresses http(s) deviennent des liens."""
    out, last = [], 0
    for match in _URL.finditer(text):
        out.append(_e(text[last:match.start()]))
        url = match.group(0).rstrip(".,;)")
        tail = match.group(0)[len(url):]
        out.append(f'<a href="{_e(url)}" style="color: {link_color}; word-break: break-all;">{_e(url)}</a>{_e(tail)}')
        last = match.end()
    out.append(_e(text[last:]))
    return "<br>".join("".join(out).split("\n"))


def _button(label: str, url: str) -> str:
    label = label.strip().lstrip("👉 ").strip() or "Ouvrir"
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" style="margin: 22px 0 8px;"><tr>'
        f'<td style="border-radius: 10px; background: {GREEN};">'
        f'<a href="{_e(url)}" style="display: inline-block; padding: 13px 24px; font-family: {FONT}; font-size: 15px; '
        f'font-weight: 700; color: #ffffff; text-decoration: none; border-radius: 10px;">{_e(label)}</a>'
        "</td></tr></table>"
        f'<p class="muted" style="margin: 0 0 18px; font-size: 12px; color: {MUTED}; word-break: break-all;">'
        f'Le bouton ne marche pas ? Copiez ce lien : <a href="{_e(url)}" style="color: {MUTED};">{_e(url)}</a></p>'
    )


def _details(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td class="detail" style="padding: 8px 12px 8px 0; border-bottom: 1px solid {BORDER}; color: {MUTED}; '
        f'font-size: 13px; white-space: nowrap; vertical-align: top;">{_e(label)}</td>'
        f'<td class="detail" style="padding: 8px 0; border-bottom: 1px solid {BORDER}; font-size: 14px; font-weight: 600;">'
        f"{_inline(value)}</td></tr>"
        for label, value in rows
    )
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin: 4px 0 18px;">{cells}</table>'


def _block(paragraph: str) -> str:
    lines = [line.rstrip() for line in paragraph.split("\n") if line.strip()]
    if not lines:
        return ""
    if len(lines) == 1 and _CODE.match(lines[0].strip()):
        return (f'<p class="code" style="margin: 8px 0 20px; font-family: Menlo, Consolas, monospace; font-size: 34px; font-weight: 700; '
                f'letter-spacing: 8px; color: {GREEN};">{_e(lines[0].strip())}</p>')
    if len(lines) == 1:
        field = _FIELD.match(lines[0])
        if field and _URL.fullmatch(field.group(2).strip()):
            return _button(field.group(1), field.group(2).strip())
        if _URL.fullmatch(lines[0].strip()):
            return _button("Ouvrir le lien", lines[0].strip())
    if len(lines) >= 2 and all(_FIELD.match(line) for line in lines):
        return _details([_FIELD.match(line).groups() for line in lines])
    bullets = [line for line in lines[1:] if line.lstrip().startswith("- ")]
    if lines[0].endswith(":") and bullets and len(bullets) == len(lines) - 1:
        items = "".join(f'<li style="margin: 0 0 4px;">{_inline(b.lstrip()[2:])}</li>' for b in bullets)
        return (f'<p style="margin: 0 0 6px; font-weight: 700;">{_inline(lines[0])}</p>'
                f'<ul style="margin: 0 0 18px; padding-left: 20px;">{items}</ul>')
    return f'<p style="margin: 0 0 16px;">{_inline(chr(10).join(lines))}</p>'


def render_html(subject: str, body: str, brand: str, for_customer: bool) -> str:
    """Email complet (HTML) à partir de son texte. brand = nom affiché en tête (boutique ou Bob)."""
    text = body.replace("\r\n", "\n").strip()
    footer_text = ""
    marker = f"\n{FOOTER_SEPARATOR}\n"
    if marker in text:
        text, footer_text = text.split(marker, 1)
    blocks = "".join(_block(p) for p in re.split(r"\n\s*\n", text))
    preheader = " ".join(re.sub(r"https?://\S+", "", text.replace("Bonjour,", "", 1)).split())[:140]

    if for_customer:
        header = f'<span class="brand" style="font-size: 18px; font-weight: 800; color: {GREEN};">{_e(brand)}</span>'
        default_footer = f"Cet email vous est envoyé par {_e(brand)}."
    else:
        logo = get_settings().public_base_url.rstrip("/") + "/dashboard/icon-192.png"
        header = (f'<img src="{_e(logo)}" width="32" height="32" alt="" style="vertical-align: middle; border-radius: 8px; border: 0;">'
                  f'<span class="brand" style="font-size: 18px; font-weight: 800; color: {GREEN}; vertical-align: middle; margin-left: 8px;">Bob</span>')
        default_footer = "Bob AI — votre vendeur sur WhatsApp. Vous recevez cet email car votre boutique utilise Bob."
    footer = "".join(f'<p style="margin: 0 0 6px;">{_inline(p.strip(), MUTED)}</p>'
                     for p in re.split(r"\n\s*\n|\n", footer_text) if p.strip()) or f'<p style="margin: 0;">{default_footer}</p>'

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_e(subject)}</title>
<style>
  @media (prefers-color-scheme: dark) {{
    body, .bg {{ background: #0D1511 !important; }}
    .title {{ color: #E4EEE8 !important; }}
    .brand, .code {{ color: #7FD6A8 !important; }}
    .card {{ background: #15201A !important; color: #E4EEE8 !important; border-color: #24342B !important; }}
    .muted, .detail {{ color: #9AAEA2 !important; border-color: #24342B !important; }}
  }}
  @media (max-width: 620px) {{ .card {{ padding: 22px !important; }} }}
</style>
</head>
<body style="margin: 0; padding: 0; background: {BG};">
<div style="display: none; max-height: 0; overflow: hidden; opacity: 0;">{_e(preheader)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" class="bg" style="background: {BG};">
<tr><td align="center" style="padding: 24px 12px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 560px;">
<tr><td style="padding: 0 4px 14px; font-family: {FONT};">{header}</td></tr>
<tr><td class="card" style="background: #ffffff; border: 1px solid {BORDER}; border-radius: 16px; padding: 32px; font-family: {FONT}; font-size: 15px; line-height: 1.6; color: {TEXT};">
<h1 class="title" style="margin: 0 0 18px; font-size: 20px; line-height: 1.35; color: {TEXT};">{_e(subject)}</h1>
{blocks}
</td></tr>
<tr><td class="muted" style="padding: 16px 12px; font-family: {FONT}; font-size: 12px; line-height: 1.5; color: {MUTED}; text-align: center;">{footer}</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


POWERED_BY = "Propulsé par Bob AI 🤖"


def customer_footer(shop_name: str, powered_by: bool = False, lines: list[str] | None = None, tu: bool = False) -> str:
    """Pied de page d'un email de la boutique à son client (plan gratuit : « Propulsé par Bob »)."""
    parts = list(lines or []) or [f"Cet email {'t' + chr(39) + 'est' if tu else 'vous est'} envoyé par {shop_name}."]
    if powered_by:
        parts.append(POWERED_BY)
    return f"\n\n{FOOTER_SEPARATOR}\n" + "\n".join(parts)
