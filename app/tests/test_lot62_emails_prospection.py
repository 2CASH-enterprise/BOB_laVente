"""
Lot 62 — emails de prospection par petits lots (compte Gmail séparé) : campagnes, 3 emails au plus, plafond,
heures d'envoi, ouverture, désinscription, réponses et adresses inexistantes lues dans la boîte.
"""
import re
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.prospect import Prospect, ProspectCampaign, ProspectEmail, ProspectEvent, ProspectionSettings
from app.models.superadmin_user import SuperAdminUser
from app.services import prospect_mailer as mailer
from app.services import prospection as pros

ROOT = Path(__file__).resolve().parents[1]
SA_HTML = (ROOT / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")
API = "/api/v1/superadmin/prospection"
TUESDAY = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)  # mardi 10 h à Abidjan


@pytest.fixture
def gmail(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "prospect_smtp_username", "prosp.agenc@gmail.com")
    monkeypatch.setattr(s, "prospect_smtp_password", "app-password")
    monkeypatch.setattr(s, "prospect_copy_to", "VOTRE_EMAIL_ICI")
    return s


class Outbox:
    def __init__(self, error=None):
        self.sent, self.error = [], error

    def __call__(self, msg: EmailMessage):
        if self.error is not None:
            raise self.error
        self.sent.append(msg)


async def _prospect(db, company="Auto Prestige", email="serge@autoprestige.ci", sector="CAR_DEALERSHIP", **extra):
    p = Prospect(code=await pros.new_code(db), company=company, contact_name=extra.pop("contact_name", "Serge Kouadio"),
                 email=email, country=extra.pop("country", "CI"), sector=sector, status="ACTIVE", **extra)
    db.add(p)
    await db.commit()
    return p


async def _campaign(db, steps=3, **filters):
    texts = mailer.DEFAULTS["CAR_DEALERSHIP"]
    fields = {}
    for i, (subject, body) in enumerate(texts[:steps], start=1):
        fields[f"subject_{i}"], fields[f"body_{i}"] = subject, body
    c = ProspectCampaign(name="Concessions", status="DRAFT", **filters, **fields)
    db.add(c)
    await db.commit()
    return c


async def _admin(client, db):
    email = f"sa{uuid.uuid4().hex[:6]}@agencai.ci"
    db.add(SuperAdminUser(email=email, hashed_password=hash_password("supersecret123"), full_name="Koffi Yao"))
    await db.commit()
    token = (await client.post("/api/v1/superadmin/login", data={"username": email, "password": "supersecret123"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, email


# --- Contenu de l'email -------------------------------------------------------------------------------------

def test_render_and_headers(gmail):
    p = Prospect(company="Auto <Prestige>", contact_name="Serge Kouadio", city="Marcory", sector="CAR_DEALERSHIP", code="abc234",
                 email="serge@autoprestige.ci")
    subject, text, html = mailer.render(p, "{entreprise} à {ville}", "{bonjour},\n\nVoici : {lien}\n\nMerci {prenom}", "e1")
    assert subject == "Auto <Prestige> à Marcory"
    assert text.startswith("Bonjour Serge,\n\nVoici : ") and text.split("\n")[2].endswith("/p/abc234") and "Merci Serge" in text
    assert "Bob, le vendeur virtuel d'AgenC'AI" in text and "/p/abc234/stop" in text
    assert "SAS 2 Cash Enterprise (Holding) — 1 Rue Hartz Huel, 22500 Kerfot, France. Exploitation par : AgenC'AI." in text
    assert "Auto &lt;Prestige&gt;" in html and "Auto <Prestige>" not in html
    assert '/p/abc234/o/e1.gif" width="1"' in html and 'href="' in html and "<script" not in html
    assert "/o/" not in mailer.render(p, "x", "y")[2]  # aperçu et essai : pas d'image d'ouverture
    anonymous = Prospect(company="X", sector="ONLINE_STORE", code="c")
    assert mailer.render(anonymous, "x", "{bonjour},")[1].startswith("Bonjour,")
    msg = mailer.build_email(p, subject, text, html, "<id@gmail.com>")
    assert msg["From"] == "Bob de AgenC'AI <prosp.agenc@gmail.com>" and msg["Reply-To"] == "prosp.agenc@gmail.com"
    assert msg["List-Unsubscribe"].startswith("<") and "/p/abc234/stop>" in msg["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and msg["Message-ID"] == "<id@gmail.com>"
    assert [part.get_content_type() for part in msg.iter_parts()] == ["text/plain", "text/html"]


def test_defaults_are_complete_and_vouvoient():
    for sector, steps in mailer.DEFAULTS.items():
        assert len(steps) == 3 and "{lien}" in steps[0][1], sector
        for subject, body in steps:
            assert body.startswith("{bonjour},") and " tu " not in body and "ton " not in body


def test_recommendation():
    assert "Réglez d'abord" in mailer.recommendation(20, 100, 0, "erreur")
    assert "nettoyez vos listes" in mailer.recommendation(20, 40, 3, None)
    assert mailer.recommendation(20, 60, 1, None).endswith("passer à 40 emails par jour.")
    assert mailer.recommendation(20, 30, 0, None).startswith("Restez à 20 par jour")
    assert mailer.recommendation(200, 900, 0, None) == "Plafond au plus haut conseillé pour un compte Gmail."


def test_send_hours():
    p = Prospect(country="CI")
    assert mailer.in_send_hours(p, TUESDAY) and not mailer.in_send_hours(p, TUESDAY.replace(hour=8))
    assert not mailer.in_send_hours(p, TUESDAY.replace(hour=17)) and not mailer.in_send_hours(p, TUESDAY + timedelta(days=4))
    assert not mailer.in_send_hours(Prospect(country="FR"), TUESDAY.replace(hour=15, minute=30))  # 17 h 30 à Paris


# --- Envoi ----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sequence_of_three_emails(db_session, gmail):
    p = await _prospect(db_session)
    c = await _campaign(db_session)
    assert await mailer.start(db_session, c) == 1
    out = Outbox()
    assert await mailer.send_next(db_session, TUESDAY, out) == "sent"
    msg = out.sent[0]
    assert msg["To"] == "serge@autoprestige.ci" and msg["Subject"] == "Auto Prestige : plus de rendez-vous d'essai depuis WhatsApp"
    await db_session.refresh(p)
    assert p.email_step == 1 and p.contacted_at is not None and pros.aware(p.next_email_at) == TUESDAY + timedelta(days=4)
    record = (await db_session.execute(select(ProspectEmail))).scalar_one()
    assert record.message_id == msg["Message-ID"] and record.message_id.endswith("@gmail.com>")
    event = (await db_session.execute(select(ProspectEvent).where(ProspectEvent.kind == "CONTACT"))).scalar_one()
    assert event.channel == "EMAIL" and event.detail.startswith("Email 1/3 — « Auto Prestige :") and event.actor == "Bob"
    assert await mailer.send_next(db_session, TUESDAY + timedelta(days=3), out) == "nothing"  # pas encore l'heure de la relance
    assert await mailer.send_next(db_session, TUESDAY + timedelta(days=4), out) == "nothing"  # samedi : on attend lundi
    monday = TUESDAY + timedelta(days=6)
    assert await mailer.send_next(db_session, monday, out) == "sent"
    await db_session.refresh(p)
    assert p.email_step == 2 and pros.aware(p.next_email_at) == monday + timedelta(days=6)
    assert out.sent[1]["Subject"] == "Re : rendez-vous d'essai pour Auto Prestige"
    assert await mailer.send_next(db_session, monday + timedelta(days=7), out) == "sent"
    await db_session.refresh(p)
    assert p.email_step == 3 and p.next_email_at is None
    assert await mailer.send_next(db_session, TUESDAY + timedelta(days=17), out) == "nothing"  # 3 emails au plus
    assert len(out.sent) == 3
    assert (await mailer.stats(db_session, c))["finished"] is True


@pytest.mark.asyncio
async def test_sequence_stops_on_click_reply_signup(db_session, gmail):
    c = await _campaign(db_session, steps=2)
    clicked = await _prospect(db_session, company="A", email="a@x.ci")
    replied = await _prospect(db_session, company="B", email="b@x.ci")
    signed = await _prospect(db_session, company="C", email="c@x.ci")
    await mailer.start(db_session, c)
    out = Outbox()
    for _ in range(3):
        assert await mailer.send_next(db_session, TUESDAY, out) == "sent"
    clicked.first_click_at = TUESDAY
    replied.replied_at = TUESDAY
    signed.tenant_id = uuid.uuid4()
    await db_session.commit()
    assert await mailer.send_next(db_session, TUESDAY + timedelta(days=6), out) == "nothing"  # lundi, relances dues : aucune
    assert len(out.sent) == 3


@pytest.mark.asyncio
async def test_limit_hours_pause_and_priority(db_session, gmail):
    c = await _campaign(db_session, steps=2)
    first = await _prospect(db_session, company="Premier", email="p1@x.ci")
    await mailer.start(db_session, c)
    out = Outbox()
    assert await mailer.send_next(db_session, TUESDAY.replace(hour=7), out) == "nothing"  # avant 9 h
    assert await mailer.send_next(db_session, TUESDAY, out) == "sent"
    later = await _prospect(db_session, company="Second", email="p2@x.ci")
    await mailer.start(db_session, c)
    row = await db_session.get(ProspectionSettings, 1)
    row.daily_limit = 1
    await db_session.commit()
    assert await mailer.send_next(db_session, TUESDAY + timedelta(hours=1), out) == "limit"  # 1 sur 24 h glissantes
    row.daily_limit = 20
    row.sending_enabled = False
    await db_session.commit()
    assert await mailer.send_next(db_session, TUESDAY + timedelta(hours=1), out) == "paused"
    row.sending_enabled = True
    await db_session.commit()
    # Le lundi suivant : la relance du premier passe avant le premier email du second.
    assert await mailer.send_next(db_session, TUESDAY + timedelta(days=6), out) == "sent"
    assert out.sent[-1]["To"] == "p1@x.ci" and out.sent[-1]["Subject"].startswith("Re :")
    assert await mailer.send_next(db_session, TUESDAY + timedelta(days=6, minutes=3), out) == "sent"
    assert out.sent[-1]["To"] == "p2@x.ci"
    await db_session.refresh(first)
    await db_session.refresh(later)
    assert first.email_step == 2 and later.email_step == 1


@pytest.mark.asyncio
async def test_not_configured(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "prospect_smtp_password", "")
    c = await _campaign(db_session)
    await _prospect(db_session)
    with pytest.raises(mailer.MailerError, match="pas encore réglé"):
        await mailer.start(db_session, c)
    assert await mailer.send_next(db_session, TUESDAY, Outbox()) == "not-configured"
    assert await mailer.process_inbox(db_session, fetch=lambda uid: 1 / 0) == {"replies": 0, "bounces": 0, "unsubscribed": 0, "ignored": 0}


@pytest.mark.asyncio
async def test_send_errors(db_session, gmail):
    c = await _campaign(db_session)
    p = await _prospect(db_session)
    await mailer.start(db_session, c)
    auth = smtplib.SMTPAuthenticationError(535, b"bad")
    assert await mailer.send_next(db_session, TUESDAY, Outbox(auth)) == "auth-error"
    row = await db_session.get(ProspectionSettings, 1)
    await db_session.refresh(row)
    assert "mot de passe d'application" in row.last_error
    assert (await db_session.execute(select(ProspectEmail))).scalars().all() == []  # rien de noté
    for attempt in range(2):
        assert await mailer.send_next(db_session, TUESDAY, Outbox(OSError("réseau"))) == "failed"
        await db_session.refresh(p)
        assert p.status == "ACTIVE"
    assert await mailer.send_next(db_session, TUESDAY, Outbox(OSError("réseau"))) == "failed"
    await db_session.refresh(p)
    assert p.status == "INVALID" and p.status_reason == "Envois en échec"  # trois échecs : on abandonne
    other = await _prospect(db_session, company="Refusé", email="nobody@x.ci")
    await mailer.start(db_session, c)
    refused = smtplib.SMTPRecipientsRefused({"nobody@x.ci": (550, b"no such user")})
    assert await mailer.send_next(db_session, TUESDAY, Outbox(refused)) == "bounced"
    await db_session.refresh(other)
    assert other.status == "INVALID" and await mailer.suppressed(db_session, "NOBODY@x.ci")
    # Une nouvelle fiche avec la même adresse n'est plus jamais visée.
    await _prospect(db_session, company="Refusé bis", email="nobody@x.ci")
    assert [x.company for x in await mailer.eligible(db_session, c)] == []


@pytest.mark.asyncio
async def test_eligible_filters(db_session, gmail):
    await _prospect(db_session, company="Bon", email="ok@x.ci", source="Salon auto")
    await _prospect(db_session, company="Autre source", email="s@x.ci", source="Réseau")
    await _prospect(db_session, company="Sans email", email=None, source="Salon auto")
    await _prospect(db_session, company="Déjà contacté", email="d@x.ci", source="Salon auto", contacted_at=TUESDAY)
    await _prospect(db_session, company="Perdu", email="l@x.ci", source="Salon auto", status_reason=None)
    lost = (await db_session.execute(select(Prospect).where(Prospect.company == "Perdu"))).scalar_one()
    lost.status = "NOT_INTERESTED"
    await _prospect(db_session, company="Commerce", email="c@x.ci", sector="ONLINE_STORE", source="Salon auto")
    await _prospect(db_session, company="Sénégal", email="sn@x.ci", source="Salon auto", country="SN")
    await db_session.commit()
    c = await _campaign(db_session, sector="CAR_DEALERSHIP", source="salon AUTO", country="CI")
    assert [p.company for p in await mailer.eligible(db_session, c)] == ["Bon"]


# --- Ouverture, désinscription ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_pixel_and_unsubscribe(client, db_session, gmail):
    p = await _prospect(db_session)
    c = await _campaign(db_session)
    await mailer.start(db_session, c)
    await mailer.send_next(db_session, TUESDAY, Outbox())
    record = (await db_session.execute(select(ProspectEmail))).scalar_one()
    other = await _prospect(db_session, company="Autre", email="o@x.ci")
    await client.get(f"/p/{other.code}/o/{record.id}.gif")  # l'email d'un autre : ignoré
    for _ in range(2):
        pixel = await client.get(f"/p/{p.code}/o/{record.id}.gif")
        assert pixel.status_code == 200 and pixel.headers["content-type"] == "image/gif" and pixel.content.startswith(b"GIF89a")
    assert (await client.get(f"/p/{p.code}/o/pas-un-id.gif")).status_code == 200
    await mailer.send_next(db_session, TUESDAY + timedelta(days=6), Outbox())  # la relance, ouverte aussi
    second = (await db_session.execute(select(ProspectEmail).where(ProspectEmail.step == 2))).scalar_one()
    await client.get(f"/p/{p.code}/o/{second.id}.gif")
    await db_session.refresh(p)
    await db_session.refresh(other)
    assert p.first_opened_at is not None and other.first_opened_at is None
    opens = (await db_session.execute(select(ProspectEvent).where(ProspectEvent.kind == "OPEN"))).scalars().all()
    assert len(opens) == 1
    page = await client.get(f"/p/{p.code}/stop")
    assert page.status_code == 200 and '<form method="post"' in page.text and "Auto Prestige" in page.text
    await db_session.refresh(p)
    assert p.status == "ACTIVE"  # ouvrir la page ne désinscrit pas (les antivirus ouvrent les liens)
    done = await client.post(f"/p/{p.code}/stop")
    assert done.status_code == 200 and "Vous ne recevrez plus nos emails" in done.text
    await db_session.refresh(p)
    assert p.status == "UNSUBSCRIBED" and await mailer.suppressed(db_session, p.email)
    assert "Vous ne recevrez plus nos emails" in (await client.get(f"/p/{p.code}/stop")).text
    assert "Lien inconnu" in (await client.post("/p/inconnu/stop")).text
    assert await mailer.send_next(db_session, TUESDAY + timedelta(days=5), Outbox()) == "nothing"


# --- Lecture de la boîte -----------------------------------------------------------------------------------------

def _mail(sender, subject, text, in_reply_to=None, **headers) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "prosp.agenc@gmail.com", subject
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    for key, value in headers.items():
        m[key.replace("_", "-")] = value
    m.set_content(text)
    return m.as_bytes()


@pytest.mark.asyncio
async def test_inbox_replies_bounces_and_stop(db_session, gmail):
    c = await _campaign(db_session)
    serge = await _prospect(db_session)
    fatou = await _prospect(db_session, company="Boutique Fatou", email="fatou@x.ci", contact_name="Fatou")
    ghost = await _prospect(db_session, company="Fantôme", email="ghost@x.ci")
    awa = await _prospect(db_session, company="Awa", email="awa@x.ci")
    await mailer.start(db_session, c)
    for _ in range(4):
        await mailer.send_next(db_session, TUESDAY, Outbox())
    sent = {e.prospect_id: e.message_id for e in (await db_session.execute(select(ProspectEmail))).scalars()}
    notified = []

    async def notify(prospect, text):
        notified.append((prospect.company, text))

    inbox = [
        (11, _mail("Serge <autre.adresse@gmail.com>", "Re: essai", "Oui, appelez-moi demain matin.\n\nLe mar. 13 oct. 2026, Bob a écrit :\n> Bonjour",
                   in_reply_to=sent[serge.id])),
        (12, _mail("fatou@x.ci", "Réponse automatique : absente", "Je suis absente jusqu'au 20.")),
        (16, _mail("fatou@x.ci", "Re: Auto", "Je suis absente jusqu'au 20.", Auto_Submitted="auto-replied")),
        (13, _mail("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)",
                   "Address not found: ghost@x.ci", X_Failed_Recipients="ghost@x.ci")),
        (14, _mail("awa@x.ci", "Re: Auto", "STOP")),
        (15, _mail("inconnu@x.ci", "Bonjour", "Une question")),
    ]
    report = await mailer.process_inbox(db_session, fetch=lambda last: [m for m in inbox if m[0] > last], notify=notify)
    assert report == {"replies": 1, "bounces": 1, "unsubscribed": 1, "ignored": 3}
    for p in (serge, fatou, ghost, awa):
        await db_session.refresh(p)
    assert serge.replied_at is not None and serge.next_email_at is None
    assert notified == [("Auto Prestige", "Oui, appelez-moi demain matin.")]
    reply = (await db_session.execute(select(ProspectEvent).where(ProspectEvent.kind == "REPLY"))).scalar_one()
    assert reply.detail == "Oui, appelez-moi demain matin."
    assert fatou.replied_at is None and fatou.status == "ACTIVE"  # absence du bureau : ignorée
    assert ghost.status == "INVALID" and await mailer.suppressed(db_session, "ghost@x.ci")
    assert (await db_session.execute(select(ProspectEmail.status).where(ProspectEmail.prospect_id == ghost.id))).scalar_one() == "BOUNCED"
    assert awa.status == "UNSUBSCRIBED"
    assert (await db_session.get(ProspectionSettings, 1)).last_imap_uid == 16
    # Le prospect qui a répondu attend une réponse humaine : « à relancer » tant qu'on n'a pas noté de contact.
    serge.last_contact_at = serge.replied_at - timedelta(minutes=5)
    assert pros.to_follow_up(serge, "CONTACTED", TUESDAY.date(), TUESDAY)
    serge.last_contact_at = serge.replied_at + timedelta(minutes=5)
    assert not pros.to_follow_up(serge, "CONTACTED", TUESDAY.date(), TUESDAY)
    again = await mailer.process_inbox(db_session, fetch=lambda last: [m for m in inbox if m[0] > last], notify=notify)
    assert again == {"replies": 0, "bounces": 0, "unsubscribed": 0, "ignored": 0}  # déjà lus

    def broken(last):
        raise OSError("imap")

    await mailer.process_inbox(db_session, fetch=broken)
    assert "Lecture de la boîte impossible" in (await db_session.get(ProspectionSettings, 1)).last_error


@pytest.mark.asyncio
async def test_reply_copy_goes_to_the_usual_address(gmail, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **mail: sent.append(mail) or True)
    p = Prospect(company="Auto <Prestige>", contact_name="Serge", email="serge@x.ci")
    await mailer.notify_reply(p, "Oui")
    assert sent[0]["to"] == "VOTRE_EMAIL_ICI" and sent[0]["subject"] == "Réponse de Serge (Auto <Prestige>)" and "« Oui »" in sent[0]["body"]


# --- API Super Admin ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_campaign_api(client, db_session, gmail, monkeypatch):
    headers, me = await _admin(client, db_session)
    assert (await client.get(f"{API}/email")).status_code == 401
    await _prospect(db_session, company="Auto Prestige", email="serge@x.ci", source="Salon auto")
    defaults = (await client.get(f"{API}/defaults?sector=INSURANCE_BROKER", headers=headers)).json()
    assert set(defaults) == {f"{k}_{i}" for k in ("subject", "body") for i in (1, 2, 3)} and "cotation" in defaults["subject_1"]
    body = {"name": " Concessions   octobre ", "sector": "CAR_DEALERSHIP", "source": "Salon auto", "country": "ci",
            "subject_1": "Bonjour {entreprise}", "body_1": "{bonjour}, voici {lien}", "subject_2": "Relance", "body_2": "  ",
            "subject_3": "Dernier", "body_3": "Fin {lien}"}
    bad = await client.post(f"{API}/campaigns", headers=headers, json=body)
    assert bad.status_code == 422 and "première relance" in bad.json()["detail"]
    body["subject_3"] = body["body_3"] = None
    assert (await client.post(f"{API}/campaigns", headers=headers, json={**body, "body_1": "sans lien"})).status_code == 422
    assert (await client.post(f"{API}/campaigns", headers=headers, json={**body, "sector": "BANQUE"})).status_code == 422
    created = (await client.post(f"{API}/campaigns", headers=headers, json=body)).json()
    assert created["name"] == "Concessions octobre" and created["country"] == "CI" and created["steps"] == 1
    assert created["status"] == "DRAFT" and created["eligible"] == 1
    cid = created["id"]
    preview = (await client.post(f"{API}/campaigns/{cid}/preview", headers=headers)).json()
    assert preview["sample"] == "Auto Prestige" and preview["steps"][0]["subject"] == "Bonjour Auto Prestige" and preview["steps"][0]["day"] == 1
    sent = []
    monkeypatch.setattr(mailer, "smtp_transport", lambda msg: sent.append(msg))
    test = (await client.post(f"{API}/campaigns/{cid}/test", headers=headers)).json()
    assert test == {"sent_to": me, "emails": 1} and sent[0]["To"] == me and sent[0]["Subject"].startswith("[Essai] ")
    assert (await db_session.execute(select(ProspectEmail))).scalars().all() == []  # un essai n'est pas noté
    two = (await client.post(f"{API}/campaigns", headers=headers, json={**body, "name": "Deux", "body_2": "Relance {lien}"})).json()
    assert two["steps"] == 2 and (await client.post(f"{API}/campaigns/{two['id']}/test", headers=headers)).json()["emails"] == 2
    assert [m["Subject"] for m in sent[1:]] == ["[Essai] Bonjour Auto Prestige", "[Essai] Relance"]
    assert (await client.delete(f"{API}/campaigns/{two['id']}", headers=headers)).status_code == 204
    started = (await client.post(f"{API}/campaigns/{cid}/start", headers=headers)).json()
    assert started["status"] == "RUNNING" and started["added"] == 1 and started["stats"]["targeted"] == 1
    assert (await client.put(f"{API}/campaigns/{cid}", headers=headers, json=body)).status_code == 409
    assert (await client.delete(f"{API}/campaigns/{cid}", headers=headers)).status_code == 409
    paused = (await client.post(f"{API}/campaigns/{cid}/pause", headers=headers)).json()
    assert paused["status"] == "PAUSED"
    assert (await client.post(f"{API}/campaigns/{cid}/pause", headers=headers)).status_code == 409
    assert (await client.put(f"{API}/campaigns/{cid}", headers=headers, json={**body, "name": "Renommée"})).json()["name"] == "Renommée"
    empty = (await client.post(f"{API}/campaigns", headers=headers, json={**body, "source": "Personne"})).json()
    nobody = await client.post(f"{API}/campaigns/{empty['id']}/start", headers=headers)
    assert nobody.status_code == 409 and "Aucun prospect" in nobody.json()["detail"]
    assert (await client.delete(f"{API}/campaigns/{empty['id']}", headers=headers)).status_code == 204
    overview = (await client.get(f"{API}/email", headers=headers)).json()
    assert overview["configured"] and overview["from"] == "Bob de AgenC'AI <prosp.agenc@gmail.com>" and overview["copy_to"]
    assert overview["daily_limit"] == 20 and overview["limits"] == [20, 40, 80, 120, 200] and len(overview["campaigns"]) == 1
    assert (await client.put(f"{API}/email/settings", headers=headers, json={"daily_limit": 50})).status_code == 422
    changed = (await client.put(f"{API}/email/settings", headers=headers, json={"daily_limit": 40, "sending_enabled": False})).json()
    assert changed["daily_limit"] == 40 and changed["sending_enabled"] is False


@pytest.mark.asyncio
async def test_test_send_needs_gmail(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "prospect_smtp_password", "")
    headers, _ = await _admin(client, db_session)
    c = await _campaign(db_session)
    r = await client.post(f"{API}/campaigns/{c.id}/test", headers=headers)
    assert r.status_code == 409
    overview = (await client.get(f"{API}/email", headers=headers)).json()
    assert overview["configured"] is False and overview["from"] is None


def test_workers_are_scheduled():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.prospect_emails" in TASK_MODULES
    schedule = celery_app.conf.beat_schedule
    assert schedule["prospect-emails"]["task"] == "app.workers.prospect_emails.send_prospect_email_task"
    assert schedule["prospect-inbox"]["task"] == "app.workers.prospect_emails.read_prospect_inbox_task"


def test_superadmin_page():
    for el in ('id="email-card"', "openCampaignForm()", '"Ont répondu"', "/api/v1/superadmin/prospection/email"):
        assert el in SA_HTML, el
    body = SA_HTML[SA_HTML.index("async function loadEmails("):]
    body = body[:body.index("\n}\n")]
    for field in ("c.name", "d.last_error", "d.recommendation", "d.from"):
        assert f"esc({field}" in body, field
    preview = SA_HTML[SA_HTML.index("async function previewCampaign("):]
    assert 'sandbox=""' in preview[:preview.index("\n}\n")]  # l'aperçu de l'email n'exécute rien
    assert re.search(r"^var emailData =", SA_HTML, re.M)
    assert "PROSPECT_SMTP_PASSWORD" in SA_HTML and "app-password" not in SA_HTML


@pytest.mark.asyncio
async def test_unsubscribed_address_is_never_sent_again(db_session, gmail):
    c = await _campaign(db_session)
    p = await _prospect(db_session, email="awa@x.ci")
    await mailer.start(db_session, c)
    duplicate = Prospect(code=await pros.new_code(db_session), company="Doublon", email="AWA@x.ci", country="CI", sector="CAR_DEALERSHIP")
    db_session.add(duplicate)
    await db_session.commit()
    await mailer.unsubscribe(db_session, duplicate, "lien de désinscription")  # désinscrit depuis une autre fiche
    assert await mailer.send_next(db_session, TUESDAY, Outbox()) == "nothing"
    await db_session.refresh(p)
    assert p.status == "ACTIVE" and p.email_step == 0
