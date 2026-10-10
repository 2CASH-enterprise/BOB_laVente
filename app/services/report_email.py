"""
Lot 60 — mise en page du rapport mensuel (email HTML et page « Rapports » du tableau de bord).

Tableaux HTML uniquement, styles en ligne, aucun script ni SVG (Gmail et Outlook les retirent) : les
graphiques sont des barres en cellules de tableau, et les chiffres sont toujours écrits à côté.
SÉCURITÉ : tout texte (nom de la boutique, d'un lien, d'un commercial, libellés) est échappé.
Les données viennent de app/services/monthly_report.py (build) ; une partie vide n'est pas affichée.
"""
from html import escape as e

FONT = "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
INK, MUTED, BORDER, BG = "#13241A", "#5B6F65", "#E3EBE6", "#F4FAF6"
BRAND = "#1B4332"
DAY = "#2E9E6B"      # heures de bureau
NIGHT = "#5B5BD6"    # hors heures (soir, nuit, week-end)
NIGHT_BG = "#1E1B4B"
TRACK = "#E6EEE9"
UP, DOWN = "#1B7A4B", "#B42318"
AMBER_BG, AMBER = "#FFF7E6", "#8A5300"
TINTS = [("#E8F5EE", "#1B7A4B"), ("#EEEEFD", "#4338CA"), ("#FFF3E8", "#B54708"), ("#E8F1FB", "#1D5FA8")]


def _num(v):
    return f"{v:,}".replace(",", " ") if isinstance(v, int) else e(str(v))


def delta(d):
    if not d:
        return ""
    up = d.startswith("+")
    color, arrow = (UP, "▲") if up else (DOWN, "▼")
    return (f'<span style="display:inline-block;font-size:12px;font-weight:700;color:{color};'
            f'background:{"#E7F6EE" if up else "#FDECEA"};border-radius:999px;padding:2px 8px;">{arrow} {e(d.lstrip("+-"))}</span>')


def section(title, body, sub=""):
    return (f'<tr><td style="padding:26px 0 0;"><div style="font-size:17px;font-weight:800;color:{INK};margin:0 0 2px;">{e(title)}</div>'
            + (f'<div style="font-size:13px;color:{MUTED};margin:0 0 12px;">{e(sub)}</div>' if sub else '<div style="height:10px"></div>')
            + body + "</td></tr>")


def tiles(items):
    cells = []
    for i, item in enumerate(items):
        label, value, d = item[:3]
        declared = len(item) > 3 and item[3]
        bg, fg = TINTS[i % 4]
        cells.append(f'<td width="50%" valign="top" style="padding:5px;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
                     f'style="background:{bg};border-radius:14px;"><tr><td style="padding:12px 12px;">'
                     f'<div style="font-size:11px;color:{fg};font-weight:700;text-transform:uppercase;letter-spacing:.03em;">{e(label)}</div>'
                     f'<div style="font-size:26px;line-height:1.2;font-weight:800;color:{INK};margin:4px 0 6px;">{_num(value)}</div>{delta(d)}'
                     + (f'<div style="font-size:11px;color:{MUTED};margin:6px 0 0;">✍️ déclaré par vous</div>' if declared else "")
                     + '</td></tr></table></td>')
    rows = "".join("<tr>" + "".join(cells[i:i + 2]) + "</tr>" for i in range(0, len(cells), 2))
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 -5px;">{rows}</table>'


def hbars(rows, color=DAY, unit=""):
    """Barres horizontales : [(libellé, valeur, détail)] ; la plus grande valeur = toute la largeur."""
    top = max((r[1] for r in rows), default=1) or 1
    out = []
    for label, value, detail in rows:
        pct = max(2, round(value / top * 100))
        out.append(
            f'<tr><td style="padding:0 0 10px;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td style="font-size:14px;color:{INK};padding:0 0 4px;">{e(label)}</td>'
            f'<td align="right" style="font-size:14px;font-weight:700;color:{INK};padding:0 0 4px;white-space:nowrap;">{_num(value)}{e(unit)}</td></tr>'
            f'<tr><td colspan="2"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{TRACK};border-radius:6px;">'
            f'<tr><td width="{pct}%" style="background:{color};height:10px;border-radius:6px;font-size:0;line-height:0;">&nbsp;</td>'
            f'<td style="font-size:0;line-height:0;">&nbsp;</td></tr></table></td></tr>'
            + (f'<tr><td colspan="2" style="font-size:12px;color:{MUTED};padding:4px 0 0;">{e(detail)}</td></tr>' if detail else "")
            + "</table></td></tr>")
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(out)}</table>'


def part_bars(rows, total_label, part_label):
    """Partie d'un tout : piste claire = utilisations, partie foncée = résultats obtenus."""
    top = max((r[1] for r in rows), default=1) or 1
    out = []
    for label, total, part, rate in rows:
        w_total = max(4, round(total / top * 100))
        w_part = round(part / total * 100) if total else 0
        out.append(
            f'<tr><td style="padding:0 0 12px;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td style="font-size:14px;color:{INK};padding:0 0 4px;">{e(label)}</td>'
            f'<td align="right" style="font-size:13px;color:{MUTED};padding:0 0 4px;white-space:nowrap;"><b style="color:{INK};">{part}</b> / {total}'
            + (f' · <b style="color:{UP};">{e(rate)}</b>' if rate else "") + '</td></tr>'
            f'<tr><td colspan="2"><table role="presentation" width="{w_total}%" cellspacing="0" cellpadding="0" style="background:#BFE3CF;border-radius:6px;">'
            f'<tr><td width="{max(w_part, 1)}%" style="background:{DAY};height:12px;border-radius:6px;font-size:0;line-height:0;">&nbsp;</td>'
            f'<td style="font-size:0;line-height:0;">&nbsp;</td></tr></table></td></tr></table></td></tr>')
    legend = (f'<div style="font-size:12px;color:{MUTED};"><span style="display:inline-block;width:10px;height:10px;background:{DAY};border-radius:3px;"></span> {e(part_label)}'
              f' &nbsp; <span style="display:inline-block;width:10px;height:10px;background:#BFE3CF;border-radius:3px;"></span> {e(total_label)}</div>')
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(out)}</table>{legend}'


def columns(values, colors, labels, height=90, label_color=MUTED):
    """Colonnes verticales (une cellule par valeur), alignées en bas."""
    top = max(values) or 1
    bars = "".join(
        f'<td valign="bottom" align="center" style="padding:0 1px;"><div style="height:{max(2, round(v / top * height))}px;'
        f'background:{c};border-radius:3px 3px 0 0;font-size:0;line-height:0;">&nbsp;</div></td>'
        for v, c in zip(values, colors))
    labs = "".join(f'<td align="center" style="font-size:10px;color:{label_color};padding-top:4px;">{e(l)}</td>' for l in labels)
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="table-layout:fixed;">'
            f'<tr style="height:{height}px;">{bars}</tr><tr>{labs}</tr></table>')


def night_hero(d):
    hours = d["hours"]
    colors = [NIGHT if (h < 8 or h >= 19) else DAY for h in range(24)]
    labels = [f"{h}h" if h % 6 == 0 else "" for h in range(24)]
    stats = "".join(
        f'<td width="{100 // max(1, len(d["stats"]))}%" valign="top" style="padding:0 6px 0 0;"><div style="font-size:24px;font-weight:800;color:#FFFFFF;">{_num(v)}</div>'
        f'<div style="font-size:12px;color:#C7C9F5;line-height:1.35;">{e(l)}</div></td>' for v, l in d["stats"])
    return f"""<tr><td style="padding:22px 0 0;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{NIGHT_BG};border-radius:18px;">
<tr><td style="padding:22px 22px 18px;">
  <div style="font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#A5A8F0;">🌙 Pendant que vous étiez fermé</div>
  <div style="font-size:44px;line-height:1.05;font-weight:800;color:#FFFFFF;margin:8px 0 4px;">{_num(d["replies"])} réponse{"s" if d["replies"] > 1 else ""}</div>
  <div style="font-size:15px;color:#E0E1FA;line-height:1.5;margin:0 0 16px;">{e(d["sentence"])}</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 18px;"><tr>{stats}</tr></table>
  <div style="font-size:12px;color:#C7C9F5;margin:0 0 6px;">Messages de vos clients, heure par heure (le week-end compte entièrement hors heures)</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#2A2766;border-radius:12px;"><tr><td style="padding:12px 10px 8px;">
  {columns(hours, colors, labels, 70, "#C7C9F5")}
  </td></tr></table>
  <div style="font-size:12px;color:#C7C9F5;margin:8px 0 0;"><span style="display:inline-block;width:10px;height:10px;background:{NIGHT};border-radius:3px;"></span> Avant 8 h et après 19 h
  &nbsp; <span style="display:inline-block;width:10px;height:10px;background:{DAY};border-radius:3px;"></span> Heures de bureau</div>
</td></tr></table></td></tr>"""


def callout(title, lines, bg, fg, icon):
    items = "".join(f'<li style="margin:0 0 4px;">{e(l)}</li>' for l in lines)
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{bg};border-radius:14px;"><tr><td style="padding:16px 18px;">'
            f'<div style="font-size:14px;font-weight:800;color:{fg};margin:0 0 6px;">{icon} {e(title)}</div>'
            + (f'<ul style="margin:0;padding-left:20px;font-size:14px;color:{INK};line-height:1.5;">{items}</ul>' if len(lines) > 1 or lines[0].startswith("-") else
               f'<div style="font-size:14px;color:{INK};line-height:1.55;">{e(lines[0])}</div>')
            + "</td></tr></table>")


def never_stops(b):
    """Bob, le commercial qui ne s'arrête jamais : présence, rapidité, temps libéré et sa valeur (estimation)."""
    tiles_html = "".join(
        f'<td width="{100 // max(1, len(b["presence"]))}%" valign="top" style="padding:3px;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#FFFFFF;border-radius:12px;">'
        f'<tr><td style="padding:10px 8px;"><div style="font-size:20px;font-weight:800;color:{BRAND};line-height:1.15;">{e(v)}</div>'
        f'<div style="font-size:12px;color:{MUTED};line-height:1.35;margin-top:2px;">{e(l)}</div></td></tr></table></td>' for v, l in b["presence"])
    rows = "".join(
        f'<tr><td style="padding:7px 0;border-bottom:1px solid #CFE6D8;font-size:14px;color:{INK};">{e(l)}</td>'
        f'<td align="right" style="padding:7px 0;border-bottom:1px solid #CFE6D8;font-size:14px;font-weight:800;color:{INK};white-space:nowrap;">{e(v)}</td></tr>'
        for l, v in b["savings"])
    return f"""<tr><td style="padding:16px 0 0;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#E8F5EE;border-radius:18px;">
<tr><td style="padding:18px 14px;">
  <div style="font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:{UP};">🤖 Votre commercial qui ne s'arrête jamais</div>
  <div style="font-size:15px;color:{INK};line-height:1.5;margin:6px 0 12px;">{e(b["sentence"])}</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 -4px 14px;"><tr>{tiles_html}</tr></table>
  <div style="font-size:14px;font-weight:800;color:{INK};margin:0 0 4px;">⏱️ Le temps que Bob vous a fait gagner</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows}</table>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{BRAND};border-radius:12px;margin:12px 0 8px;"><tr><td style="padding:14px 16px;">
    <div style="font-size:12px;color:#9FD8B9;font-weight:700;text-transform:uppercase;letter-spacing:.05em;">{e(b["value_title"])}</div>
    <div style="font-size:26px;font-weight:800;color:#FFFFFF;line-height:1.2;">{e(b["value"])}</div>
    <div style="font-size:13px;color:#CFE9DB;">{e(b["compare"])}</div></td></tr></table>
  <div style="font-size:11px;color:{MUTED};line-height:1.45;">{e(b["method"])}</div>
</td></tr></table></td></tr>"""


def validate_block(v):
    """Ventes, paiements, issues de rendez-vous : c'est le commerçant qui les déclare dans Bob."""
    if not v["items"]:
        ok = (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#E8F5EE;border-radius:14px;"><tr>'
              f'<td style="padding:14px 18px;font-size:14px;color:{INK};">✅ <b>Tout est à jour</b> : vos ventes de la période sont toutes indiquées.</td></tr></table>')
        return f'<tr><td style="padding:16px 0 0;">{ok}</td></tr>'
    items = "".join(f'<li style="margin:0 0 4px;"><b>{e(n)}</b> {e(t)}</li>' for n, t in v["items"])
    return f"""<tr><td style="padding:16px 0 0;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#FFF1F0;border:1px solid #F7C9C4;border-radius:14px;">
<tr><td style="padding:16px 18px;">
  <div style="font-size:15px;font-weight:800;color:#B42318;margin:0 0 4px;">✍️ À valider pour des chiffres justes</div>
  <div style="font-size:13px;color:{INK};line-height:1.5;margin:0 0 8px;">Bob compte vos conversations tout seul. Vos ventes, vos paiements et vos rendez-vous honorés, c'est vous qui les indiquez : tant qu'ils ne sont pas validés, ils manquent dans ce rapport.</div>
  <ul style="margin:0 0 12px;padding-left:20px;font-size:14px;color:{INK};line-height:1.5;">{items}</ul>
  <a href="{e(v["url"])}" style="display:inline-block;background:#B42318;color:#fff;font-weight:700;font-size:14px;text-decoration:none;border-radius:10px;padding:10px 18px;">Valider maintenant</a>
</td></tr></table></td></tr>"""


def report(d) -> str:
    """Email complet. d = données de monthly_report.build, plus first_name (destinataire)."""
    hello = f"Bonjour {e(d['first_name'])}," if d.get("first_name") else "Bonjour,"
    body = [
        f'<tr><td style="font-size:15px;color:{INK};line-height:1.6;">{hello}<br>Voici ce que Bob a fait pour <b>{e(d["shop"])}</b> du {e(d["period"])}.</td></tr>',
        section("Bob en chiffres", tiles(d["tiles"]), d["tiles_sub"]),
    ]
    if d["night"]["replies"]:
        body.append(night_hero(d["night"]))
    body.append(never_stops(d["bob"]))
    body.append(validate_block(d["to_validate"]))
    if any(d["daily"]):
        day_colors = [NIGHT if i in d["weekend_days"] else DAY for i in range(len(d["daily"]))]
        step = 5 if len(d["daily"]) <= 40 else 15
        day_labels = [str(n) if i % step == 0 else "" for i, n in enumerate(d["daily_dates"])]
        body.append(section("Vos conversations jour par jour", columns(d["daily"], day_colors, day_labels, 80)
                            + f'<div style="font-size:12px;color:{MUTED};margin:6px 0 0;"><span style="display:inline-block;width:10px;height:10px;background:{NIGHT};border-radius:3px;"></span> Week-end'
                              f' &nbsp; <span style="display:inline-block;width:10px;height:10px;background:{DAY};border-radius:3px;"></span> Semaine'
                            + (f' · meilleur jour : {e(d["best_day"])}' if d.get("best_day") else "") + "</div>"))
    body.append(section(d["funnel_title"], hbars(d["funnel"], BRAND), d["funnel_sub"]))
    if d["intents"]:
        body.append(section("Ce que demandent vos clients", hbars(d["intents"], DAY), "Les demandes les plus fréquentes, en messages"))
    if d["objections"]:
        body.append(section("Réponses aux objections", part_bars(d["objections"], "Utilisations", d["obtained_label"]),
                            d["objections_sub"]))
    if d["sources"]:
        body.append(section("D'où viennent vos clients", hbars(d["sources"], "#1D5FA8"), "Nouveaux clients par source"))
    if d.get("commercials"):
        body.append(section("Résultats par commercial", hbars(d["commercials"], "#B54708"), "Prospects par commercial"))
    if d["todo"]:
        body.append(section("Le mois qui vient", callout("À faire maintenant", d["todo"], AMBER_BG, AMBER, "⏳")))
    if d.get("advice"):
        body.append(f'<tr><td style="padding:14px 0 0;">{callout("Notre conseil", [d["advice"]], "#E8F5EE", UP, "💡")}</td></tr>')
    body.append(f'<tr><td align="center" style="padding:24px 0 4px;"><a href="{e(d["url"])}" style="display:inline-block;background:{BRAND};color:#fff;'
                f'font-weight:700;font-size:15px;text-decoration:none;border-radius:10px;padding:13px 26px;">Voir le détail dans Bob</a></td></tr>')
    if d.get("renewal"):
        body.append(f'<tr><td style="padding:22px 0 0;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid {BORDER};border-radius:14px;">'
                    f'<tr><td style="padding:14px 18px;font-size:14px;color:{INK};line-height:1.55;"><b>Votre abonnement</b><br>{e(d["renewal"])}</td></tr></table></td></tr>')
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light only"><title>{e(d["subject"])}</title></head>
<body style="margin:0;padding:0;background:{BG};">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{BG};"><tr><td align="center" style="padding:14px 6px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;font-family:{FONT};">
<tr><td style="background:{BRAND};border-radius:18px 18px 0 0;padding:20px 18px;">
  <div style="font-size:13px;font-weight:700;color:#9FD8B9;letter-spacing:.06em;text-transform:uppercase;">Bob · rapport mensuel</div>
  <div style="font-size:24px;line-height:1.25;font-weight:800;color:#FFFFFF;margin:6px 0 0;">{e(d["headline"])}</div>
  <div style="font-size:14px;color:#CFE9DB;margin:4px 0 0;">{e(d["shop"])} · {e(d["period"])}</div>
  {f'<div style="display:inline-block;margin:10px 0 0;background:#FFF7E6;color:#8A5300;font-size:13px;font-weight:700;border-radius:999px;padding:4px 12px;">⏰ {e(d["renewal_short"])}</div>' if d.get("renewal_short") else ""}
</td></tr>
<tr><td style="background:#FFFFFF;border:1px solid {BORDER};border-top:0;border-radius:0 0 18px 18px;padding:20px 16px 24px;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(body)}</table>
</td></tr>
<tr><td style="padding:16px 12px;font-size:12px;line-height:1.5;color:{MUTED};text-align:center;">{"<br>".join(e(line) for line in d["footer"])}</td></tr>
</table></td></tr></table></body></html>"""


def text(d) -> str:
    """Version texte (messageries simples, et version de secours)."""
    lines = [f"Bonjour {d['first_name']}," if d.get("first_name") else "Bonjour,", "",
             f"Voici ce que Bob a fait pour {d['shop']} du {d['period']}.", "", "BOB EN CHIFFRES"]
    for item in d["tiles"]:
        label, value, delta_ = item[:3]
        declared = " (déclaré par vous)" if len(item) > 3 and item[3] else ""
        lines.append(f"- {label} : {value}{' (' + delta_ + ')' if delta_ else ''}{declared}")
    if d["night"]["replies"]:
        lines += ["", "PENDANT QUE VOUS ÉTIEZ FERMÉ", f"{d['night']['replies']} réponses de Bob le soir, la nuit et le week-end."]
        lines += [f"- {v} {label}" for v, label in d["night"]["stats"]]
    lines += ["", "VOTRE COMMERCIAL QUI NE S'ARRÊTE JAMAIS"] + [f"- {v} : {label}" for v, label in d["bob"]["presence"]]
    lines += [f"- {label} : {v}" for label, v in d["bob"]["savings"]]
    if d["to_validate"]["items"]:
        lines += ["", "À VALIDER POUR DES CHIFFRES JUSTES"] + [f"- {n} {t}" for n, t in d["to_validate"]["items"]]
    lines += ["", d["funnel_title"].upper()] + [f"- {label} : {v}" for label, v, _ in d["funnel"]]
    if d["intents"]:
        lines += ["", "CE QUE DEMANDENT VOS CLIENTS"] + [f"- {label} : {v} messages" for label, v, _ in d["intents"]]
    if d["sources"]:
        lines += ["", "D'OÙ VIENNENT VOS CLIENTS"] + [f"- {label} : {v} clients{' · ' + x if x else ''}" for label, v, x in d["sources"]]
    if d.get("commercials"):
        lines += ["", "RÉSULTATS PAR COMMERCIAL"] + [f"- {label} : {v} prospects{' · ' + x if x else ''}" for label, v, x in d["commercials"]]
    if d["todo"]:
        lines += ["", "LE MOIS QUI VIENT"] + [f"- {t}" for t in d["todo"]]
    if d.get("advice"):
        lines += ["", "NOTRE CONSEIL", d["advice"]]
    lines += ["", f"Voir le détail : {d['url']}"]
    if d.get("renewal"):
        lines += ["", "VOTRE ABONNEMENT", d["renewal"]]
    lines += ["", "— L'équipe Bob", ""] + d["footer"]
    return "\n".join(lines)
