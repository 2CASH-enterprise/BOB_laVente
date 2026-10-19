"""
Lot 83 — demandes reçues par email pour les agences immobilières (portails SeLoger, Leboncoin, Bien'ici, Logic-Immo, PAP,
et formulaire du site de l'agence).

- Chaque agence a son adresse : la boîte Gmail dédiée de Bob avec « +code » (bob.contacts+parc-7k2m@gmail.com). L'agence y
  fait suivre ses emails (règle de transfert de sa messagerie) ou la donne directement aux portails.
- La boîte est relue chaque minute, en lecture seule. Lecture par des RÈGLES FIXES (aucune IA) : portail reconnu par son
  adresse, puis nom, email, téléphone, message et référence de l'annonce (rapprochée de la référence du mandat ou de la
  référence du bien). Email mal reconnu : jamais de réponse au hasard, la demande va dans « À traiter ».
- Bob prépare la réponse (un appel à l'IA, comme une réponse WhatsApp), avec les verrous de l'immobilier : pas de pièce
  interdite ni de question discriminatoire (ligne retirée), jamais d'estimation ni de chiffre de crédit (réponse
  remplacée et demande « À traiter »). Lien WhatsApp pré-rempli : c'est le client qui écrit en premier, jamais Bob.
- Par défaut l'agent valide chaque réponse (« Je valide avant envoi ») ; l'agence peut passer en envoi automatique.
- La réponse part au nom de l'agence (adresse de réponse : celle de l'agence). Plafond : celui des conversations de la
  formule, compté à part. Demandes effacées au bout d'un an.
"""
import hashlib
import logging
import re
import secrets
import smtplib
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
from urllib.parse import quote

from sqlalchemy import delete, func, select

logger = logging.getLogger(__name__)

SOURCES = {
    "SELOGER": ("SeLoger", ("seloger.com", "seloger.fr")),
    "LEBONCOIN": ("Leboncoin", ("leboncoin.fr",)),
    "BIENICI": ("Bien'ici", ("bienici.com",)),
    "LOGICIMMO": ("Logic-Immo", ("logic-immo.com",)),
    "PAP": ("PAP", ("pap.fr",)),
    "SITE": ("Site de l'agence", ()),
}
STATUSES = {"DRAFT": "Réponse à valider", "SENT": "Répondu", "TO_HANDLE": "À traiter", "IGNORED": "Écartée",
            "FAILED": "Envoi impossible"}
NOT_AVAILABLE = "Les réponses aux demandes des portails sont disponibles à partir du plan Indépendant."
PLANS = ("INDEPENDANT", "STARTER", "PRO", "ENTERPRISE")
GLOBAL_DAILY_LIMIT = 400  # Gmail : environ 500 envois par jour ; marge pour les tests et les notifications
TENANT_HOURLY_LIMIT = 60
KEEP_DAYS = 365
GMAIL_FORWARDING = "forwarding-noreply@google.com"
SAFE_REPLY = ("Merci pour votre message. Un conseiller de l'agence revient vers vous très vite pour vous répondre "
              "et, si vous le souhaitez, organiser une visite.")

_LABELS = {
    "name": r"(?:nom(?:\s+et\s+pr[ée]nom|\s+complet)?|pr[ée]nom\s+(?:et\s+)?nom|name|full\s*name|contact|de\s+la\s+part\s+de)",
    "first": r"(?:pr[ée]nom|first\s*name)",
    "last": r"(?:nom\s+de\s+famille|last\s*name)",
    "email": r"(?:e-?mail|courriel|adresse\s+(?:e-?mail|[ée]lectronique)|mail)",
    "phone": r"(?:t[ée]l[ée]phone|t[ée]l\.?|portable|mobile|phone|num[ée]ro(?:\s+de\s+t[ée]l[ée]phone)?)",
    "message": r"(?:message|votre\s+message|commentaires?|demande|question|texte)",
    "reference": (r"(?:r[ée]f(?:[ée]rence)?(?:\s+(?:de\s+l'annonce|annonce|du\s+bien|du\s+mandat|mandat))?|"
                  r"(?:n°|num[ée]ro)\s*(?:d'annonce|de\s+l'annonce|de\s+mandat|du\s+mandat))"),
}
_LINE = re.compile(r"^\s*[*•\-]?\s*(?P<label>[^:：\n]{2,40}?)\s*[:：]+\s*(?P<value>.*?)\s*$")
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+33\s?|0033\s?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")
_REFERENCE = re.compile(r"(?i)\br[ée]f(?:[ée]rence)?(?:\s+(?:de\s+l'annonce|annonce|du\s+bien|du\s+mandat|mandat))?\s*"
                        r"(?:n°|no|#)?\s*[:.]?\s*([A-Z0-9][A-Z0-9_\-/]{1,30})")
_NOREPLY = re.compile(r"(no-?reply|ne-?pas-?repondre|nepasrepondre|donotreply|do-not-reply|mailer-daemon|postmaster|"
                      r"notifications?@|bounce)", re.I)
_FORWARD_HEADER = re.compile(r"^\s*(-{2,}.*(forwarded|transf[ée]r[ée]|original).*-{2,}|(de|from|envoy[ée]|sent|date|[àa]|to|"
                             r"objet|subject|cc)\s*:.*)$", re.I)
_AUTO_REPLY_SUBJECT = re.compile(r"^(r[ée]ponse automatique|automatic reply|auto[- ]?reply|out of office|absent|absence)", re.I)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _plan(tenant) -> str:
    return getattr(tenant.plan, "value", tenant.plan) or "FREE"


def available(tenant) -> bool:
    from app.services.business_type import is_real_estate

    return tenant is not None and is_real_estate(tenant) and not tenant.is_demo and _plan(tenant) in PLANS


def configured() -> bool:
    from app.core.config import get_settings

    s = get_settings()
    return bool(s.leads_smtp_username and s.leads_smtp_password and "@" in s.leads_smtp_username)


def label(source: str | None) -> str:
    return SOURCES.get(source or "SITE", SOURCES["SITE"])[0]


def _plain(text: str | None) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()


async def ensure_code(db, tenant) -> str:
    """Code de l'adresse de l'agence (« parc-7k2m »), créé une fois ; jamais deux fois le même."""
    from app.models.tenant import Tenant

    if tenant.lead_inbox_code:
        return tenant.lead_inbox_code
    base = re.sub(r"[^a-z0-9]+", "-", _plain(tenant.name)).strip("-")[:20] or "agence"
    for _ in range(10):
        code = f"{base}-{secrets.token_hex(2)}"
        if (await db.execute(select(Tenant.id).where(Tenant.lead_inbox_code == code))).first() is None:
            tenant.lead_inbox_code = code
            await db.commit()
            return code
    raise RuntimeError("Code d'adresse introuvable")


def address_for(code: str | None) -> str | None:
    from app.core.config import get_settings

    user = get_settings().leads_smtp_username
    if not code or "@" not in user:
        return None
    local, domain = user.split("@", 1)
    return f"{local}+{code}@{domain}"


def test_token(code: str) -> str:
    from app.core.config import get_settings

    return hashlib.sha256(f"{get_settings().secret_key}:lead-test:{code}".encode()).hexdigest()[:20]


# --- Lecture d'un email -------------------------------------------------------------------------------------------

def body_text(message) -> str:
    """Texte de l'email, lignes gardées (le HTML est converti : cellules de tableau → « libellé : valeur »)."""
    part = message.get_body(preferencelist=("plain", "html")) if message.is_multipart() else message
    try:
        content = part.get_content() if part is not None else ""
    except Exception:  # noqa: BLE001 — email abîmé
        content = ""
    if part is not None and part.get_content_type() == "text/html":
        content = re.sub(r"(?is)<(script|style).*?</\1>", " ", content)
        content = re.sub(r"(?i)</t[dh]>\s*<t[dh][^>]*>", " : ", content)
        content = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>|</li>|</h\d>", "\n", content)
        content = re.sub(r"<[^>]+>", " ", content)
        import html as _html

        content = _html.unescape(content)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in str(content).splitlines()]
    return "\n".join(lines).strip()


def recipients(message) -> list[str]:
    values = []
    for header in ("Delivered-To", "X-Original-To", "X-Forwarded-To", "Envelope-To", "To", "Cc"):
        values += [str(v) for v in (message.get_all(header) or [])]
    found = [addr.lower() for _, addr in getaddresses(values) if addr]
    for received in message.get_all("Received") or []:
        found += [a.lower() for a in re.findall(r"for <([^>]+)>", str(received))]
    return found


def code_of(message) -> str | None:
    """Code de l'agence dans l'adresse de réception (« +code »), seulement sur NOTRE adresse."""
    from app.core.config import get_settings

    user = get_settings().leads_smtp_username.lower()
    if "@" not in user:
        return None
    local, domain = user.split("@", 1)
    pattern = re.compile(rf"^{re.escape(local)}\+([a-z0-9-]{{4,32}})@{re.escape(domain)}$")
    for addr in recipients(message):
        match = pattern.match(addr)
        if match:
            return match.group(1)
    return None


def gmail_confirmation(message, text: str) -> str | None:
    """Code du transfert Gmail (« Gmail Forwarding Confirmation »), affiché à l'agence pour qu'elle le recopie."""
    if GMAIL_FORWARDING not in (message.get("From") or "").lower():
        return None
    match = re.search(r"(?i)(?:confirmation code|code de confirmation)\s*:\s*(\d{6,12})", text) or \
        re.search(r"\(#(\d{6,12})\)", message.get("Subject") or "")
    return match.group(1) if match else None


def source_of(message, text: str) -> str:
    sender = (message.get("From") or "").lower() + " " + (message.get("Reply-To") or "").lower()
    for code, (_, domains) in SOURCES.items():
        if any(d in sender for d in domains):
            return code
    head = text[:4000].lower()
    for code, (_, domains) in SOURCES.items():
        if any(d in head for d in domains):
            return code
    return "SITE"


def _labelled(text: str) -> dict:
    """{champ: valeur} des lignes « Libellé : valeur » ; le message peut continuer sur les lignes suivantes."""
    out: dict = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = _LINE.match(line)
        if not match:
            continue
        label_text, value = _plain(match.group("label")).strip(), match.group("value").strip()
        for field, pattern in _LABELS.items():
            if field in out or not re.fullmatch(pattern, label_text):
                continue
            if field == "message":
                more = []
                for nxt in lines[i + 1:i + 30]:
                    if not nxt.strip() and (more or value):
                        break
                    if _LINE.match(nxt) and any(re.fullmatch(p, _plain(_LINE.match(nxt).group("label")).strip())
                                                for p in _LABELS.values()):
                        break
                    more.append(nxt)
                value = "\n".join([value] + more).strip()
            if value:
                out[field] = value
            break
    return out


def _excluded(addr: str, tenant) -> bool:
    from app.core.config import get_settings

    addr = addr.lower()
    ours = get_settings().leads_smtp_username.lower()
    own_domain = ours.split("@", 1)[1] if "@" in ours else ""
    if _NOREPLY.search(addr) or addr == (tenant.email or "").lower():
        return True
    if ours and (addr == ours or (addr.startswith(ours.split("@")[0] + "+") and addr.endswith("@" + own_domain))):
        return True
    portal_domains = [d for _, (_, ds) in SOURCES.items() for d in ds]
    # Une adresse d'un portail n'est gardée que si c'est une adresse relais de messagerie (Leboncoin cache parfois l'email).
    return any(addr.endswith("@" + d) or addr.endswith("." + d) for d in portal_domains) and "relais" not in addr \
        and "messagerie" not in addr


def _clean_phone(value: str | None) -> str | None:
    if not value:
        return None
    match = _PHONE.search(value)
    return " ".join(match.group(0).split()) if match else None


def parse(message, tenant) -> dict:
    """Ce que Bob lit d'une demande : source, nom, email, téléphone, message, référence. Champs absents : None."""
    text = body_text(message)
    fields = _labelled(text)
    out = {"source": source_of(message, text), "subject": (message.get("Subject") or "")[:255] or None}
    email = None
    labelled = _EMAIL.search(fields.get("email") or "")
    candidates = ([labelled.group(0)] if labelled else []) + \
        [addr for _, addr in getaddresses([str(message.get("Reply-To") or "")]) if addr] + _EMAIL.findall(text)
    for addr in candidates:
        if not _excluded(addr, tenant):
            email = addr.strip(".").lower()
            break
    out["email"] = email
    name = fields.get("name")
    if name and fields.get("first") and fields["first"].lower() not in name.lower():
        name = f"{fields['first']} {name}"  # formulaires « Prénom » + « Nom »
    if not name and (fields.get("first") or fields.get("last")):
        name = " ".join(v for v in (fields.get("first"), fields.get("last")) if v)
    if not name:
        display, addr = parseaddr(str(message.get("Reply-To") or ""))
        if addr and addr.lower() == email and display and not any(d[0].lower() in display.lower() for d in SOURCES.values()):
            name = display
    out["name"] = " ".join(str(name).split())[:200] if name and not _EMAIL.fullmatch(str(name).strip()) else None
    own = re.sub(r"\D", "", tenant.phone or "")[-9:]

    def theirs(number: str | None) -> bool:  # jamais le numéro de l'agence
        return bool(number) and (not own or re.sub(r"\D", "", number)[-9:] != own)

    phone = _clean_phone(fields.get("phone"))
    if not theirs(phone):
        phone = next((p for p in (" ".join(m.group(0).split()) for m in _PHONE.finditer(text)) if theirs(p)), None)
    out["phone"] = phone
    message_text = fields.get("message")
    if not message_text:
        kept = []
        for line in text.splitlines():
            if _FORWARD_HEADER.match(line) or (_LINE.match(line) and any(
                    re.fullmatch(p, _plain(_LINE.match(line).group("label")).strip()) for p in _LABELS.values())):
                continue
            kept.append(line)
        message_text = "\n".join(kept).strip()
    out["message"] = re.sub(r"\n{3,}", "\n\n", message_text)[:2000] or None
    reference = fields.get("reference")
    if reference:
        reference = reference.split()[0].strip(".,;")
    else:
        match = _REFERENCE.search(text) or _REFERENCE.search(out["subject"] or "")
        reference = match.group(1) if match else None
    out["reference"] = reference[:64] if reference else None
    return out


async def match_product(db, tenant_id, reference: str | None, haystack: str):
    """Le bien de la demande : référence du mandat ou référence du bien, sinon son nom cité dans l'email."""
    from app.models.product import Product

    products = (await db.execute(select(Product).where(Product.tenant_id == tenant_id))).scalars().all()
    if reference:
        ref = reference.strip().lower()
        for product in products:
            mandate = str((product.sheet or {}).get("reference") or "").strip().lower()
            if ref and (ref == mandate or ref == (product.sku or "").strip().lower()):
                return product
    plain = _plain(haystack)
    named = [p for p in products if len(p.name or "") >= 8 and _plain(p.name) in plain]
    return max(named, key=lambda p: len(p.name)) if named else None


# --- Réponse de Bob -----------------------------------------------------------------------------------------------

def facts(product) -> str:
    from decimal import Decimal

    from app.services.real_estate import card_detail

    if product is None:
        return "Aucun bien précis n'a été identifié dans la demande."
    sheet = product.sheet or {}
    lines = [f"Bien : {product.name}"]
    detail = card_detail(product)
    if detail:
        lines.append(f"Résumé : {detail}")
    rental = sheet.get("transaction") == "Location"
    price = f"{Decimal(str(product.price)):,.0f} {product.currency}".replace(",", " ")
    lines.append(f"{'Loyer mensuel charges comprises' if rental else 'Prix honoraires inclus'} : {price}")
    for key, name in (("fees", "Honoraires"), ("charges", "Charges"), ("deposit", "Dépôt de garantie"),
                      ("floor", "Étage"), ("furnished", "Meublé / vide"), ("available_from", "Disponibilité"),
                      ("district", "Quartier"), ("ges", "GES"), ("bedrooms", "Chambres")):
        if sheet.get(key):
            lines.append(f"{name} : {sheet[key]}")
    if product.description:
        lines.append(f"Description : {product.description[:700]}")
    lines.append("Disponible : " + ("oui" if product.active and (product.stock_quantity or 0) > 0 else
                                    "NON (déjà loué ou vendu)"))
    return "\n".join(lines)


def system_prompt(tenant) -> str:
    from app.agents.prompts import _agency_identity

    return f"""Tu es Bob, l'assistant de {tenant.name}, {_agency_identity(tenant)}.
Tu réponds PAR EMAIL à une personne qui vient de contacter l'agence (depuis un portail d'annonces ou le site de l'agence).

RÈGLES
- Vouvoiement, ton chaleureux et professionnel, 120 mots au plus, en paragraphes courts.
- Écris seulement le corps du message : PAS de « Bonjour », PAS de signature, PAS de lien (ils sont ajoutés ensuite).
- Réponds d'abord à sa question, avec UNIQUEMENT les informations de la FICHE ci-dessous. N'invente jamais un bien, un
  prix, une disponibilité ni une caractéristique. Si l'information manque, dis qu'un conseiller la lui précisera.
- Si le bien n'est plus disponible, dis-le simplement et propose de lui envoyer des biens proches.
- Pose 1 ou 2 questions pour préparer la suite : location → date d'emménagement souhaitée et dossier (justificatifs de
  revenus, garant) ; achat → délai du projet et financement (déjà étudié ou non, sans aucun chiffre) ; vente → adresse et
  délai. Propose une visite en demandant ses disponibilités (ne fixe jamais toi-même un jour ni une heure).
- Jamais d'estimation de bien, de taux, de mensualité ni de capacité d'emprunt. Jamais de remise sur les honoraires.
- Ne demande jamais de relevé bancaire, de carte Vitale, de photo d'identité, de casier judiciaire, d'attestation de
  bonne tenue de compte, ni rien sur la nationalité, l'origine, la religion, la santé, la situation familiale.
- Ne promets jamais qu'un dossier sera accepté."""


def _first_name(name: str | None) -> str | None:
    if not name:
        return None
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    titles = {"m", "m.", "mr", "mme", "mlle", "madame", "monsieur"}
    if parts and parts[0].lower() in titles and len(parts) > 1:
        return f"{'Madame' if parts[0].lower().startswith(('mme', 'madame', 'mlle')) else 'Monsieur'} {parts[-1]}"
    return parts[0] if parts else None


def _clean_body(text: str) -> str:
    from app.services.reply_cleanup import clean_ai_reply

    text = clean_ai_reply(text or "")
    lines = [line for line in text.strip().splitlines() if not re.search(r"https?://|www\.", line)]
    while lines and re.match(r"(?i)^\s*(bonjour|madame|monsieur|cher|ch[èe]re)\b.{0,60},?\s*$", lines[0]):
        lines.pop(0)
    while lines and re.match(r"(?i)^\s*(cordialement|bien (à vous|cordialement)|l'agence|l'équipe|bob)\b", lines[-1]):
        lines.pop()
    return "\n".join(lines).strip()


async def wa_digits(db, tenant_id) -> str | None:
    from app.models.whatsapp_account import WhatsAppAccount

    account = (await db.execute(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))).scalars().first()
    if account is None or not account.display_phone_number:
        return None
    return "".join(ch for ch in account.display_phone_number if ch.isdigit()) or None


def _money(value, currency: str) -> str:
    from decimal import Decimal

    amount = f"{Decimal(str(value)):,.0f}".replace(",", "\u202f")
    return f"{amount} €" if (currency or "").upper() == "EUR" else f"{amount} {currency}"


def compose(tenant, lead, product, body: str, digits: str | None) -> tuple[str, str]:
    """
    Texte de la réponse (version de secours, et ce que l'agent relit et corrige). Mise en page de l'email (lot 40) :
    « Libellé : valeur » sur plusieurs lignes → petit tableau du bien ; « Écrire sur WhatsApp : lien » → bouton.
    """
    from app.services.email_layout import customer_footer
    from app.services.real_estate import card_detail

    subject = (f"Votre demande — {product.name}" if product else f"Votre demande à {tenant.name}")[:255]
    hello = f"Bonjour {_first_name(lead.contact_name)}," if _first_name(lead.contact_name) else "Bonjour,"
    lines = [hello, "", body.strip()]
    if product is not None:
        sheet = product.sheet or {}
        rental = sheet.get("transaction") == "Location"
        rows = [f"Bien : {product.name}"]
        detail = card_detail(product)
        if detail:
            rows.append(f"En bref : {detail}")
        rows.append(f"{'Loyer charges comprises' if rental else 'Prix honoraires inclus'} : "
                    f"{_money(product.price, product.currency)}{' / mois' if rental else ''}")
        if sheet.get("fees") and not rental:
            rows.append(f"Honoraires : {sheet['fees']}")
        if lead.reference:
            rows.append(f"Référence : {lead.reference}")
        lines += ["", "\n".join(rows)]
    if digits:
        about = f"Bonjour, je vous écris au sujet de {product.name}" if product else "Bonjour, je vous écris suite à ma demande"
        if lead.reference:
            about += f" (réf. {lead.reference})"
        lines += ["", "Pour aller plus vite, continuons sur WhatsApp :", "",
                  f"Écrire sur WhatsApp : https://wa.me/{digits}?text={quote(about)}"]
    signature = [f"L'équipe {tenant.name}"]
    if tenant.phone:
        signature.append(f"Tél. {tenant.phone}")
    lines += ["", "\n".join(signature)]
    why = f"Cet email vous est envoyé par {tenant.name} en réponse à votre demande ({label(lead.source)})."
    return subject, "\n".join(lines) + customer_footer(tenant.name, not tenant.is_paid, [why])


def html_of(tenant, lead, product) -> str:
    """Version mise en page (en-tête au nom de l'agence, photo du bien, tableau, bouton WhatsApp)."""
    from html import escape

    from app.services.email_layout import render_html

    page = render_html(lead.reply_subject or "", lead.reply_text or "", tenant.name, True)
    image = getattr(product, "image_url", None) if product is not None else None
    if image and str(image).startswith("https://"):
        photo = (f'<img src="{escape(image, quote=True)}" alt="{escape(product.name, quote=True)}" width="496" '
                 'style="display: block; width: 100%; max-width: 496px; height: auto; border: 0; border-radius: 12px; '
                 'margin: 0 0 18px;">')
        page = page.replace("</h1>", "</h1>\n" + photo, 1)
    return page


async def draft_reply(db, tenant, lead, product, llm_client) -> tuple[str, str | None]:
    """(corps de la réponse, motif « À traiter » ou None). Ne lève jamais d'exception."""
    from app.agents.orchestrator import record_usage
    from app.services.real_estate import contains_valuation_or_loan_figure, legal_guard

    if llm_client is None:
        return SAFE_REPLY, "Service d'IA non configuré : réponse simple préparée, à compléter."
    user = (f"FICHE\n{facts(product)}\n\nDEMANDE reçue ({label(lead.source)})\n"
            f"Nom : {lead.contact_name or 'non indiqué'}\nMessage :\n{lead.message or '(pas de message, simple demande de contact)'}")
    try:
        response = await llm_client.create_message(system=system_prompt(tenant), messages=[{"role": "user", "content": user}],
                                                   tools=[], max_tokens=600)
    except Exception:  # noqa: BLE001 — panne de l'IA : réponse simple, l'agent complète
        logger.warning("Réponse à une demande par email non préparée (IA indisponible)")
        return SAFE_REPLY, "Service d'IA indisponible : réponse simple préparée, à compléter."
    record_usage(db, tenant.id, None, "LEAD_EMAIL", getattr(llm_client, "model", None), response)
    text = "\n".join(b.get("text", "") for b in (response or {}).get("content", []) if b.get("type") == "text")
    body = _clean_body(text)
    if not body:
        return SAFE_REPLY, "Réponse de l'IA vide : réponse simple préparée, à compléter."
    body, removed = legal_guard(body)
    if contains_valuation_or_loan_figure(body):
        return SAFE_REPLY, "La réponse de Bob contenait une estimation ou un chiffre de crédit : remplacée."
    reason = f"Ligne retirée de la réponse : {', '.join(dict.fromkeys(removed))}." if removed else None
    return body, reason


# --- Envoi --------------------------------------------------------------------------------------------------------

def smtp_send(msg: EmailMessage) -> None:
    from app.core.config import get_settings

    s = get_settings()
    with smtplib.SMTP(s.prospect_smtp_host, s.prospect_smtp_port, timeout=20) as server:
        server.starttls()
        server.login(s.leads_smtp_username, s.leads_smtp_password)
        server.send_message(msg)


async def send(db, lead, tenant, sent_by: str, transport=None) -> tuple[bool, str | None]:
    """Envoie la réponse préparée ; (envoyée, motif sinon)."""
    from app.core.config import get_settings
    from app.models.inbound_lead import InboundLead

    if not configured():
        return False, "La boîte des demandes n'est pas encore réglée sur le serveur."
    if not lead.contact_email or not lead.reply_text:
        return False, "Pas d'adresse email du client, ou réponse vide."
    since = now_utc() - timedelta(hours=24)
    sent_day = (await db.execute(select(func.count()).select_from(InboundLead).where(
        InboundLead.status == "SENT", InboundLead.replied_at >= since))).scalar_one()
    sent_hour = (await db.execute(select(func.count()).select_from(InboundLead).where(
        InboundLead.tenant_id == tenant.id, InboundLead.status == "SENT",
        InboundLead.replied_at >= now_utc() - timedelta(hours=1)))).scalar_one()
    if sent_day >= GLOBAL_DAILY_LIMIT or sent_hour >= TENANT_HOURLY_LIMIT:
        return False, "Trop d'envois en peu de temps : réessayez dans une heure."
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = formataddr((tenant.name[:80], s.leads_smtp_username))
    msg["To"] = lead.contact_email
    msg["Reply-To"] = tenant.email
    msg["Subject"] = lead.reply_subject or f"Votre demande à {tenant.name}"
    msg["Message-ID"] = make_msgid(domain=s.leads_smtp_username.split("@", 1)[1])
    msg.set_content(lead.reply_text)
    from app.models.product import Product

    product = await db.get(Product, lead.product_id) if lead.product_id else None
    msg.add_alternative(html_of(tenant, lead, product), subtype="html")
    try:
        (transport or smtp_send)(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Réponse à une demande non envoyée : %s", type(exc).__name__)
        lead.status, lead.reason = "FAILED", f"Envoi impossible ({type(exc).__name__}) : réessayez."
        await db.commit()
        return False, lead.reason
    lead.status, lead.replied_at, lead.sent_by = "SENT", now_utc(), sent_by[:255]
    await db.commit()
    return True, None


# --- Boîte de réception -------------------------------------------------------------------------------------------

def imap_fetch(last_uid: int) -> list[tuple[int, bytes]]:
    """Messages arrivés depuis le dernier lu, en lecture seule."""
    import imaplib

    from app.core.config import get_settings

    s = get_settings()
    out = []
    with imaplib.IMAP4_SSL(s.prospect_imap_host) as box:
        box.login(s.leads_smtp_username, s.leads_smtp_password)
        box.select("INBOX", readonly=True)
        _, data = box.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid][:200]
        for uid in uids:
            _, parts = box.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append((uid, raw))
    return out


def _is_bounce_or_auto_reply(message) -> bool:
    """Les emails des portails sont automatiques (« auto-generated ») : seuls les retours d'erreur et les réponses
    d'absence sont écartés."""
    sender = (message.get("From") or "").lower()
    auto = (message.get("Auto-Submitted") or "").lower()
    return ("mailer-daemon" in sender or "postmaster" in sender or message.get_content_type() == "multipart/report"
            or auto == "auto-replied" or bool(message.get("X-Autoreply"))
            or bool(_AUTO_REPLY_SUBJECT.search((message.get("Subject") or "").strip())))


async def _state(db):
    from app.models.inbound_lead import LeadInboxState

    row = await db.get(LeadInboxState, 1)
    if row is None:
        row = LeadInboxState(id=1, last_uid=0)
        db.add(row)
        await db.flush()
    return row


async def _month_count(db, tenant_id) -> int:
    from app.models.inbound_lead import InboundLead

    start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (await db.execute(select(func.count()).select_from(InboundLead).where(
        InboundLead.tenant_id == tenant_id, InboundLead.is_test.is_(False), InboundLead.received_at >= start))).scalar_one()


async def quota(db, tenant) -> int | None:
    from app.services import plan_quota

    return (await plan_quota.settings(db))["quotas"].get(_plan(tenant))


async def _link_customer(db, tenant, lead, product):
    """Client retrouvé (ou créé) par son numéro ; la demande est notée sur sa fiche. Sans numéro : aucun client créé."""
    from app.models.customer import Customer
    from app.services.insurance_contracts import normalize_phone

    number = normalize_phone(lead.contact_phone, tenant.country) if lead.contact_phone else None
    if not number:
        return None
    customer = (await db.execute(select(Customer).where(Customer.tenant_id == tenant.id,
                                                        Customer.whatsapp_number == number))).scalars().first()
    moment = now_utc()
    if customer is None:
        customer = Customer(tenant_id=tenant.id, whatsapp_number=number, country=tenant.country,
                            acquisition_source="PORTAL",
                            acquisition_detail=(label(lead.source) + (f" · {product.name}" if product else ""))[:255])
        if lead.contact_name:
            parts = lead.contact_name.split()
            customer.first_name = parts[0][:255]
            customer.last_name = " ".join(parts[1:])[:255] or None
        db.add(customer)
    if lead.contact_email and not customer.email:
        customer.email, customer.email_source, customer.email_collected_at = lead.contact_email, "PORTAL", moment
    note = f"[{moment.strftime('%d/%m/%Y')} · demande {label(lead.source)}{' · ' + product.name if product else ''}] " \
           f"{(lead.message or '').strip()[:300]}"
    customer.notes = f"{customer.notes}\n{note}".strip() if customer.notes else note
    await db.flush()
    return customer


def alert(tenant, lead, product) -> tuple[str, str]:
    from app.core.config import get_settings

    link = get_settings().public_base_url.rstrip("/") + "/dashboard/?tab=leads"
    who = lead.contact_name or lead.contact_email or "Un client"
    what = f" pour {product.name}" if product else ""
    if lead.status == "SENT":
        subject = f"Bob a répondu à {who} ({label(lead.source)})"
        head = f"Bob a répondu par email à {who}{what}, quelques instants après sa demande."
    elif lead.status == "DRAFT":
        subject = f"Réponse prête à valider : {who} ({label(lead.source)})"
        head = f"Nouvelle demande de {who}{what}. Bob a préparé la réponse : vérifiez-la et envoyez-la d'un clic."
    else:
        subject = f"Demande à traiter : {who} ({label(lead.source)})"
        head = f"Nouvelle demande de {who}{what}. {lead.reason or 'Bob ne peut pas y répondre seul.'}"
    body = f"{head}\n\nMessage : {(lead.message or '—')[:600]}\n\nVoir la demande : {link}"
    return subject, body


async def handle(db, tenant, lead, product, llm_client, notify=None, transport=None) -> None:
    """Prépare la réponse, l'envoie si l'agence a choisi l'envoi automatique, prévient l'agence."""
    if lead.status != "TO_HANDLE":
        body, reason = await draft_reply(db, tenant, lead, product, llm_client)
        lead.reply_subject, lead.reply_text = compose(tenant, lead, product, body, await wa_digits(db, tenant.id))
        if reason and body == SAFE_REPLY:
            lead.status, lead.reason = "TO_HANDLE", reason
        else:
            lead.status, lead.reason = "DRAFT", reason
        await db.commit()
        if lead.status == "DRAFT" and tenant.lead_auto_send:
            await send(db, lead, tenant, "BOB", transport=transport)
    if notify is not False and tenant.email:
        subject, body = alert(tenant, lead, product)
        try:
            await (notify or _notify)(tenant.email, subject, body)
        except Exception:  # noqa: BLE001 — la demande reste visible dans le tableau de bord
            logger.warning("Alerte de demande non envoyée à l'agence")


async def _notify(to: str, subject: str, body: str) -> None:
    import asyncio

    from app.services.email_service import send_email

    await asyncio.to_thread(send_email, to=to, subject=subject, body=body)


async def process_inbox(db, fetch=None, llm_client=None, notify=None, transport=None) -> dict:
    """Lit les nouveaux emails de la boîte des demandes et les traite un par un."""
    from email import message_from_bytes
    from email.policy import default

    from app.models.inbound_lead import InboundLead
    from app.models.tenant import Tenant

    report = {"leads": 0, "to_handle": 0, "gmail_codes": 0, "ignored": 0}
    if not configured():
        return report
    state = await _state(db)
    await db.commit()
    try:
        messages = (fetch or imap_fetch)(state.last_uid)
    except Exception as exc:  # noqa: BLE001
        state.last_error, state.last_error_at = f"Lecture de la boîte impossible : {type(exc).__name__}", now_utc()
        await db.commit()
        logger.warning("Boîte des demandes illisible : %s", type(exc).__name__)
        return report
    if llm_client is None:
        from app.agents.dependency import get_llm_client

        llm_client = get_llm_client()
    for uid, raw in messages:
        state.last_uid = max(state.last_uid, uid)
        state.last_run_at = now_utc()
        await db.commit()
        message = message_from_bytes(raw, policy=default)
        code = code_of(message)
        tenant = (await db.execute(select(Tenant).where(Tenant.lead_inbox_code == code))).scalar_one_or_none() if code else None
        if tenant is None or _is_bounce_or_auto_reply(message):
            report["ignored"] += 1
            continue
        text = body_text(message)
        gmail_code = gmail_confirmation(message, text)
        if gmail_code:
            tenant.lead_gmail_code, tenant.lead_gmail_code_at = gmail_code, now_utc()
            await db.commit()
            report["gmail_codes"] += 1
            continue
        from app.core.config import get_settings

        is_test = (message.get("X-Bob-Test") or "") == test_token(code)
        if parseaddr(message.get("From") or "")[1].lower() == get_settings().leads_smtp_username.lower() and not is_test:
            report["ignored"] += 1  # jamais nos propres envois (boucle)
            continue
        if not available(tenant):
            report["ignored"] += 1
            continue
        message_id = (message.get("Message-ID") or f"<uid-{uid}@lead>").strip()[:255]
        if (await db.execute(select(InboundLead.id).where(InboundLead.tenant_id == tenant.id,
                                                          InboundLead.message_id == message_id))).first():
            report["ignored"] += 1
            continue
        data = parse(message, tenant)
        if is_test and not data["email"]:
            data["email"] = (tenant.email or "").lower() or None  # le test répond à l'agence elle-même
        product = await match_product(db, tenant.id, data["reference"], f"{data['subject'] or ''}\n{text}")
        lead = InboundLead(tenant_id=tenant.id, message_id=message_id, source=data["source"], subject=data["subject"],
                           contact_name=data["name"], contact_email=data["email"], contact_phone=data["phone"],
                           message=data["message"], reference=data["reference"],
                           product_id=product.id if product else None, is_test=is_test, status="DRAFT",
                           received_at=now_utc())
        if not data["email"]:
            lead.status, lead.reason = "TO_HANDLE", "Adresse email du client introuvable dans l'email : à traiter à la main."
        elif not is_test:
            limit = await quota(db, tenant)
            if limit is not None and await _month_count(db, tenant.id) >= limit:
                lead.status, lead.reason = "TO_HANDLE", "Plafond de demandes du mois atteint pour votre formule."
        db.add(lead)
        await db.flush()
        if not is_test:
            customer = await _link_customer(db, tenant, lead, product)
            lead.customer_id = customer.id if customer else None
        await db.commit()
        await handle(db, tenant, lead, product, llm_client, notify=notify, transport=transport)
        report["leads"] += 1
        report["to_handle"] += lead.status == "TO_HANDLE"
    await db.commit()
    return report


def test_email(tenant, product) -> EmailMessage:
    """Faux contact « SeLoger » envoyé à l'adresse de l'agence, pour vérifier tout le parcours (réponse à l'agence)."""
    from app.core.config import get_settings

    s = get_settings()
    reference = (product.sheet or {}).get("reference") or product.sku if product else "TEST"
    msg = EmailMessage()
    msg["From"] = formataddr(("SeLoger (test Bob)", s.leads_smtp_username))
    msg["To"] = address_for(tenant.lead_inbox_code)
    msg["Subject"] = f"Nouveau contact pour votre annonce réf. {reference}"
    msg["X-Bob-Test"] = test_token(tenant.lead_inbox_code)
    msg["Message-ID"] = make_msgid(domain=s.leads_smtp_username.split("@", 1)[1])
    msg.set_content("\n".join([
        "Nouveau contact pour votre annonce (seloger.com)",
        f"Référence de l'annonce : {reference}",
        "Nom : Claire Martin (test)",
        f"Email : {tenant.email}",
        "Message : Bonjour, ce bien est-il toujours disponible ? Je souhaiterais le visiter rapidement.",
    ]))
    return msg


async def purge(db, now: datetime | None = None) -> int:
    from app.models.inbound_lead import InboundLead

    limit = (now or now_utc()) - timedelta(days=KEEP_DAYS)
    result = await db.execute(delete(InboundLead).where(InboundLead.received_at < limit))
    await db.commit()
    return result.rowcount or 0


def response_seconds(leads) -> int | None:
    """Délai médian entre la réception et la réponse envoyée."""
    from statistics import median

    delays = []
    for lead in leads:
        if lead.status == "SENT" and lead.replied_at and lead.received_at and not lead.is_test:
            a = lead.replied_at if lead.replied_at.tzinfo else lead.replied_at.replace(tzinfo=timezone.utc)
            b = lead.received_at if lead.received_at.tzinfo else lead.received_at.replace(tzinfo=timezone.utc)
            delays.append(max(0, (a - b).total_seconds()))
    return round(median(delays)) if delays else None
