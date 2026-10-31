"""
Lot 98 — parcours à boutons des cabinets de courtage et agents d'assurance (écrans « Assu »), validé le 31/10.

But : une demande de devis COMPLÈTE, transmise au cabinet avec l'accord explicite du client (bouton « Envoyer au
cabinet » = accord horodaté, CIMA, lot 54), par insurance.update_request — exactement comme quand Bob la remplit par
écrit. Jamais de prix, de prime ni de pourcentage (lot 53) ; aucun document demandé ; aucun appel d'IA.
Aussi : « Mon contrat » (registre des contrats, lot 55, sans la prime), « Faire une réclamation » (registre et accusé
de réception officiel, lot 57), « Être rappelé » (créneau d'appel, même outil de rendez-vous que Bob).

Boutons (préfixe « g: ») : g:menu, g:qd (devis), g:qc:<P|E> (pour vous / entreprise), g:qb:<BRANCHE>,
g:qa:<BRANCHE>:<champ>:<choix>, g:qs:<BRANCHE> (envoyer), g:ap (être rappelé), g:mc (mes contrats),
g:mc:<contrat>, g:mr:<contrat>:<R|M> (renouveler, modifier), g:rc (réclamation), g:sl:A:-:<créneau|x>, g:human.
"""
import re
import uuid

from sqlalchemy import select

from app.services import auto_flow
from app.services.auto_flow import HUMAN, PREFIX, _buttons, _turn, _you

# Questions par branche : (champ, type, question, choix [(libellé, valeur)]) — type : btn, list, text, int.
YES_NO_COVER = [("Tiers", "Tiers (responsabilité civile)"), ("Tous risques", "Tous risques"), ("Je ne sais pas", "À conseiller")]
COUNTS = [(str(n), str(n)) for n in range(1, 6)] + [("6 ou plus", "6")]
SCRIPTS = {
    "AUTO": [("usage", "btn", "le véhicule sert pour…", [("Usage privé", "Usage privé"), ("Travail", "Travail"),
                                                          ("Taxi ou transport", "Taxi ou transport")]),
             ("coverage", "btn", "quelle formule vous intéresse ?", YES_NO_COVER),
             ("vehicle", "text", "la marque, le modèle et l'année du véhicule ? (par exemple : Toyota Corolla 2018)", None)],
    "MOTO": [("usage", "btn", "la moto sert pour…", [("Usage privé", "Usage privé"), ("Livraison", "Livraison"),
                                                    ("Taxi-moto", "Taxi-moto")]),
             ("vehicle", "text", "la marque et le modèle ?", None)],
    "SANTE": [("persons_count", "list", "combien de personnes à assurer ?", COUNTS),
              ("ages", "text", "leurs âges ? (par exemple : 35, 32, 8)", None),
              ("coverage", "btn", "quelle couverture ?", [("Hospitalisation", "Hospitalisation"),
                                                         ("Tous les soins", "Hospitalisation et soins courants"),
                                                         ("Je ne sais pas", "À conseiller")])],
    "HABITATION": [("occupancy", "btn", "vous êtes…", [("Propriétaire", "Propriétaire"), ("Locataire", "Locataire")]),
                   ("housing_type", "btn", "quel logement ?", [("Appartement", "Appartement"), ("Maison ou villa", "Maison ou villa"),
                                                                ("Studio", "Studio")])],
    "VOYAGE": [("destination", "text", "quelle destination ?", None),
               ("travel_dates", "text", "quand, et pour combien de temps ?", None),
               ("persons_count", "list", "combien de voyageurs ?", COUNTS)],
    "VIE_PREVOYANCE": [("objective", "list", "quel est votre objectif ?",
                        [("Épargne", "Épargne"), ("Retraite", "Retraite"), ("Études des enfants", "Études des enfants"),
                         ("Obsèques", "Obsèques"), ("Protéger mes proches", "Protéger mes proches")]),
                       ("age", "int", "votre âge ?", None)],
    "SCOLAIRE": [("children_count", "list", "combien d'enfants ?", COUNTS),
                 ("ages", "text", "leurs âges ? (par exemple : 6, 9)", None)],
    "RC_PRO": [("company_name", "text", "le nom de votre entreprise ?", None),
               ("activity", "text", "son activité ?", None),
               ("employees", "int", "combien de salariés ?", None)],
    "FLOTTE": [("company_name", "text", "le nom de votre entreprise ?", None),
               ("vehicles_count", "int", "combien de véhicules ?", None),
               ("usage", "btn", "ils servent pour…", [("Transport personnes", "Transport de personnes"),
                                                      ("Marchandises", "Transport de marchandises"),
                                                      ("Véhicules service", "Véhicules de service")])],
    "MULTIRISQUE_PRO": [("company_name", "text", "le nom de votre entreprise ?", None),
                        ("activity", "text", "son activité ?", None),
                        ("premises", "text", "vos locaux : quel type, dans quelle ville ?", None)],
    "MARCHANDISES": [("company_name", "text", "le nom de votre entreprise ?", None),
                     ("goods", "text", "quelles marchandises ?", None),
                     ("route", "text", "quel trajet, et par quel moyen (route, mer, air) ?", None)],
    "AUTRE": [("description", "text", "décrivez votre besoin en une phrase.", None)],
}
YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def _branches_of(products) -> list[str]:
    from app.services.insurance import BRANCHES, SKU_PREFIX

    codes = []
    for p in products:
        code = (p.sku or "")[len(SKU_PREFIX):] if (p.sku or "").startswith(SKU_PREFIX) else None
        if code in BRANCHES and code != "AUTRE" and p.active and code not in codes:
            codes.append(code)
    return codes


async def offered(db, tenant) -> list[str]:
    """Les assurances que le cabinet propose (ses produits actifs) ; aucune déclarée = toutes."""
    from app.models.product import Product
    from app.services.insurance import DEFAULT_PRODUCTS

    products = (await db.execute(select(Product).where(Product.tenant_id == tenant.id, Product.active.is_(True)))).scalars().all()
    return _branches_of(products) or list(DEFAULT_PRODUCTS)


SHORT = {"AUTO": "Auto", "MOTO": "Moto", "SANTE": "Santé", "HABITATION": "Habitation", "VOYAGE": "Voyage",
         "VIE_PREVOYANCE": "Vie et prévoyance", "SCOLAIRE": "Scolaire", "RC_PRO": "RC professionnelle",
         "FLOTTE": "Flotte automobile", "MULTIRISQUE_PRO": "Multirisque pro", "MARCHANDISES": "Marchandises",
         "AUTRE": "Autre besoin"}


def _short(code: str) -> str:
    return SHORT.get(code, code)[:24]


async def _request(db, tenant, customer, branch):
    from app.services.insurance import open_request

    return await open_request(db, tenant.id, customer.id, branch)


# --- Écrans ---------------------------------------------------------------------------------------------------------

async def welcome(db, tenant, tu):
    rows = [(f"{PREFIX}qd", "Demander un devis", None), (f"{PREFIX}mc", "Mon contrat", _you(tu, "Échéance, renouvellement", "Échéance, renouvellement")),
            (f"{PREFIX}rc", "Faire une réclamation", _you(tu, "Ou déclarer un sinistre", "Ou déclarer un sinistre")),
            (f"{PREFIX}human", "Parler à un conseiller", None)]
    body = (f"Bonjour et bienvenue au *{tenant.name}* ! Je suis Bob, l'assistant du cabinet. "
            + _you(tu, "Que souhaitez-vous ?", "Que souhaites-tu ?"))
    return _turn([{"type": "list", "body": body, "button": "Choisir", "rows": rows}])


async def quote_start(db, tenant, tu):
    from app.services.insurance import BRANCHES, ENTREPRISE, PARTICULIER

    codes = await offered(db, tenant)
    sides = {BRANCHES[c][1] for c in codes}
    if sides == {PARTICULIER, ENTREPRISE}:
        return _turn([_buttons(_you(tu, "C'est pour vous, ou pour votre entreprise ?", "C'est pour toi, ou pour ton entreprise ?"),
                               [(f"{PREFIX}qc:P", "Pour moi"), (f"{PREFIX}qc:E", "Pour mon entreprise")])])
    return await branch_list(db, tenant, tu, "E" if sides == {ENTREPRISE} else "P")


async def branch_list(db, tenant, tu, side):
    from app.services.insurance import BRANCHES, ENTREPRISE, PARTICULIER

    wanted = ENTREPRISE if side == "E" else PARTICULIER
    codes = [c for c in await offered(db, tenant) if BRANCHES[c][1] == wanted][:9]
    rows = [(f"{PREFIX}qb:{c}", _short(c), (BRANCHES[c][4] or None) and BRANCHES[c][4][:72]) for c in codes]
    rows.append((f"{PREFIX}qb:AUTRE", "Autre besoin", None))
    return _turn([{"type": "list", "body": "Que voulez-vous assurer ?" if not tu else "Que veux-tu assurer ?",
                   "button": "Choisir", "rows": rows}])


def _steps(branch, details) -> list:
    """Questions restantes (une réponse « vide » n'existe pas : chaque question est posée une fois)."""
    script = SCRIPTS.get(branch, SCRIPTS["AUTRE"])
    left = [step for step in script if details.get(step[0]) in (None, "")]
    if branch == "AUTO" and details.get("vehicle") and not details.get("vehicle_year"):
        left.append(("vehicle_year", "int", "et l'année du véhicule ?", None))
    return left


async def next_question(db, tenant, tu, customer, conversation, branch, note: str | None = None):
    from app.services import shop_cart

    request = await _request(db, tenant, customer, branch)
    details = (request.details or {}) if request is not None else {}
    left = _steps(branch, details)
    if not left:
        return await consent(db, tenant, tu, customer, branch)
    field, kind, question, choices = left[0]
    total = len(SCRIPTS.get(branch, SCRIPTS["AUTRE"]))
    position = total - len([s for s in left if s[0] != "vehicle_year"]) + 1
    lead = (f"{note}\n" if note else "") + (f"Question {position} sur {total} : " if field != "vehicle_year" and total > 1 else "")
    text = lead + (question[0].upper() + question[1:] if not lead.endswith(": ") else question)
    if kind == "btn":
        return _turn([_buttons(text, [(f"{PREFIX}qa:{branch}:{field}:{i}", label[:20]) for i, (label, _) in enumerate(choices)])])
    if kind == "list":
        return _turn([{"type": "list", "body": text, "button": "Choisir",
                       "rows": [(f"{PREFIX}qa:{branch}:{field}:{i}", label[:24], None) for i, (label, _) in enumerate(choices)]}])
    cart = await shop_cart.get_cart(db, tenant, customer, create=True)
    shop_cart._touch(cart, awaiting=f"assu:{branch}:{field}")
    return _turn([{"type": "text", "body": text}])


async def answer(db, tenant, tu, customer, conversation, branch, field, value):
    """Réponse enregistrée dans la demande de cotation (valeur vérifiée par insurance.update_request)."""
    from app.services import insurance

    values = {"branch": branch, field: value}
    if branch == "AUTO" and field == "vehicle" and isinstance(value, str):
        year = YEAR.search(value)
        if year:
            values["vehicle_year"] = int(year.group(1))
            values["vehicle"] = " ".join(YEAR.sub("", value).split()) or value
    if field in ("age", "employees", "vehicles_count", "vehicle_year") and isinstance(value, str):
        digits = re.search(r"\d+", value)
        values[field] = digits.group(0) if digits else value
    result = await insurance.update_request(db, tenant.id, customer.id, conversation.id if conversation else None, values)
    note = None
    if result.get("rejected"):
        note = _you(tu, "Je n'ai pas compris cette réponse.", "Je n'ai pas compris cette réponse.")
    return await next_question(db, tenant, tu, customer, conversation, branch, note=note)


async def consent(db, tenant, tu, customer, branch):
    """Récapitulatif + « Envoyer au cabinet » : l'accord explicite du client (rien ne part sans lui)."""
    from app.services import insurance

    request = await _request(db, tenant, customer, branch)
    if request is None:
        return await welcome(db, tenant, tu)
    if request.status == insurance.STATUS_SUBMITTED:
        return sent(tu)
    lines = [_you(tu, "Votre demande :", "Ta demande :")] + [f"• {line}" for line in insurance.request_lines(request)]
    lines += ["", _you(tu, "Pour vous faire une proposition, le cabinet a besoin de ces informations. Je les lui transmets ?",
                       "Pour te faire une proposition, le cabinet a besoin de ces informations. Je les lui transmets ?")]
    notice = insurance.privacy_notice(tenant)
    if notice:
        lines += ["", notice]
    return _turn([_buttons("\n".join(lines), [(f"{PREFIX}qs:{branch}", "Envoyer au cabinet"), HUMAN])])


def sent(tu):
    body = ("✅ *Demande de devis envoyée*\n"
            + _you(tu, "Un conseiller du cabinet étudie votre demande et vous fait une proposition ici.",
                   "Un conseiller du cabinet étudie ta demande et te fait une proposition ici."))
    return _turn([_buttons(body, [(f"{PREFIX}ap", "Être rappelé"), (f"{PREFIX}menu", "Autre demande")])])


async def submit(db, tenant, tu, customer, conversation, branch):
    from app.services import insurance

    request = await _request(db, tenant, customer, branch)
    if request is None:
        return await welcome(db, tenant, tu)
    result = await insurance.update_request(db, tenant.id, customer.id, conversation.id if conversation else None,
                                            {"branch": branch, "consent": True})
    if result["request"].status != insurance.STATUS_SUBMITTED:  # il manque encore une réponse
        return await next_question(db, tenant, tu, customer, conversation, branch)
    from app.models.conversation import Message, MessageSender

    if result.get("submitted_now"):
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.SYSTEM,
                       message_type="quote_request", content="Demande de cotation transmise au cabinet : "
                       + insurance.branch_label(branch)))
    return sent(tu)


async def contracts(db, tenant, tu, customer, contract_id=None):
    """« Mon contrat » : le registre du cabinet (lot 55), jamais la prime."""
    from app.models.insurance_contract import InsuranceContract
    from app.services.insurance import branch_label
    from app.services.insurance_contracts import ACTIVE

    rows = (await db.execute(select(InsuranceContract).where(
        InsuranceContract.tenant_id == tenant.id, InsuranceContract.customer_id == customer.id,
        InsuranceContract.status == ACTIVE).order_by(InsuranceContract.expires_on))).scalars().all()
    if contract_id is not None:
        rows = [c for c in rows if c.id == contract_id]
    if not rows:
        return _turn([_buttons(_you(tu, "Je ne trouve pas de contrat à votre numéro. Un conseiller peut vérifier avec vous.",
                                    "Je ne trouve pas de contrat à ton numéro. Un conseiller peut vérifier avec toi."),
                               [HUMAN, (f"{PREFIX}menu", "Menu")])])
    if len(rows) > 1:
        return _turn([{"type": "list", "body": _you(tu, "Lequel de vos contrats ?", "Lequel de tes contrats ?"), "button": "Choisir",
                       "rows": [(f"{PREFIX}mc:{c.id}", branch_label(c.branch).replace("Assurance ", "").capitalize()[:24],
                                 f"échéance le {_date(c.expires_on)}") for c in rows[:10]]}])
    contract = rows[0]
    who = ", ".join(p for p in (contract.insurer, f"n° {contract.policy_number}" if contract.policy_number else None) if p)
    label = branch_label(contract.branch).replace("Assurance ", "")
    body = (_you(tu, "Votre contrat", "Ton contrat") + f" *{label}*" + (f" ({who})" if who else "")
            + f" arrive à échéance le *{_date(contract.expires_on)}*.\n"
            + _you(tu, "Que souhaitez-vous faire ?", "Que veux-tu faire ?"))
    return _turn([_buttons(body, [(f"{PREFIX}mr:{contract.id}:R", "Le renouveler"), (f"{PREFIX}mr:{contract.id}:M", "Le modifier"),
                                  HUMAN])])


def _date(day) -> str:
    from app.services.local_time import _MONTHS

    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


async def contract_request(db, tenant, tu, customer, contract_id, what):
    from app.models.insurance_contract import InsuranceContract
    from app.services.insurance import branch_label

    contract = await db.get(InsuranceContract, contract_id)
    if contract is None or contract.tenant_id != tenant.id or contract.customer_id != customer.id:
        return await contracts(db, tenant, tu, customer)
    label = branch_label(contract.branch)
    ref = f" n° {contract.policy_number}" if contract.policy_number else ""
    if what == "R":
        reason = f"Le client souhaite renouveler son contrat {label}{ref} (échéance le {_date(contract.expires_on)})"
        text = _you(tu, "C'est noté : un conseiller prépare votre renouvellement et vous envoie la proposition ici.",
                    "C'est noté : un conseiller prépare ton renouvellement et t'envoie la proposition ici.")
    else:
        reason = f"Le client souhaite modifier son contrat {label}{ref}"
        text = _you(tu, "C'est noté : un conseiller vous contacte ici pour la modification.",
                    "C'est noté : un conseiller te contacte ici pour la modification.")
    return _turn(handoff=True, handoff_reason=reason, handoff_text=text)


async def complaint_start(db, tenant, tu, customer):
    from app.services import shop_cart

    cart = await shop_cart.get_cart(db, tenant, customer, create=True)
    shop_cart._touch(cart, awaiting="assu:complaint")
    return _turn([{"type": "text", "body": _you(
        tu, "Décrivez votre réclamation ou votre sinistre en un message : je l'enregistre tout de suite pour le cabinet.",
        "Décris ta réclamation ou ton sinistre en un message : je l'enregistre tout de suite pour le cabinet.")}])


async def complaint(db, tenant, tu, customer, conversation, text):
    """Registre des réclamations (lot 57) + accusé de réception officiel ; le cabinet est prévenu par e-mail."""
    from datetime import datetime, timezone

    from app.services import insurance_complaints
    from app.services.handoff_service import conversation_link

    record, created = await insurance_complaints.record_from_whatsapp(db, tenant, customer, conversation, text)
    if not created:
        return _turn([_buttons(_you(tu, f"C'est ajouté à votre réclamation {record.reference} : le cabinet la traite.",
                                    f"C'est ajouté à ta réclamation {record.reference} : le cabinet la traite."),
                               [HUMAN, (f"{PREFIX}menu", "Menu")])])
    record.notified_at = datetime.now(timezone.utc)
    subject, body = insurance_complaints.alert_email(tenant, customer, record, conversation_link(conversation))

    def acknowledged():
        record.acknowledged_at = datetime.now(timezone.utc)

    return _turn([{"type": "text", "body": insurance_complaints.acknowledgement(tenant, record),
                   "message_type": "complaint_ack", "after_sent": acknowledged}], alerts=[(subject, body)])


# --- Aiguillage -----------------------------------------------------------------------------------------------------

def _uuid(raw):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def handle(db, tenant, text, reply_id, referral=None, tu=False, customer=None, conversation=None):
    from app.services.guided_flow import wants_welcome

    if reply_id and reply_id.startswith(PREFIX):
        parts = reply_id[len(PREFIX):].split(":")
        turn = await _tap(db, tenant, tu, customer, conversation, parts[0], parts)
        return turn if turn is not None else await welcome(db, tenant, tu)
    if not reply_id and wants_welcome(text, referral):
        return await welcome(db, tenant, tu)
    if not reply_id and customer is not None:
        return await handle_text(db, tenant, tu, customer, conversation, text)
    return None


async def _tap(db, tenant, tu, customer, conversation, action, parts):
    from app.services.insurance import BRANCHES

    if action == "menu":
        return await welcome(db, tenant, tu)
    if action == "human":
        return await auto_flow._tap(db, tenant, tu, customer, conversation, "human", parts)
    if action == "qd":
        return await quote_start(db, tenant, tu)
    if action == "qc" and len(parts) == 2 and parts[1] in ("P", "E"):
        return await branch_list(db, tenant, tu, parts[1])
    if customer is None or conversation is None:
        return None
    if action == "qb" and len(parts) == 2 and parts[1] in BRANCHES:
        if parts[1] != "AUTRE" and parts[1] not in await offered(db, tenant):
            return await quote_start(db, tenant, tu)
        return await next_question(db, tenant, tu, customer, conversation, parts[1])
    if action == "qa" and len(parts) == 4 and parts[1] in SCRIPTS and parts[3].isdigit():
        step = next((s for s in SCRIPTS[parts[1]] if s[0] == parts[2] and s[1] in ("btn", "list")), None)
        if step is None or int(parts[3]) >= len(step[3]):
            return None
        return await answer(db, tenant, tu, customer, conversation, parts[1], parts[2], step[3][int(parts[3])][1])
    if action == "qs" and len(parts) == 2 and parts[1] in BRANCHES:
        return await submit(db, tenant, tu, customer, conversation, parts[1])
    if action == "ap":
        return await auto_flow.slots(db, tenant, tu, customer, "A")
    if action == "sl" and len(parts) == 4 and parts[1] == "A":
        return await auto_flow._tap(db, tenant, tu, customer, conversation, "sl", parts)
    if action == "mc":
        return await contracts(db, tenant, tu, customer, _uuid(parts[1]) if len(parts) == 2 else None)
    if action == "mr" and len(parts) == 3 and _uuid(parts[1]) and parts[2] in ("R", "M"):
        return await contract_request(db, tenant, tu, customer, _uuid(parts[1]), parts[2])
    if action == "rc":
        return await complaint_start(db, tenant, tu, customer)
    return None


async def handle_text(db, tenant, tu, customer, conversation, text):
    from app.services import shop_cart

    cart = await shop_cart.get_cart(db, tenant, customer)
    wanted = shop_cart.awaiting(cart)
    if wanted and wanted.startswith("auto:"):
        return await auto_flow.handle_text(db, tenant, tu, customer, conversation, text)
    if not wanted or not wanted.startswith("assu:"):
        if wanted:
            shop_cart._touch(cart, awaiting=None)
        return None
    clean = " ".join((text or "").split())
    if wanted == "assu:complaint":
        shop_cart._touch(cart, awaiting=None)
        if len(clean) < 3:
            return None
        return await complaint(db, tenant, tu, customer, conversation, clean)  # même une question : c'est sa réclamation
    if not auto_flow._plausible(text):
        shop_cart._touch(cart, awaiting=None)  # une question : l'IA répond (sans jamais de prix), Bob n'attend plus
        return None
    _, branch, field = wanted.split(":")
    shop_cart._touch(cart, awaiting=None)
    return await answer(db, tenant, tu, customer, conversation, branch, field, clean[:300])


async def expects_text(db, tenant, customer, text) -> bool:
    from app.services import shop_cart

    wanted = shop_cart.awaiting(await shop_cart.get_cart(db, tenant, customer))
    if wanted == "assu:complaint":
        return len((text or "").strip()) >= 3
    if wanted and wanted.startswith("assu:"):
        return auto_flow._plausible(text)
    return await auto_flow.expects_text(db, tenant, customer, text)
