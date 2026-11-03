"""
Lot 103 — affiches vitrine premium (maquettes validées le 03/11).

Cinq modèles, imprimables tels quels (format réglé par @page, fonds imprimés) :
- « agence » (A4) : photo d'un bien ou d'un produit choisi par le commerçant, accroche, QR code du kit ;
- « sombre » (A4) : version épurée sur fond sombre, QR code du kit ;
- « chevalet » (A5) : carte de comptoir ou de porte, QR code du kit ;
- « bien » (A4, agences immobilières) : fiche d'un bien avec son QR code, prix TOUJOURS avec les honoraires (loi ALUR),
  DPE et GES, référence du mandat, adresse et carte professionnelle de l'agence ;
- « vitre » (20 × 20 cm) : autocollant à coller à côté d'un produit ou d'un bien, avec son QR code.

Tous les secteurs ont leur accroche. Polices Cormorant Garamond et Jost (licence OFL) intégrées à la page : rien
n'est chargé chez Google (RGPD). Une information absente ne laisse jamais de case vide : la ligne disparaît.
Tout ce qui vient du commerçant est échappé. Signature discrète « Proposé par Bob ».
"""
import base64
import re
from functools import lru_cache
from html import escape
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
_FONTS = (  # (famille, graisse, style, fichier)
    ("Cormorant Garamond", 400, "italic", "cormorant-garamond-latin-400-italic.woff2"),
    ("Cormorant Garamond", 500, "normal", "cormorant-garamond-latin-500-normal.woff2"),
    ("Cormorant Garamond", 500, "italic", "cormorant-garamond-latin-500-italic.woff2"),
    ("Jost", 300, "normal", "jost-latin-300-normal.woff2"),
    ("Jost", 400, "normal", "jost-latin-400-normal.woff2"),
    ("Jost", 500, "normal", "jost-latin-500-normal.woff2"),
)

KIT_MODELS = ("agence", "sombre", "chevalet")
PRODUCT_MODELS = ("bien", "vitre")
MODEL_LABELS = {"agence": "Affiche avec photo (A4)", "sombre": "Affiche sombre épurée (A4)",
                "chevalet": "Chevalet de comptoir (A5)", "bien": "Affiche du bien (A4)",
                "vitre": "Autocollant vitre (20 × 20 cm)"}
# Teinte d'accent : (sur fond sombre, sur fond clair) — la seconde, plus foncée, garde le contraste sur l'ivoire.
ACCENTS = {"or": ("#B89A67", "#8A6D3B"), "champagne": ("#C9B79C", "#7A6A4F"),
           "sauge": ("#A3B1A0", "#4F6150"), "rose": ("#D4A59A", "#8C4F45")}
ACCENT_LABELS = {"or": "Or", "champagne": "Champagne", "sauge": "Sauge", "rose": "Rose poudré"}
SIZES = {"agence": (210, 297), "sombre": (210, 297), "bien": (210, 297), "chevalet": (148, 210), "vitre": (200, 200)}

INK, IVORY, MUTED, RULE = "#0E1116", "#F5F2EC", "#5E5A53", "#CFC8BA"
SIGNATURE = "Proposé par Bob"

SECTOR_WORD = {"REAL_ESTATE": "Immobilier", "CAR_DEALERSHIP": "Automobile", "INSURANCE_BROKER": "Assurance",
               "ONLINE_STORE": "Boutique"}
KICKER = {"REAL_ESTATE": "Votre projet immobilier", "CAR_DEALERSHIP": "Votre prochain véhicule",
          "INSURANCE_BROKER": "Votre assurance", "ONLINE_STORE": "Vos achats"}
SERVICES = {"CAR_DEALERSHIP": ["Disponibilité", "Essai", "Reprise", "Rendez-vous"],
            "INSURANCE_BROKER": ["Devis", "Renouvellement", "Votre contrat", "Réclamation"],
            "ONLINE_STORE": ["Questions", "Commandes", "Livraison", "Suivi"]}
TAGLINE = {"REAL_ESTATE": "Nos biens, vos questions, une réponse immédiate.",
           "CAR_DEALERSHIP": "Nos véhicules, vos questions, une réponse immédiate.",
           "INSURANCE_BROKER": "Vos devis, vos questions, une réponse immédiate.",
           "ONLINE_STORE": "Nos produits, vos questions, une réponse immédiate."}
CHEVALET_KICKER = {"REAL_ESTATE": "Nos biens et nos services", "CAR_DEALERSHIP": "Nos véhicules disponibles",
                   "INSURANCE_BROKER": "Devis et contrats", "ONLINE_STORE": "Nos produits"}
VITRE = {  # (accroche, titre, titre en italique, ligne)
    "REAL_ESTATE": ("Ce bien vous plaît ?", "Visitez-le", "depuis la rue.", "Photos, prix, visite : scannez, on vous répond."),
    "CAR_DEALERSHIP": ("Ce véhicule vous plaît ?", "Réservez", "votre essai.", "Prix, essai, reprise : scannez, on vous répond."),
    "INSURANCE_BROKER": ("Une question ?", "Votre devis", "en un message.", "Devis, contrat : scannez, on vous répond."),
    "ONLINE_STORE": ("Ce produit vous plaît ?", "Commandez-le", "en un message.", "Prix, stock, livraison : scannez, on vous répond."),
}
VITRE_TU = {
    "ONLINE_STORE": ("Ce produit te plaît ?", "Commande-le", "en un message.", "Prix, stock, livraison : scanne, on te répond."),
}


class PosterError(ValueError):
    pass


def _sector(tenant) -> str:
    from app.services.business_type import normalize

    return normalize(getattr(tenant, "business_type", None))


def _tu(tenant) -> bool:
    from app.services.address_form import uses_tu

    return uses_tu(tenant)


def services(tenant) -> list[str]:
    """Les quatre mots du bandeau ; une agence n'affiche que ses activités déclarées."""
    sector = _sector(tenant)
    if sector != "REAL_ESTATE":
        return SERVICES.get(sector, SERVICES["ONLINE_STORE"])
    from app.services.real_estate import activities_of

    acts = activities_of(tenant)
    words = (["Acheter"] if "VENTE" in acts else []) + (["Louer"] if "LOCATION" in acts else []) \
        + (["Vendre", "Faire estimer"] if "VENTE" in acts else []) + (["Mettre en location"] if "GESTION" in acts else [])
    return words[:4]


def accent_of(name: str | None) -> tuple[str, str]:
    return ACCENTS.get(name or "or", ACCENTS["or"])


@lru_cache(maxsize=1)
def fonts_css() -> str:
    rules = []
    for family, weight, style, filename in _FONTS:
        data = base64.b64encode((FONTS_DIR / filename).read_bytes()).decode()
        rules.append(f"@font-face{{font-family:'{family}';font-style:{style};font-weight:{weight};font-display:block;"
                     f"src:url(data:font/woff2;base64,{data}) format('woff2')}}")
    return "".join(rules)


def safe_image(url: str | None) -> str | None:
    """Seulement une adresse web complète (http ou https), sans guillemet ni chevron."""
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s\"'<>]+$", url):
        return None
    return url


def photo_of(product) -> str | None:
    """La photo d'origine du commerçant (pas le détourage sur fond blanc), sinon celle des cartes."""
    return safe_image(getattr(product, "image_original_url", None)) or safe_image(getattr(product, "image_url", None))


def scalable(svg: str) -> str:
    """Le QR code de segno a une largeur fixe sans viewBox : on lui en donne un pour qu'il suive la taille du cadre."""
    match = re.match(r'(<svg\b[^>]*?)\swidth="(\d+)"\sheight="(\d+)"', svg)
    if not match or "viewBox" in svg[:match.end()]:
        return svg
    return f'{match.group(1)} viewBox="0 0 {match.group(2)} {match.group(3)}"' + svg[match.end():]


def _qr_box(svg: str, size: int, pad: int, ring: str, offset: int, label: str) -> str:
    svg = scalable(svg)
    return (f'<div class="qr" role="img" aria-label="{escape(label)}" style="width:{size}px;height:{size}px;padding:{pad}px;'
            f'outline:1px solid {ring};outline-offset:{offset}px">{svg}</div>')


def _page(title: str, model: str, body: str, ground: str) -> str:
    width, height = SIZES[model]
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
{fonts_css()}
@page {{ size: {width}mm {height}mm; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; }}
body {{ background: #E9E6E0; font-family: Jost, system-ui, sans-serif; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.bar {{ position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; justify-content: center;
  padding: 12px 16px; background: #fff; border-bottom: 1px solid #ddd; font: 14px system-ui, sans-serif; color: #222; }}
.bar button {{ background: {INK}; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px; font: inherit; font-weight: 600; cursor: pointer; }}
.bar span {{ color: #555; font-size: 13px; }}
.sheet {{ width: {width}mm; height: {height}mm; margin: 24px auto; overflow: hidden; position: relative; background: {ground};
  box-shadow: 0 8px 30px rgba(0,0,0,.18); display: flex; flex-direction: column; }}
.serif {{ font-family: 'Cormorant Garamond', Georgia, serif; font-variant-numeric: lining-nums; }}
.lbl {{ font-weight: 500; font-size: 12px; letter-spacing: .3em; text-transform: uppercase; }}
.name {{ font-weight: 500; letter-spacing: .32em; text-transform: uppercase; }}
.qr {{ background: #fff; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }}
.qr svg {{ width: 100%; height: 100%; display: block; }}
.sig {{ font-size: 12px; letter-spacing: .24em; text-transform: uppercase; text-align: center; }}
.photo {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; object-position: center 50%; }}
h1 {{ margin: 0; font-weight: 500; }}
@media print {{ body {{ background: none; }} .bar {{ display: none; }} .sheet {{ margin: 0; box-shadow: none; }} }}
</style></head>
<body>
<div class="bar"><button type="button" onclick="window.print()">Imprimer ou enregistrer en PDF</button>
<span>Format {escape(MODEL_LABELS[model].split("(")[-1].rstrip(")"))} · marges « Aucune » · échelle 100 %</span></div>
<div class="sheet">
{body}
</div>
</body></html>"""


def _footer(left: str, right: str, rule: str, left_color: str, sig_color: str, size: int = 20) -> str:
    return (f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:16px;border-top:1px solid {rule};padding-top:16px">'
            f'<div class="lbl" style="font-weight:400;letter-spacing:.24em;color:{left_color}">{left}</div>'
            f'<div style="font-size:{size}px;letter-spacing:.12em;white-space:nowrap">{right}</div></div>'
            f'<div class="sig" style="margin-top:14px;color:{sig_color}">{SIGNATURE}</div>')


# --- Les trois affiches du kit (QR code du kit) ----------------------------------------------------------------------

def kit_poster(tenant, data: dict, model: str, accent: str | None = None, photo: str | None = None) -> str:
    if model not in KIT_MODELS:
        raise PosterError("Modèle d'affiche inconnu.")
    sector, tu = _sector(tenant), _tu(tenant)
    dark, _ = accent_of(accent)
    name, phone = escape(tenant.name), escape(data["phone"])
    word = escape(SECTOR_WORD.get(sector, "Boutique"))
    scan = "Scanne" if tu else "Scannez"
    title = f"Affiche — {tenant.name}"
    if model == "agence":
        return _page(title, model, _agence(name, word, phone, data["qr_svg"], sector, tu, dark, photo, services(tenant), scan), INK)
    if model == "sombre":
        return _page(title, model, _sombre(name, word, phone, data["qr_svg"], sector, dark, services(tenant), scan), INK)
    return _page(title, model, _chevalet(name, phone, data["qr_svg"], sector, tu, dark, photo), INK)


def _agence(name, word, phone, qr, sector, tu, accent, photo, words, scan) -> str:
    lead = ("Scanne le code : notre assistant te répond sur WhatsApp, jour et nuit." if tu
            else "Scannez le code : notre assistant vous répond sur WhatsApp, jour et nuit.")
    header = (f'<div style="position:absolute;left:0;right:0;top:44px;display:flex;flex-direction:column;align-items:center;gap:8px;color:{IVORY}">'
              f'<div class="name" style="font-size:20px">{name}</div>'
              f'<div class="lbl" style="font-weight:400;letter-spacing:.28em;color:#E7DFD0">{word}</div></div>')
    if photo:
        top = (f'<div style="position:relative;height:640px;flex:0 0 auto;overflow:hidden">'
               f'<img class="photo" src="{escape(photo)}" alt="">'
               f'<div style="position:absolute;inset:0;background:linear-gradient(180deg,rgba(14,17,22,.62) 0%,rgba(14,17,22,0) 22%,'
               f'rgba(14,17,22,0) 70%,rgba(14,17,22,1) 100%)"></div>{header}</div>')
        lift = "margin-top:-40px;"
    else:
        top = f'<div style="position:relative;height:250px;flex:0 0 auto;border-bottom:1px solid {accent}">{header}</div>'
        lift = "padding-top:64px;"
    services_row = "".join(f'<div class="serif" style="font-size:24px;font-style:italic">{escape(w)}</div>' for w in words)
    return f"""{top}
<div style="flex-grow:1;padding:0 64px 36px 64px;{lift}display:flex;flex-direction:column;position:relative;color:{IVORY}">
  <div style="display:flex;gap:48px;align-items:flex-end">
    <div style="flex:1 1 auto;display:flex;flex-direction:column;gap:18px">
      <div class="lbl" style="color:{accent}">{escape(KICKER.get(sector, KICKER["ONLINE_STORE"]))}</div>
      <h1 class="serif" style="font-size:66px;line-height:1;letter-spacing:-.01em">Une question ? <span style="font-style:italic;color:{accent}">Une réponse,</span> tout de suite.</h1>
      <p style="margin:0;font-weight:300;font-size:17px;line-height:1.55;color:#D9D4CA;max-width:380px">{lead}</p>
    </div>
    <div style="flex:0 0 auto;display:flex;flex-direction:column;align-items:center;gap:18px">
      {_qr_box(qr, 168, 10, accent, 7, "QR code WhatsApp")}
      <div class="lbl" style="color:{accent}">{scan}</div>
    </div>
  </div>
  <div style="flex-grow:1"></div>
  <div style="display:flex;justify-content:space-between;gap:12px;border-top:1px solid {accent};padding:16px 0;margin-bottom:18px">{services_row}</div>
  {_footer("WhatsApp · 7 jours sur 7", phone, "rgba(184,154,103,.35)", "#D9D4CA", "#A8A398", 22)}
</div>"""


def _sombre(name, word, phone, qr, sector, accent, words, scan) -> str:
    line = " · ".join(escape(w) for w in words)
    return f"""<div style="flex-grow:1;margin:40px;border:1px solid {accent};padding:64px 56px 36px 56px;display:flex;flex-direction:column;align-items:center;color:{IVORY}">
  <div class="name" style="font-size:18px;letter-spacing:.36em;text-align:center">{name}</div>
  <div class="lbl" style="margin-top:10px;font-weight:400;color:#CFC8BA">{word}</div>
  <div style="margin-top:36px;width:56px;height:1px;background:{accent}"></div>
  <h1 class="serif" style="margin-top:72px;font-weight:400;font-style:italic;font-size:120px;line-height:.9;color:{accent}">{scan}</h1>
  <p class="serif" style="margin:22px 0 0 0;font-weight:500;font-size:32px;line-height:1.2;text-align:center;max-width:520px">{escape(TAGLINE.get(sector, TAGLINE["ONLINE_STORE"]))}</p>
  <div style="margin-top:56px">{_qr_box(qr, 196, 12, accent, 10, "QR code WhatsApp")}</div>
  <div class="lbl" style="margin-top:52px;font-weight:400;letter-spacing:.32em;color:#CFC8BA;text-align:center">{line}</div>
  <div style="flex-grow:1"></div>
  <div style="width:100%">{_footer("WhatsApp · 7 jours sur 7", phone, "rgba(184,154,103,.45)", "#CFC8BA", "#A8A398")}</div>
</div>"""


def _chevalet(name, phone, qr, sector, tu, accent, photo) -> str:
    title = ("Scanne, <span style=\"font-style:italic;color:{a}\">on te répond.</span>" if tu
             else "Scannez, <span style=\"font-style:italic;color:{a}\">on vous répond.</span>").format(a=accent)
    head = f'<div class="name" style="position:absolute;left:0;right:0;top:30px;text-align:center;font-size:15px;letter-spacing:.34em;color:{IVORY}">{name}</div>'
    if photo:
        top = (f'<div style="position:relative;height:300px;flex:0 0 auto;overflow:hidden"><img class="photo" src="{escape(photo)}" alt="">'
               f'<div style="position:absolute;inset:0;background:linear-gradient(180deg,rgba(14,17,22,.55) 0%,rgba(14,17,22,0) 35%,'
               f'rgba(14,17,22,0) 65%,rgba(14,17,22,1) 100%)"></div>{head}</div>')
    else:
        top = f'<div style="position:relative;height:150px;flex:0 0 auto">{head}<div style="position:absolute;left:50%;bottom:30px;width:56px;margin-left:-28px;height:1px;background:{accent}"></div></div>'
    return f"""{top}
<div style="flex-grow:1;padding:4px 44px 28px 44px;display:flex;flex-direction:column;align-items:center;color:{IVORY}">
  <div class="lbl" style="letter-spacing:.32em;color:{accent};text-align:center">{escape(CHEVALET_KICKER.get(sector, CHEVALET_KICKER["ONLINE_STORE"]))}</div>
  <h1 class="serif" style="margin-top:12px;font-size:44px;line-height:1;text-align:center">{title}</h1>
  <div style="margin-top:34px">{_qr_box(qr, 172, 10, accent, 8, "QR code WhatsApp")}</div>
  <div style="flex-grow:1"></div>
  <div style="width:100%">{_footer("WhatsApp", phone, "rgba(184,154,103,.45)", "#CFC8BA", "#A8A398", 17)}</div>
</div>"""


# --- Les affiches d'un produit ou d'un bien (QR code du produit) ------------------------------------------------------

def _clean(value) -> str:
    return " ".join(str(value or "").split())


def bien_facts(product) -> dict:
    """Tout ce que l'affiche du bien montre, tiré de la fiche ; ce qui manque n'apparaît pas."""
    from app.services.immo_flow import _place
    from app.services.product_cards import money_text
    from app.services.real_estate import _plain

    sheet = product.sheet or {}
    rent = _plain(sheet.get("transaction")) == _plain("Location")
    specs = []
    for key, label, unit in (("surface", "Habitables", " m²"), ("rooms", "Pièces", ""), ("bedrooms", "Chambres", ""),
                             ("land_surface", "Terrain", " m²")):
        value = _clean(sheet.get(key))
        if value:
            specs.append((value if (not unit or value.lower().endswith("m²") or value.lower().endswith("m2")) else value + unit, label))
    fees = _clean(sheet.get("fees"))
    price = float(product.price or 0)
    if fees and price > 0:
        amount = money_text(product.price, product.currency).replace(" EUR", " €")  # une affiche française écrit « € »
        if rent:
            notes = [f"Loyer charges comprises{', dont ' + _clean(sheet['charges']) + ' de charges' if _clean(sheet.get('charges')) else ''}."]
            if _clean(sheet.get("deposit")):
                notes.append(f"Dépôt de garantie : {_clean(sheet['deposit'])}.")
            notes.append(f"Honoraires : {fees}.")
        else:
            notes = [f"Prix honoraires inclus. Honoraires : {fees}."]
    else:  # loi ALUR : jamais un prix sans ses honoraires
        amount, notes = None, ["Prix et honoraires sur demande auprès de l'agence."]
    energy = [(label, _clean(sheet.get(key))) for key, label in (("dpe", "DPE"), ("ges", "GES")) if _clean(sheet.get(key))]
    return {"rent": rent, "banner": "À louer" if rent else "À vendre", "place": _place(product), "specs": specs[:4],
            "amount": amount, "notes": [n.replace("..", ".") for n in notes], "energy": energy,
            "reference": _clean(sheet.get("reference")), "energy_cost": _clean(sheet.get("energy_cost"))}


def product_poster(tenant, product, phone: str, qr_svg: str, model: str, accent: str | None = None) -> str:
    if model not in PRODUCT_MODELS:
        raise PosterError("Modèle d'affiche inconnu.")
    _, light = accent_of(accent)
    if model == "vitre":
        return _page(f"Autocollant — {product.name}", model, _vitre(tenant, qr_svg, light), IVORY)
    if _sector(tenant) != "REAL_ESTATE":
        raise PosterError("L'affiche d'un bien est réservée aux agences immobilières.")
    photo = photo_of(product)
    if not photo:
        raise PosterError("Ajoutez d'abord une photo à ce bien (adresse web complète) pour son affiche.")
    return _page(f"Affiche — {product.name}", model, _bien(tenant, product, phone, qr_svg, light, photo), IVORY)


def _bien(tenant, product, phone, qr, accent, photo) -> str:
    f = bien_facts(product)
    profile = getattr(tenant, "real_estate_profile", None) or {}
    ref = f'<div class="lbl" style="font-weight:400;letter-spacing:.24em;color:{MUTED}">Réf. {escape(f["reference"])}</div>' if f["reference"] else ""
    specs = ""
    if f["specs"]:
        cells = "".join(
            f'<div style="padding:12px 0 12px {0 if i == 0 else 18}px;{"" if i == 0 else f"border-left:1px solid {RULE};"}display:flex;flex-direction:column;gap:2px">'
            f'<div class="serif" style="font-size:26px">{escape(v)}</div>'
            f'<div class="lbl" style="font-weight:400;letter-spacing:.2em;color:{MUTED}">{escape(label)}</div></div>'
            for i, (v, label) in enumerate(f["specs"]))
        specs = (f'<div style="margin-top:22px;display:grid;grid-template-columns:repeat({len(f["specs"])},minmax(0,1fr));'
                 f'border-top:1px solid {INK};border-bottom:1px solid {RULE}">{cells}</div>')
    if f["amount"]:
        per = ' <span style="font-size:24px;font-style:italic">par mois</span>' if f["rent"] else ""
        price = f'<div class="serif" style="font-size:46px;line-height:1">{escape(f["amount"])}{per}</div>'
    else:
        price = '<div class="serif" style="font-size:34px;line-height:1.05;font-style:italic">Prix sur demande</div>'
    notes = "".join(f'<div style="font-size:12px;line-height:1.5;color:{MUTED};max-width:390px">{escape(n)}</div>' for n in f["notes"])
    energy = "".join(f'<div>{escape(label)} <span style="font-weight:500">{escape(value)}</span></div>' for label, value in f["energy"])
    energy_row = (f'<div class="lbl" style="display:flex;gap:18px;margin-top:6px;font-weight:400;letter-spacing:.2em;color:{INK}">{energy}</div>'
                  if energy else "")
    legal = " · ".join(escape(x) for x in (_clean(profile.get("address")),
                                            _card_line(_clean(profile.get("card")))) if x)
    place = f'<div class="lbl" style="margin-top:28px;color:{accent}">{escape(f["place"])}</div>' if f["place"] else '<div style="margin-top:28px"></div>'
    return f"""<div style="flex-grow:1;padding:44px 48px 28px 48px;display:flex;flex-direction:column;color:{INK}">
  <div style="display:flex;justify-content:space-between;align-items:baseline;gap:16px">
    <div class="name" style="font-size:17px">{escape(tenant.name)}</div>{ref}
  </div>
  <div style="position:relative;margin-top:22px;height:440px;flex:0 0 auto;overflow:hidden">
    <img class="photo" src="{escape(photo)}" alt="">
    <div class="lbl" style="position:absolute;left:0;top:28px;background:{INK};color:{IVORY};padding:12px 22px 12px 24px;font-size:13px;letter-spacing:.34em">{f["banner"]}</div>
  </div>
  {place}
  <h1 class="serif" style="margin-top:10px;font-size:46px;line-height:1.04;letter-spacing:-.01em">{escape(product.name[:90])}</h1>
  {specs}
  <div style="flex-grow:1;margin-top:22px;display:flex;justify-content:space-between;align-items:flex-start;gap:32px">
    <div style="flex:1 1 auto;display:flex;flex-direction:column;gap:8px">{price}{notes}{energy_row}</div>
    <div style="flex:0 0 auto;display:flex;gap:16px;align-items:center">
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;text-align:right">
        <div class="serif" style="font-style:italic;font-size:22px;line-height:1.1">Photos, visite,<br>questions</div>
        <div class="lbl" style="font-weight:400;letter-spacing:.24em;color:{accent}">Scannez</div>
      </div>
      {_qr_box(qr, 124, 8, INK, 5, "QR code du bien")}
    </div>
  </div>
  {_footer(legal, escape(phone), RULE, MUTED, MUTED, 18)}
</div>"""


def _card_line(card: str) -> str:
    if not card:
        return ""
    return card if card.lower().startswith("carte") else f"Carte professionnelle {card}"


def _vitre(tenant, qr, accent) -> str:
    sector = _sector(tenant)
    kicker, title, italic, line = (VITRE_TU if _tu(tenant) else VITRE).get(sector, VITRE.get(sector, VITRE["ONLINE_STORE"]))
    return f"""<div style="flex-grow:1;margin:28px;border:1px solid {accent};padding:52px 48px 26px 48px;display:flex;flex-direction:column;align-items:center;color:{INK}">
  <div class="lbl" style="letter-spacing:.32em;color:{accent};text-align:center">{escape(kicker)}</div>
  <h1 class="serif" style="margin-top:16px;font-size:62px;line-height:1;text-align:center;letter-spacing:-.01em">{escape(title)} <span style="font-style:italic">{escape(italic)}</span></h1>
  <div style="margin-top:44px">{_qr_box(qr, 216, 12, INK, 10, "QR code")}</div>
  <div class="serif" style="margin-top:34px;font-style:italic;font-size:24px;text-align:center">{escape(line)}</div>
  <div style="flex-grow:1"></div>
  <div style="width:100%;display:flex;justify-content:space-between;align-items:baseline;border-top:1px solid {RULE};padding-top:14px">
    <div class="name" style="font-size:14px">{escape(tenant.name)}</div>
    <div class="lbl" style="font-weight:400;letter-spacing:.24em;color:{MUTED}">Sur WhatsApp</div>
  </div>
  <div class="sig" style="margin-top:10px;color:{MUTED}">{SIGNATURE}</div>
</div>"""
