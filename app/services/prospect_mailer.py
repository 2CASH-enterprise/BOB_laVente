"""
Lot 62 — emails de prospection d'AgenC'AI, envoyés par petits lots depuis le compte Gmail de prospection
(choix du 10/10 : 1A 2A 3A 4A).

- Expéditeur affiché « Bob de AgenC'AI » ; réponses vers la même boîte (Bob les y lit), copie à l'adresse
  habituelle (PROSPECT_COPY_TO). Identifiants seulement dans le .env (mot de passe d'APPLICATION Gmail).
- Campagne : prospects jamais contactés d'un secteur / d'une source / d'un pays ; 3 emails au plus (1er jour,
  4e jour, 10e jour) ; tout s'arrête au clic, à l'inscription, à la réponse, à la désinscription.
- Envoi : un email à la fois (tâche toutes les 3 minutes), en semaine de 9 h à 17 h à l'heure du prospect, sous
  le plafond des dernières 24 heures (20 au départ ; la page recommande quand monter, l'humain décide).
- Suivi : ouverture (image de 1 pixel, indicative), clic (lien personnel du lot 61), réponse et adresse
  inexistante (lecture de la boîte en lecture seule toutes les 15 minutes), désinscription en un clic
  (lien + en-têtes List-Unsubscribe) : une adresse désinscrite ou inexistante n'est plus jamais contactée.
"""
import hashlib
import logging
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.services import prospection as pros

logger = logging.getLogger(__name__)

STEP_DELAYS = {1: timedelta(days=4), 2: timedelta(days=6)}  # 2e email au 4e jour, 3e au 10e jour
SEND_HOURS = (9, 17)
LIMITS = [20, 40, 80, 120, 200]
FAILURES_BEFORE_INVALID = 3
COMPANY = "AgenC'AI"
LEGAL = ("Bob AI est une solution développée par SAS 2 Cash Enterprise. © {year} SAS 2 Cash Enterprise (Holding) — "
         "1 Rue Hartz Huel, 22500 Kerfot, France. Exploitation par : " + COMPANY + ".")
SIGNATURE = "Bob, le vendeur virtuel d'" + COMPANY
STOP_WORDS = re.compile(r"\b(stop|d[ée]sinscri\w*|d[ée]sabonn\w*|ne plus (me )?(recevoir|contacter|[ée]crire)|retirez|unsubscribe)\b", re.I)
AUTO_SUBJECTS = re.compile(r"^(r[ée]ponse automatique|automatic reply|auto[- ]?reply|out of office|absent|absence)", re.I)

DEFAULTS = {
    "ONLINE_STORE": [
        ("{entreprise} : vos clients WhatsApp servis 24 h / 24",
         "{bonjour},\n\nVos clients vous écrivent sur WhatsApp le soir, le week-end, pendant que vous servez au comptoir… "
         "et certains repartent sans réponse.\n\nBob est un vendeur virtuel qui répond à leur place, tout de suite : il "
         "présente vos produits, donne les prix, prend la commande et relance ceux qui hésitent. Vous gardez la main à "
         "tout moment.\n\nVous pouvez l'essayer en 2 minutes avec votre propre catalogue :\n{lien}\n\nBonne journée,"),
        ("Re : {entreprise} et WhatsApp",
         "{bonjour},\n\nJe me permets de revenir vers vous : la plupart des commerçants que nous accompagnons reçoivent un "
         "tiers de leurs messages en dehors des heures d'ouverture.\n\nBob y répond pour vous, sans délai. L'essai prend "
         "2 minutes :\n{lien}\n\nBonne journée,"),
        ("Dernier message de ma part",
         "{bonjour},\n\nC'est mon dernier message : si ce n'est pas le bon moment, aucun souci.\n\nSi vous voulez voir "
         "Bob répondre à vos clients, la démo reste ouverte :\n{lien}\n\nBelle continuation,"),
    ],
    "CAR_DEALERSHIP": [
        ("{entreprise} : plus de rendez-vous d'essai depuis WhatsApp",
         "{bonjour},\n\nVos prospects vous écrivent sur WhatsApp pour un véhicule, souvent le soir ou le week-end. Bob, "
         "notre vendeur virtuel, leur répond tout de suite, qualifie leur projet (budget, financement, reprise) et leur "
         "propose un rendez-vous d'essai dans vos créneaux.\n\nVous pouvez l'essayer en 2 minutes :\n{lien}\n\nBonne journée,"),
        ("Re : rendez-vous d'essai pour {entreprise}",
         "{bonjour},\n\nJe reviens vers vous : un prospect qui attend une réponse le samedi soir a souvent écrit à un "
         "autre vendeur le lundi.\n\nBob répond à votre place et vous transmet un prospect qualifié, prêt pour l'essai :\n"
         "{lien}\n\nBonne journée,"),
        ("Dernier message de ma part",
         "{bonjour},\n\nC'est mon dernier message : si ce n'est pas le bon moment, aucun souci.\n\nLa démo de Bob pour "
         "votre concession reste ouverte :\n{lien}\n\nBelle continuation,"),
    ],
    "INSURANCE_BROKER": [
        ("{entreprise} : vos demandes de cotation préparées sur WhatsApp",
         "{bonjour},\n\nVos clients et prospects vous écrivent sur WhatsApp à toute heure. Bob, notre assistant virtuel, "
         "leur répond tout de suite, réunit les informations utiles pour la cotation (sans jamais donner de prix) et "
         "vous la transmet. Il suit aussi les échéances de vos contrats.\n\nVous pouvez l'essayer en 2 minutes :\n{lien}"
         "\n\nBonne journée,"),
        ("Re : cotations et échéances pour {entreprise}",
         "{bonjour},\n\nJe reviens vers vous : chaque contrat qui arrive à échéance sans rappel est un client qui peut "
         "partir. Bob rappelle vos clients au bon moment et prépare les demandes de cotation :\n{lien}\n\nBonne journée,"),
        ("Dernier message de ma part",
         "{bonjour},\n\nC'est mon dernier message : si ce n'est pas le bon moment, aucun souci.\n\nLa démo de Bob pour "
         "votre cabinet reste ouverte :\n{lien}\n\nBelle continuation,"),
    ],
}


class MailerError(ValueError):
    """Refus : message montré tel quel."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def settings_ok() -> bool:
    from app.core.config import get_settings

    s = get_settings()
    return bool(s.prospect_smtp_username and s.prospect_smtp_password)


def email_hash(email: str) -> str:
    return hashlib.sha256((email or "").strip().lower().encode()).hexdigest()


async def get_settings_row(db):
    from app.models.prospect import ProspectionSettings

    row = await db.get(ProspectionSettings, 1)
    if row is None:
        row = ProspectionSettings(id=1, daily_limit=LIMITS[0], sending_enabled=True, last_imap_uid=0)
        db.add(row)
        await db.flush()
    return row


async def suppressed(db, email: str | None) -> bool:
    from app.models.prospect import ProspectSuppression

    if not email:
        return False
    return (await db.execute(select(ProspectSuppression.id).where(ProspectSuppression.email_hash == email_hash(email)))).first() is not None


async def suppress(db, email: str | None, reason: str) -> None:
    from app.models.prospect import ProspectSuppression

    if email and not await suppressed(db, email):
        db.add(ProspectSuppression(email_hash=email_hash(email), reason=reason))


# --- Textes ----------------------------------------------------------------------------------------------------

def steps_of(campaign) -> list[tuple[str, str]]:
    steps = [(campaign.subject_1, campaign.body_1)]
    for subject, body in ((campaign.subject_2, campaign.body_2), (campaign.subject_3, campaign.body_3)):
        if subject and body:
            steps.append((subject, body))
    return steps


def fill(template: str, prospect) -> str:
    first = (prospect.contact_name or "").split()[0] if (prospect.contact_name or "").strip() else ""
    values = {"{bonjour}": f"Bonjour {first}" if first else "Bonjour", "{prenom}": first,
              "{entreprise}": prospect.company or "", "{ville}": prospect.city or "", "{lien}": pros.link(prospect.code)}
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def _base() -> str:
    from app.core.config import get_settings

    return get_settings().public_base_url.rstrip("/")


def stop_url(prospect) -> str:
    return f"{_base()}/p/{prospect.code}/stop"


def render(prospect, subject: str, body: str, email_id: str | None = None) -> tuple[str, str, str]:
    """(objet, texte, html) d'un email ; l'image d'ouverture n'est ajoutée que pour un vrai envoi (email_id)."""
    subject = " ".join(fill(subject, prospect).split())[:200]
    text_body = fill(body, prospect).strip()
    sector = pros.SECTORS.get(prospect.sector, "").lower()
    legal = LEGAL.format(year=now_utc().year)
    why = (f"Vous recevez cet email car {prospect.company} exerce une activité ({sector}) que Bob peut aider. "
           f"Pour ne plus recevoir nos emails : {stop_url(prospect)}")
    text = f"{text_body}\n\n{SIGNATURE}\n\n--\n{why}\n{legal}"

    def paragraph(chunk: str) -> str:
        out = []
        for line in chunk.split("\n"):
            safe = escape(line)
            safe = re.sub(r"(https?://[^\s<]+)", lambda m: f'<a href="{m.group(1)}" style="color:#1B4332;font-weight:600;">{m.group(1)}</a>', safe)
            out.append(safe)
        return f'<p style="margin:0 0 14px;">{"<br>".join(out)}</p>'

    pixel = f'<img src="{_base()}/p/{prospect.code}/o/{email_id}.gif" width="1" height="1" alt="" style="display:block;border:0;">' if email_id else ""
    html = (f'<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(subject)}</title></head><body style="margin:0;padding:0;background:#ffffff;">'
            f'<div style="max-width:580px;margin:0 auto;padding:20px 16px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
            f'font-size:15px;line-height:1.6;color:#13241A;">'
            + "".join(paragraph(chunk) for chunk in re.split(r"\n\s*\n", text_body))
            + f'<p style="margin:0 0 22px;font-weight:600;">{escape(SIGNATURE)}</p>'
            f'<p style="margin:0;padding-top:12px;border-top:1px solid #E3EBE6;font-size:11.5px;line-height:1.5;color:#5B6F65;">'
            f'{escape(why.split(" : ")[0])} : <a href="{escape(stop_url(prospect))}" style="color:#5B6F65;">ne plus recevoir nos emails</a>.<br>'
            f'{escape(legal)}</p>{pixel}</div></body></html>')
    return subject, text, html


def build_email(prospect, subject: str, text: str, html: str, message_id: str) -> EmailMessage:
    from app.core.config import get_settings

    s = get_settings()
    msg = EmailMessage()
    msg["From"] = formataddr((s.prospect_from_name, s.prospect_smtp_username))
    msg["To"] = prospect.email
    msg["Reply-To"] = s.prospect_smtp_username
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["List-Unsubscribe"] = f"<{stop_url(prospect)}>, <mailto:{s.prospect_smtp_username}?subject=desinscription>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def smtp_transport(msg: EmailMessage) -> None:
    from app.core.config import get_settings

    s = get_settings()
    with smtplib.SMTP(s.prospect_smtp_host, s.prospect_smtp_port, timeout=20) as server:
        server.starttls()
        server.login(s.prospect_smtp_username, s.prospect_smtp_password)
        server.send_message(msg)


# --- Qui reçoit quoi, et quand -------------------------------------------------------------------------------------

def in_send_hours(prospect, now: datetime) -> bool:
    from app.services.local_time import COUNTRY_TIMEZONES

    local = now.astimezone(ZoneInfo(COUNTRY_TIMEZONES.get((prospect.country or "").upper(), "UTC")))
    return local.weekday() < 5 and SEND_HOURS[0] <= local.hour < SEND_HOURS[1]


async def eligible(db, campaign) -> list:
    """Prospects jamais contactés, avec email, correspondant aux filtres, ni désinscrits ni déjà dans une campagne."""
    from app.models.prospect import Prospect, ProspectSuppression

    stmt = select(Prospect).where(Prospect.status == "ACTIVE", Prospect.email.is_not(None), Prospect.contacted_at.is_(None),
                                  Prospect.campaign_id.is_(None), Prospect.tenant_id.is_(None))
    if campaign.sector:
        stmt = stmt.where(Prospect.sector == campaign.sector)
    if campaign.source:
        stmt = stmt.where(func.lower(Prospect.source) == campaign.source.strip().lower())
    if campaign.country:
        stmt = stmt.where(Prospect.country == campaign.country)
    blocked = set((await db.execute(select(ProspectSuppression.email_hash))).scalars())
    return [p for p in (await db.execute(stmt.order_by(Prospect.created_at))).scalars().all() if email_hash(p.email) not in blocked]


def stopped(prospect) -> bool:
    """Plus aucun email : réponse, clic, compte créé, ou statut autre que « en cours »."""
    return (prospect.status != "ACTIVE" or prospect.replied_at is not None or prospect.first_click_at is not None
            or prospect.tenant_id is not None or prospect.demo_at is not None)


async def sent_last_24h(db, now: datetime) -> int:
    from app.models.prospect import ProspectEmail

    return (await db.execute(select(func.count(ProspectEmail.id)).where(
        ProspectEmail.status.in_(("SENT", "BOUNCED")), ProspectEmail.sent_at >= now - timedelta(hours=24)))).scalar_one()


async def next_candidate(db, now: datetime):
    """Le prochain email à envoyer : d'abord les relances dues, puis les premiers emails, dans les heures d'envoi."""
    from app.models.prospect import Prospect, ProspectCampaign

    running = {c.id: c for c in (await db.execute(select(ProspectCampaign).where(ProspectCampaign.status == "RUNNING"))).scalars()}
    if not running:
        return None, None
    rows = (await db.execute(select(Prospect).where(Prospect.campaign_id.in_(running.keys()), Prospect.status == "ACTIVE")
                             .order_by(Prospect.email_step.desc(), Prospect.created_at))).scalars().all()
    for p in rows:
        campaign = running[p.campaign_id]
        if stopped(p) or not p.email or p.email_step >= len(steps_of(campaign)):
            continue
        if p.email_step > 0 and (p.next_email_at is None or pros.aware(p.next_email_at) > now):
            continue
        if not in_send_hours(p, now):
            continue
        if await suppressed(db, p.email):
            continue
        return p, campaign
    return None, None


async def send_next(db, now: datetime | None = None, transport=None) -> str:
    """Envoie au plus un email ; renvoie ce qui s'est passé (journal et tests)."""
    from app.models.prospect import ProspectEmail

    now = now or now_utc()
    if not settings_ok():
        return "not-configured"
    row = await get_settings_row(db)
    if not row.sending_enabled:
        return "paused"
    if await sent_last_24h(db, now) >= row.daily_limit:
        return "limit"
    prospect, campaign = await next_candidate(db, now)
    if prospect is None:
        return "nothing"
    step = prospect.email_step + 1
    subject_t, body_t = steps_of(campaign)[step - 1]
    record = ProspectEmail(prospect_id=prospect.id, campaign_id=campaign.id, step=step, status="SENT", sent_at=now)
    db.add(record)
    await db.flush()
    from app.core.config import get_settings

    domain = (get_settings().prospect_smtp_username.split("@")[-1] or "agencai").strip()
    record.message_id = make_msgid(idstring=f"bob{step}", domain=domain)
    subject, text, html = render(prospect, subject_t, body_t, str(record.id))
    try:
        (transport or smtp_transport)(build_email(prospect, subject, text, html, record.message_id))
    except smtplib.SMTPAuthenticationError:
        await db.rollback()
        await _error(db, "Connexion à Gmail refusée : vérifiez le mot de passe d'application dans le .env", now)
        return "auth-error"
    except smtplib.SMTPRecipientsRefused:
        record.status, record.error = "BOUNCED", "Adresse refusée par le serveur"
        await _invalid(db, prospect, "Adresse refusée à l'envoi")
        await db.commit()
        return "bounced"
    except Exception as exc:  # noqa: BLE001 — une erreur passagère : on réessaie plus tard, puis on abandonne
        record.status, record.error = "FAILED", str(exc)[:300]
        failures = (await db.execute(select(func.count(ProspectEmail.id)).where(
            ProspectEmail.prospect_id == prospect.id, ProspectEmail.step == step, ProspectEmail.status == "FAILED"))).scalar_one()
        if failures >= FAILURES_BEFORE_INVALID:  # cet échec compris
            await _invalid(db, prospect, "Envois en échec")
        await db.commit()
        await _error(db, f"Envoi impossible : {str(exc)[:200]}", now)
        return "failed"
    prospect.email_step = step
    prospect.contacted_at = prospect.contacted_at or now
    prospect.last_contact_at = now
    prospect.next_email_at = now + STEP_DELAYS[step] if step < len(steps_of(campaign)) else None
    pros.add_event(db, prospect, "CONTACT", channel="EMAIL", actor="Bob",
                   detail=f"Email {step}/{len(steps_of(campaign))} — « {subject} » (campagne {campaign.name})", at=now)
    row.last_error = None
    await db.commit()
    return "sent"


async def _error(db, message: str, now: datetime) -> None:
    row = await get_settings_row(db)
    row.last_error, row.last_error_at = message[:300], now
    await db.commit()
    logger.warning("Prospection : %s", message)


async def _invalid(db, prospect, reason: str) -> None:
    prospect.status, prospect.status_reason, prospect.next_email_at = "INVALID", reason, None
    await suppress(db, prospect.email, "BOUNCED")
    pros.add_event(db, prospect, "STATUS", detail=f"Coordonnées invalides — {reason}", actor="Bob")


async def unsubscribe(db, prospect, how: str) -> None:
    if prospect.status != "UNSUBSCRIBED":
        prospect.status, prospect.status_reason, prospect.next_email_at = "UNSUBSCRIBED", how, None
        pros.add_event(db, prospect, "STATUS", detail=f"Ne plus contacter — {how}", actor="prospect")
    await suppress(db, prospect.email, "UNSUBSCRIBED")
    await db.commit()


async def record_open(db, prospect, email_id: str) -> None:
    from uuid import UUID

    from app.models.prospect import ProspectEmail

    try:
        record = await db.get(ProspectEmail, UUID(email_id))
    except ValueError:
        return
    if record is None or record.prospect_id != prospect.id or record.opened_at is not None:
        return
    moment = now_utc()
    record.opened_at = moment
    if prospect.first_opened_at is None:
        prospect.first_opened_at = moment
        pros.add_event(db, prospect, "OPEN", detail=f"Email {record.step} ouvert (indicatif)", actor="prospect", at=moment)
    await db.commit()


# --- Campagnes ---------------------------------------------------------------------------------------------------

async def start(db, campaign) -> int:
    """Rattache les prospects visés et lance l'envoi ; renvoie le nombre de prospects ajoutés à la campagne."""
    from app.models.prospect import Prospect

    if not settings_ok():
        raise MailerError("Le compte Gmail de prospection n'est pas encore réglé (.env du serveur)")
    added = 0
    for prospect in await eligible(db, campaign):
        prospect.campaign_id, prospect.email_step, prospect.next_email_at = campaign.id, 0, None
        added += 1
    already = (await db.execute(select(func.count(Prospect.id)).where(Prospect.campaign_id == campaign.id))).scalar_one()
    if not already:
        raise MailerError("Aucun prospect à contacter : il faut des prospects avec un email, jamais contactés, "
                          "qui correspondent aux filtres")
    campaign.status = "RUNNING"
    campaign.started_at = campaign.started_at or now_utc()
    await db.commit()
    return added


async def stats(db, campaign) -> dict:
    from app.models.prospect import Prospect, ProspectEmail

    prospects = (await db.execute(select(Prospect).where(Prospect.campaign_id == campaign.id))).scalars().all()
    emails = (await db.execute(select(ProspectEmail).where(ProspectEmail.campaign_id == campaign.id))).scalars().all()
    sent_to = {e.prospect_id for e in emails if e.status in ("SENT", "BOUNCED")}
    total_steps = len(steps_of(campaign))
    waiting = sum(1 for p in prospects if not stopped(p) and p.email_step < total_steps)
    return {
        "targeted": len(prospects), "emails_sent": sum(1 for e in emails if e.status in ("SENT", "BOUNCED")),
        "reached": len(sent_to), "opened": sum(1 for p in prospects if p.first_opened_at and p.id in sent_to),
        "clicked": sum(1 for p in prospects if p.first_click_at and p.id in sent_to),
        "replied": sum(1 for p in prospects if p.replied_at), "signed_up": sum(1 for p in prospects if p.tenant_id),
        "unsubscribed": sum(1 for p in prospects if p.status == "UNSUBSCRIBED"),
        "bounced": sum(1 for p in prospects if p.status == "INVALID"), "waiting": waiting,
        "finished": campaign.status == "RUNNING" and waiting == 0,
    }


def recommendation(limit: int, sent_7d: int, bounced_7d: int, error: str | None) -> str:
    """Quand monter le plafond (la décision reste humaine)."""
    if error:
        return "Réglez d'abord l'erreur d'envoi avant d'augmenter."
    rate = bounced_7d / sent_7d if sent_7d else 0
    if sent_7d >= 20 and rate >= 0.05:
        return f"{round(rate * 100)} % d'adresses inexistantes cette semaine : nettoyez vos listes avant d'augmenter."
    higher = next((n for n in LIMITS if n > limit), None)
    if higher is None:
        return "Plafond au plus haut conseillé pour un compte Gmail."
    if sent_7d >= limit * 3 and rate < 0.03:
        return f"Une semaine d'envois sans souci : vous pouvez passer à {higher} emails par jour."
    return f"Restez à {limit} par jour quelques jours encore avant de passer à {higher}."


# --- Lecture de la boîte : réponses et adresses inexistantes ---------------------------------------------------------

def imap_fetch(last_uid: int) -> list[tuple[int, bytes]]:
    """Messages arrivés depuis le dernier lu, en lecture seule (rien n'est marqué ni déplacé dans Gmail)."""
    import imaplib

    from app.core.config import get_settings

    s = get_settings()
    out = []
    with imaplib.IMAP4_SSL(s.prospect_imap_host) as box:
        box.login(s.prospect_smtp_username, s.prospect_smtp_password)
        box.select("INBOX", readonly=True)
        status, data = box.uid("search", None, f"UID {last_uid + 1}:*")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid][:200]
        for uid in uids:
            status, parts = box.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append((uid, raw))
    return out


def _text_of(message) -> str:
    part = message.get_body(preferencelist=("plain", "html")) if message.is_multipart() else message
    try:
        content = part.get_content() if part is not None else ""
    except Exception:  # noqa: BLE001
        content = ""
    if part is not None and part.get_content_type() == "text/html":
        content = re.sub(r"<[^>]+>", " ", content)
    lines = []
    for line in content.splitlines():
        if line.strip().startswith(">") or re.match(r"^\s*(Le |On ).{5,120}(a écrit|wrote)\s*:", line):
            break
        lines.append(line)
    return " ".join(" ".join(lines).split())


def _is_bounce(message) -> bool:
    sender = (message.get("From") or "").lower()
    return ("mailer-daemon" in sender or "postmaster" in sender or message.get_content_type() == "multipart/report"
            or bool(message.get("X-Failed-Recipients")))


def _is_auto(message) -> bool:
    auto = (message.get("Auto-Submitted") or "no").lower()
    return (auto != "no" or message.get("X-Autoreply") or message.get("X-Autorespond")
            or (message.get("Precedence") or "").lower() in ("auto_reply", "bulk", "junk")
            or bool(AUTO_SUBJECTS.search((message.get("Subject") or "").strip())))


async def process_inbox(db, fetch=None, notify=None) -> dict:
    """Lit les nouveaux messages : adresse inexistante → invalide ; réponse → séquence arrêtée et copie envoyée."""
    from email import message_from_bytes
    from email.policy import default

    from app.models.prospect import Prospect, ProspectEmail

    report = {"replies": 0, "bounces": 0, "unsubscribed": 0, "ignored": 0}
    if not settings_ok():
        return report
    row = await get_settings_row(db)
    await db.commit()
    try:
        messages = (fetch or imap_fetch)(row.last_imap_uid)
    except Exception as exc:  # noqa: BLE001
        await _error(db, f"Lecture de la boîte impossible : {str(exc)[:200]}", now_utc())
        return report
    for uid, raw in messages:
        row.last_imap_uid = max(row.last_imap_uid, uid)
        message = message_from_bytes(raw, policy=default)
        if _is_bounce(message):
            text = raw.decode("utf-8", errors="ignore")
            candidates = {a.lower() for a in re.findall(r"[\w.+'-]+@[\w-]+\.[\w.-]+", text)}
            for header in message.get_all("X-Failed-Recipients") or []:
                candidates |= {a.strip().lower() for a in str(header).split(",")}
            found = (await db.execute(select(Prospect).where(func.lower(Prospect.email).in_(candidates or {""}),
                                                             Prospect.email_step > 0))).scalars().all()
            for prospect in found:
                if prospect.status != "INVALID":
                    await _invalid(db, prospect, "Adresse inexistante (retour de Gmail)")
                    last = (await db.execute(select(ProspectEmail).where(ProspectEmail.prospect_id == prospect.id)
                                             .order_by(ProspectEmail.sent_at.desc()).limit(1))).scalar_one_or_none()
                    if last is not None:
                        last.status = "BOUNCED"
                    report["bounces"] += 1
            continue
        if _is_auto(message):
            report["ignored"] += 1
            continue
        refs = " ".join(str(message.get(h) or "") for h in ("In-Reply-To", "References"))
        prospect = None
        ids = re.findall(r"<[^>]+>", refs)
        if ids:
            email_row = (await db.execute(select(ProspectEmail).where(ProspectEmail.message_id.in_(ids)).limit(1))).scalar_one_or_none()
            if email_row is not None:
                prospect = await db.get(Prospect, email_row.prospect_id)
        if prospect is None:
            sender = parseaddr(message.get("From") or "")[1].lower()
            prospect = (await db.execute(select(Prospect).where(func.lower(Prospect.email) == sender, Prospect.email_step > 0)
                                         .limit(1))).scalar_one_or_none() if sender else None
        if prospect is None:
            report["ignored"] += 1
            continue
        text = _text_of(message)
        moment = now_utc()
        if STOP_WORDS.search(text[:200]) and len(text) < 200:
            await unsubscribe(db, prospect, "demandé par email")
            report["unsubscribed"] += 1
            continue
        prospect.replied_at = prospect.replied_at or moment
        prospect.next_email_at = None
        pros.add_event(db, prospect, "REPLY", detail=(text[:300] or "(message sans texte)"), actor="prospect", at=moment)
        report["replies"] += 1
        await db.commit()
        await (notify or notify_reply)(prospect, text)
    await db.commit()
    return report


async def notify_reply(prospect, text: str) -> None:
    """Copie de la réponse à l'adresse habituelle (par l'envoi d'emails ordinaire de Bob)."""
    from app.core.config import get_settings
    from app.services.email_service import send_email

    to = get_settings().prospect_copy_to
    if not to:
        return
    who = prospect.contact_name or prospect.email
    body = (f"Bonjour,\n\n{who} ({prospect.company}) a répondu à un email de prospection :\n\n« {text[:1500]} »\n\n"
            f"Répondez depuis la boîte de prospection, puis notez la suite dans la fiche du prospect.\n\n"
            f"Fiche : {_base()}/superadmin/\n\n— Bob")
    try:
        send_email(to=to, subject=f"Réponse de {who} ({prospect.company})", body=body, from_name="Bob")
    except Exception:  # noqa: BLE001
        logger.warning("Copie de la réponse impossible")
