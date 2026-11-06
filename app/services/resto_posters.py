"""
Lot 108 b — supports imprimés du restaurant (maquettes validées le 06/11, canvas « Bob — supports QR du restaurant »).

- « resto_vitrine » (A4) : « Commandez et réservez sur WhatsApp », photo d'un plat au choix, QR code du kit ;
- « resto_comptoir » (A5, clair) : « Notre carte sur WhatsApp », les rubriques de la carte, QR code du kit ;
- « plat » (A4, sombre) : un plat (plat du jour, spécialité ou nouveauté) avec sa photo ronde, sa composition, ses
  accompagnements et son prix, QR code du plat ;
- « tables » (A4) : les chevalets des tables (A6), 4 par page, chacun avec le QR code de sa table.

Mêmes polices et même mise en page imprimable que le lot 103 (vitrine_posters). Ce qui manque n'apparaît pas ; tout ce
qui vient du restaurant est échappé ; signature « Proposé par Bob ».
"""
from datetime import datetime, timezone
from html import escape

from app.services import vitrine_posters as vp

R_INK, R_CREAM, R_PANEL, R_MUTED, R_SOFT = "#14110F", "#F4EEE3", "#2A231D", "#CFC6B6", "#D9D0C1"
RESTO_KIT_MODELS = ("resto_vitrine", "resto_comptoir")
BANNERS = {"jour": "Plat du jour", "specialite": "Notre spécialité", "nouveau": "Nouveau"}
MODE_WORDS = {"PICKUP": "À emporter", "DELIVERY": "Livraison", "DINE_IN": "Sur place"}
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
          "décembre"]


def _dot(accent: str) -> str:
    return f'<span style="color:{accent}">·</span>'


def _modes(tenant, accent: str) -> str:
    from app.services import restaurant

    words = [MODE_WORDS[m] for m in restaurant.modes(tenant)]
    return f" {_dot(accent)} ".join(escape(w) for w in words)


def _clock(text: str) -> str:
    hour, _, minute = text.partition(":")
    return f"{int(hour)} h" + (f" {minute}" if minute and minute != "00" else "")


def short_hours(tenant) -> str | None:
    """« Cuisine ouverte de 11 h à 23 h, 7 jours sur 7 » quand tous les jours ouverts ont les mêmes horaires ; sinon rien."""
    hours = getattr(tenant, "kitchen_hours", None) or {}
    days = {day: tuple(tuple(r) for r in (hours.get(str(day)) or [])) for day in range(7)}
    open_days = [ranges for ranges in days.values() if ranges]
    if not open_days or len(set(open_days)) != 1 or len(open_days[0]) > 2:
        return None
    ranges = " et ".join(f"de {_clock(a)} à {_clock(b)}" for a, b in open_days[0])
    return f"Cuisine ouverte {ranges}" + (", 7 jours sur 7" if len(open_days) == 7 else "")


def _price(product) -> str:
    from app.services.product_cards import money_text

    if not product.price or float(product.price) <= 0:
        return ""
    text = money_text(product.price, product.currency)
    return text[:-4] + ' <span style="font-size:.48em;letter-spacing:.14em">FCFA</span>' if text.endswith(" XOF") else escape(text)


def today_line(tenant, now: datetime | None = None) -> str:
    from app.services.local_time import tenant_zone

    local = (now or datetime.now(timezone.utc)).astimezone(tenant_zone(tenant))
    return f"{DAYS[local.weekday()].capitalize()} {local.day} {MONTHS[local.month - 1]}"


def _ornament(accent: str, width: int = 70) -> str:
    return (f'<div aria-hidden="true" style="margin-top:12px;display:flex;align-items:center;gap:12px">'
            f'<div style="width:{width}px;height:1px;background:{accent}"></div>'
            f'<div style="width:7px;height:7px;transform:rotate(45deg);border:1px solid {accent}"></div>'
            f'<div style="width:{width}px;height:1px;background:{accent}"></div></div>')


def _signature(color: str) -> str:
    return f'<div style="font-size:12px;letter-spacing:.06em;color:{color};white-space:nowrap">Proposé par <span style="font-weight:500">Bob</span></div>'


# --- Affiches du kit ---------------------------------------------------------------------------------------------------

def kit_poster(tenant, data: dict, model: str, accent: str | None, photo: str | None, extra: dict) -> str:
    if model not in RESTO_KIT_MODELS:
        raise vp.PosterError("Modèle d'affiche inconnu.")
    dark, light = vp.accent_of(accent or "safran")
    title = f"Affiche — {tenant.name}"
    if model == "resto_vitrine":
        return vp._page(title, model, _vitrine(tenant, data, dark, photo, extra), R_INK)
    return vp._page(title, model, _comptoir(tenant, data, light, extra), R_CREAM)


def _vitrine(tenant, data, accent, photo, extra) -> str:
    tu = vp._tu(tenant)
    books = extra.get("books", False)
    head = (f'<div style="position:absolute;left:0;right:0;top:42px;display:flex;flex-direction:column;align-items:center;gap:8px;color:{R_CREAM}">'
            f'<div class="name" style="font-size:20px">{escape(tenant.name)}</div>'
            f'<div class="lbl" style="font-weight:400;letter-spacing:.28em;color:#E2D8C6">Restaurant'
            f'{" · " + escape(extra["city"]) if extra.get("city") else ""}</div></div>')
    if photo:
        top = (f'<div style="position:relative;height:560px;flex:0 0 auto;overflow:hidden"><img class="photo" src="{escape(photo)}" alt="">'
               f'<div style="position:absolute;inset:0;background:linear-gradient(180deg,rgba(20,17,15,.72) 0%,rgba(20,17,15,0) 26%,'
               f'rgba(20,17,15,0) 68%,rgba(20,17,15,1) 100%)"></div>{head}</div>')
    else:
        top = f'<div style="position:relative;height:240px;flex:0 0 auto;border-bottom:1px solid {accent}">{head}</div>'
    verbs = ("Commande et réserve" if tu else "Commandez et réservez") if books else ("Commande" if tu else "Commandez")
    lead = ("Scanne le code : notre carte s'ouvre, tu choisis en quelques touches." if tu
            else "Scannez le code : notre carte s'ouvre, vous choisissez en quelques touches.")
    actions = ["Commander"] + (["Réserver une table"] if books else []) + ["Voir la carte"]
    row = "".join(f'<div class="serif" style="font-size:25px;font-style:italic">{escape(a)}</div>' for a in actions)
    hours = short_hours(tenant)
    hours_line = f'<div style="font-size:12px;letter-spacing:.08em;color:{R_SOFT}">{escape(hours)}</div>' if hours else ""
    return f"""{top}
<div style="flex-grow:1;padding:44px 64px 36px 64px;display:flex;flex-direction:column;color:{R_CREAM}">
  <div style="display:flex;gap:48px;align-items:flex-end">
    <div style="flex:1 1 auto;display:flex;flex-direction:column;gap:16px">
      <div class="lbl" style="color:{accent}">Sans attendre, sans appeler</div>
      <h1 class="serif" style="font-size:56px;line-height:1;letter-spacing:-.01em">{verbs} <span style="font-style:italic;color:{accent}">sur WhatsApp.</span></h1>
      <p style="margin:0;font-weight:300;font-size:17px;line-height:1.55;color:{R_SOFT};max-width:390px">{lead}</p>
    </div>
    <div style="flex:0 0 auto;display:flex;flex-direction:column;align-items:center;gap:18px">
      {vp._qr_box(data["qr_svg"], 164, 10, accent, 7, "QR code WhatsApp du restaurant")}
      <div class="lbl" style="color:{accent}">{"Scanne" if tu else "Scannez"}</div>
    </div>
  </div>
  <div style="flex-grow:1"></div>
  <div style="display:flex;justify-content:space-between;gap:12px;border-top:1px solid {accent};border-bottom:1px solid rgba(201,145,61,.35);padding:16px 0">{row}</div>
  <div class="lbl" style="display:flex;justify-content:center;gap:22px;padding-top:14px;font-weight:400;letter-spacing:.26em;color:{R_SOFT}">{_modes(tenant, accent)}</div>
  <div style="display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding-top:24px">
    <div style="display:flex;flex-direction:column;gap:4px"><div style="font-size:22px;letter-spacing:.1em">{escape(data["phone"])}</div>{hours_line}</div>
    {_signature(R_MUTED)}
  </div>
</div>"""


def _comptoir(tenant, data, accent, extra) -> str:
    tu = vp._tu(tenant)
    sections = extra.get("sections") or []
    kicker = " · ".join(escape(s) for s in sections[:4]) or "La carte du jour"
    lead = ("Les plats du jour, les prix, les accompagnements. Tu commandes, on prépare." if tu
            else "Les plats du jour, les prix, les accompagnements. Vous commandez, on prépare.")
    return f"""<div style="flex-grow:1;padding:40px 48px 28px 48px;display:flex;flex-direction:column;align-items:center;color:{R_INK}">
  <div class="name" style="font-size:15px;text-align:center">{escape(tenant.name)}</div>
  <div style="width:56px;height:1px;background:{accent};margin-top:14px"></div>
  <div class="lbl" style="margin-top:40px;letter-spacing:.32em;color:{accent};text-align:center">{kicker}</div>
  <h1 class="serif" style="margin-top:14px;font-size:54px;line-height:1;text-align:center">Notre carte <span style="display:block;font-style:italic">sur WhatsApp</span></h1>
  <p style="margin:18px 0 0 0;font-weight:300;font-size:16px;line-height:1.5;text-align:center;color:#4A433A;max-width:380px">{lead}</p>
  <div style="margin-top:34px">{vp._qr_box(data["qr_svg"], 186, 12, R_INK, 8, "QR code de la carte")}</div>
  <div style="flex-grow:1"></div>
  <div class="serif" style="display:flex;justify-content:center;gap:18px;font-style:italic;font-size:21px;padding-bottom:16px">{_modes(tenant, accent)}</div>
  <div style="width:100%;display:flex;justify-content:space-between;align-items:baseline;border-top:1px solid {R_MUTED};padding-top:12px">
    <div style="font-size:17px;letter-spacing:.1em">{escape(data["phone"])}</div>{_signature("#5E564B")}
  </div>
</div>"""


# --- Affiche d'un plat (QR code du plat) -------------------------------------------------------------------------------

def dish_facts(product) -> dict:
    from app.services.resto_flow import sides_of

    sheet = product.sheet or {}
    composition = vp._clean(sheet.get("composition") or sheet.get("menu_content"))
    return {"name": vp._clean(product.name)[:70], "composition": composition[:160], "sides": sides_of(product)[:4],
            "price": _price(product)}


def dish_poster(tenant, product, phone: str, qr_svg: str, accent: str | None, banner: str | None,
                now: datetime | None = None) -> str:
    dark, _ = vp.accent_of(accent or "safran")
    word = BANNERS.get(banner or "jour", BANNERS["jour"])
    dateline = today_line(tenant, now) if word == BANNERS["jour"] else "À la carte"
    f = dish_facts(product)
    photo = vp.photo_of(product)
    plate = (f'<div style="margin-top:30px;width:400px;height:400px;flex:0 0 auto;border-radius:50%;overflow:hidden;'
             f'outline:1px solid {dark};outline-offset:12px;position:relative"><img class="photo" src="{escape(photo)}" alt=""></div>'
             if photo else '<div style="flex-grow:1"></div>' + _ornament(dark, 110))  # sans photo : le plat au centre
    composition = (f'<p class="serif" style="margin:12px 0 0 0;font-style:italic;font-size:21px;line-height:1.4;color:{R_SOFT};'
                   f'text-align:center;max-width:540px">{escape(f["composition"])}</p>' if f["composition"] else "")
    sides = ""
    if f["sides"]:
        words = f" {_dot(dark)} ".join(f"<span>{escape(s)}</span>" for s in f["sides"])
        sides = (f'<div class="lbl" style="margin-top:14px;display:flex;flex-wrap:wrap;justify-content:center;gap:10px;font-weight:400;'
                 f'letter-spacing:.26em;color:{R_MUTED}"><span>Au choix</span> {_dot(dark)} {words}</div>')
    price = ""
    if f["price"]:
        rule = '<div aria-hidden="true" style="width:54px;height:1px;background:rgba(201,145,61,.6)"></div>'
        price = (f'<div style="margin-top:22px;display:flex;align-items:center;gap:18px">{rule}'
                 f'<div class="serif" style="font-size:46px;line-height:1">{f["price"]}</div>{rule}</div>')
    tu = vp._tu(tenant)
    body = f"""<div style="flex-grow:1;margin:24px;border:1px solid {dark};padding:6px;display:flex">
<div style="flex-grow:1;border:1px solid rgba(201,145,61,.35);padding:34px 52px 24px 52px;display:flex;flex-direction:column;align-items:center;color:{R_CREAM}">
  <div class="name" style="font-size:15px;letter-spacing:.36em;text-align:center">{escape(tenant.name)}</div>
  {_ornament(dark)}
  <div class="serif" style="margin-top:18px;font-style:italic;font-weight:400;font-size:76px;line-height:.95;color:{dark}">{escape(word)}</div>
  <div class="lbl" style="margin-top:10px;font-weight:400;letter-spacing:.32em;color:{R_MUTED}">{escape(dateline)}</div>
  {plate}
  <h1 class="serif" style="margin-top:40px;font-size:54px;line-height:1;text-align:center">{escape(f["name"])}</h1>
  {composition}{sides}{price}
  <div style="flex-grow:1"></div>
  <div style="width:100%;display:flex;justify-content:space-between;align-items:center;gap:24px;border-top:1px solid rgba(201,145,61,.45);padding-top:18px">
    <div style="display:flex;gap:14px;align-items:center">
      {vp._qr_box(qr_svg, 96, 6, R_CREAM, 0, "QR code du plat")}
      <div style="display:flex;flex-direction:column;gap:4px">
        <div class="serif" style="font-style:italic;font-size:22px;line-height:1.1">{"Commande-le" if tu else "Commandez-le"} sur WhatsApp</div>
        <div class="lbl" style="font-weight:400;letter-spacing:.14em;white-space:nowrap;color:{dark}">{_modes(tenant, dark)}</div>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
      <div style="font-size:17px;letter-spacing:.1em;white-space:nowrap">{escape(phone)}</div>{_signature(R_MUTED)}
    </div>
  </div>
</div></div>"""
    return vp._page(f"{word} — {product.name}", "plat", body, R_INK)


# --- Chevalets des tables --------------------------------------------------------------------------------------------

def _table_card(tenant, label: str, qr_svg: str, accent: str, tu: bool) -> str:
    steps = ("Scanne", "Choisis", "On te sert") if tu else ("Scannez", "Choisissez", "On vous sert")
    line = f" {_dot(accent)} ".join(f"<span>{s}</span>" for s in steps)
    return f"""<div class="card" style="width:105mm;height:148mm;padding:26px 30px 18px 30px;background:{R_INK};color:{R_CREAM};
  display:flex;flex-direction:column;align-items:center;outline:1px dashed #B5ADA0;outline-offset:-1px;overflow:hidden">
  <div class="name" style="font-size:13px;text-align:center">{escape(tenant.name)}</div>
  <div style="width:48px;height:1px;background:{accent};margin-top:12px"></div>
  <div style="margin-top:14px;display:flex;align-items:center;gap:14px">
    <div class="lbl" style="font-weight:400;letter-spacing:.34em;color:{R_MUTED}">Table</div>
    <div class="serif" style="font-style:italic;font-size:{92 if len(label) <= 3 else 64}px;line-height:.9;color:{accent}">{escape(label)}</div>
  </div>
  <h1 class="serif" style="margin-top:10px;font-size:31px;line-height:1.05;text-align:center">{"Commande" if tu else "Commandez"}<br><span style="font-style:italic">depuis {"ta" if tu else "votre"} table</span></h1>
  <div style="margin-top:22px">{vp._qr_box(qr_svg, 150, 10, accent, 7, f"QR code de la table {label}")}</div>
  <div class="serif" style="margin-top:24px;display:flex;gap:10px;font-style:italic;font-size:18px">{line}</div>
  <div style="flex-grow:1"></div>
  <div style="width:100%;display:flex;justify-content:space-between;align-items:baseline;border-top:1px solid rgba(201,145,61,.45);padding-top:10px">
    <div class="lbl" style="font-weight:400;letter-spacing:.2em;color:{R_MUTED}">Sur WhatsApp</div>{_signature(R_MUTED)}
  </div>
</div>"""


def table_sheet(tenant, tables: list[tuple[str, str]], accent: str | None = None) -> str:
    """Les chevalets (A6), 4 par feuille A4, traits de coupe en pointillés."""
    if not tables:
        raise vp.PosterError("Créez d'abord les QR codes de vos tables.")
    dark, _ = vp.accent_of(accent or "safran")
    tu = vp._tu(tenant)
    cards = [_table_card(tenant, label, svg, dark, tu) for label, svg in tables]
    sheets = ['<div style="display:grid;grid-template-columns:105mm 105mm;grid-template-rows:148mm 148mm;background:#fff">'
              + "".join(cards[i:i + 4]) + "</div>" for i in range(0, len(cards), 4)]
    return vp._page(f"Chevalets des tables — {tenant.name}", "tables", "", "#FFFFFF", sheets=sheets)
