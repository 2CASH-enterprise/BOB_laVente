"""
Lot 83 — demandes reçues par email pour les agences (portails SeLoger, Leboncoin, Bien'ici, Logic-Immo, PAP, formulaire
du site) : adresse Gmail dédiée « +code », lecture par règles fixes, réponse préparée par Bob avec les verrous de
l'immobilier, validation par l'agent par défaut, envoi au nom de l'agence, jamais de WhatsApp envoyé par Bob.
"""
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import default
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.customer import Customer
from app.models.inbound_lead import InboundLead
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import inbound_leads as leads
from app.services import product_sheets
from app.services.business_type import ONLINE_STORE, REAL_ESTATE
from app.tests.fakes import FakeLLMClient, text_response

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
BOX = "bob.contacts@gmail.com"


@pytest.fixture(autouse=True)
def leads_box(monkeypatch):
    monkeypatch.setattr(get_settings(), "leads_smtp_username", BOX)
    monkeypatch.setattr(get_settings(), "leads_smtp_password", "app-password")


async def _agency(db, plan=TenantPlan.PRO, business_type=REAL_ESTATE, code=None, auto=False, phone="+33 6 00 00 00 99"):
    email = f"a{uuid.uuid4().hex[:8]}@parc.fr"
    tenant = Tenant(name="Agence du Parc", country="FR", currency="EUR", email=email, plan=plan, business_type=business_type,
                    business_type_chosen_at=datetime.now(timezone.utc), phone="04 78 00 00 00",
                    lead_inbox_code=code or f"parc-{uuid.uuid4().hex[:4]}", lead_auto_send=auto)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Claire", role=Role.OWNER))
    if phone:
        db.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=f"pn-{uuid.uuid4().hex[:6]}",
                               system_user_token="t", display_phone_number=phone))
    db.add(Product(tenant_id=tenant.id, sku="B-1", name="T3 lumineux rue Garibaldi", price=1150, currency="EUR",
                   stock_quantity=1, active=True, bob_category="APPARTEMENT", image_url="https://cdn.example.com/t3.jpg",
                   sheet=product_sheets.normalize_sheet("APPARTEMENT", {"transaction": "Location", "city": "Lyon 3e",
                                                                        "rooms": 3, "surface": 68, "reference": "MAND-1234",
                                                                        "charges": "120 €"})))
    await db.commit()
    return tenant


async def _headers(client, tenant):
    token = (await client.post("/api/v1/auth/login", data={"username": tenant.email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _seloger(code, *, mid=None, reply_to="Claire Martin <claire.martin@exemple.fr>", body=None, frm="SeLoger <no-reply@seloger.com>",
             extra=""):
    body = body or """Un internaute est intéressé par votre annonce.
Référence de l'annonce : MAND-1234
Nom : Claire Martin
Téléphone : 06 12 34 56 78
Email : claire.martin@exemple.fr
Message :
Bonjour, ce T3 est-il toujours disponible ? Je peux visiter samedi.

L'équipe SeLoger"""
    return (f"From: {frm}\nReply-To: {reply_to}\nTo: agence@parc.fr\nDelivered-To: bob.contacts+{code}@gmail.com\n"
            f"Subject: Nouveau contact pour votre annonce\nMessage-ID: {mid or '<' + uuid.uuid4().hex + '@seloger.com>'}\n"
            f"Auto-Submitted: auto-generated\n{extra}Content-Type: text/plain; charset=utf-8\n\n{body}\n").encode()


def _msg(raw: bytes):
    return message_from_bytes(raw, policy=default)


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, msg):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append(msg)


class Alerts:
    def __init__(self):
        self.items = []

    async def __call__(self, to, subject, body):
        self.items.append((to, subject, body))


GOOD = ("Oui, le T3 de la rue Garibaldi est toujours disponible : 68 m², 3 pièces, 1 150 € charges comprises.\n\n"
        "Vous souhaitez emménager à partir de quand, et avez-vous déjà vos justificatifs de revenus ?\n"
        "Quelles sont vos disponibilités pour une visite ?")


async def _run(db, raws, llm=None, outbox=None, alerts=None):
    return await leads.process_inbox(db, fetch=lambda last: [(i + 1 + last, r) for i, r in enumerate(raws)],
                                     llm_client=llm if llm is not None else FakeLLMClient([text_response(GOOD)] * 10),
                                     notify=alerts or Alerts(), transport=outbox or Outbox())


async def _leads(db, tenant):
    tenant_id = tenant if isinstance(tenant, uuid.UUID) else tenant.id
    db.expire_all()
    return (await db.execute(select(InboundLead).where(InboundLead.tenant_id == tenant_id)
                             .order_by(InboundLead.received_at))).scalars().all()


# --- Lecture par règles fixes ---------------------------------------------------------------------------------------

class _T:
    email, phone, name = "agence@parc.fr", "04 78 00 00 00", "Agence du Parc"


def test_portal_email_is_read():
    data = leads.parse(_msg(_seloger("parc-1a2b")), _T)
    assert data == {"source": "SELOGER", "subject": "Nouveau contact pour votre annonce", "email": "claire.martin@exemple.fr",
                    "name": "Claire Martin", "phone": "06 12 34 56 78", "reference": "MAND-1234",
                    "message": "Bonjour, ce T3 est-il toujours disponible ? Je peux visiter samedi."}
    assert leads.code_of(_msg(_seloger("parc-1a2b"))) == "parc-1a2b"


def test_site_form_in_html_and_forwarded_without_labels():
    raw = ("From: Site <wordpress@parc.fr>\nTo: bob.contacts+parc-1a2b@gmail.com\nSubject: Nouveau message du site\n"
           "Content-Type: text/html; charset=utf-8\n\n<table><tr><td>Prénom</td><td>Jean</td></tr><tr><td>Nom</td><td>Dupont</td></tr>"
           "<tr><td>Adresse e-mail</td><td>jean@dupont.fr</td></tr><tr><td>Votre message</td><td>Je veux vendre ma maison à Bron.</td></tr></table>").encode()
    data = leads.parse(_msg(raw), _T)
    assert (data["source"], data["name"], data["email"], data["message"]) == ("SITE", "Jean Dupont", "jean@dupont.fr",
                                                                              "Je veux vendre ma maison à Bron.")
    forwarded = ("From: Agence <agence@parc.fr>\nTo: bob.contacts+parc-1a2b@gmail.com\nSubject: Fwd: Contact Bien'ici\n"
                 "Content-Type: text/plain; charset=utf-8\n\n---------- Forwarded message ---------\nDe : Bien'ici <contact@bienici.com>\n"
                 "Objet : Contact\n\nPaul Durand (paul.durand@exemple.fr, 07 11 22 33 44) vous a écrit au sujet de la réf. B-1 :\n"
                 "Le parking est-il compris ?").encode()
    data = leads.parse(_msg(forwarded), _T)
    assert data["source"] == "BIENICI" and data["email"] == "paul.durand@exemple.fr" and data["phone"] == "07 11 22 33 44"
    assert data["reference"] == "B-1" and "parking" in data["message"] and "Forwarded" not in data["message"]


def test_agency_and_portal_addresses_are_never_the_client():
    body = "Nouveau contact\nEmail : no-reply@seloger.com\nTéléphone : 04 78 00 00 00\nMessage : Bonjour"
    data = leads.parse(_msg(_seloger("parc-1a2b", reply_to="agence@parc.fr", body=body)), _T)
    assert data["email"] is None and data["phone"] is None  # le numéro de l'agence n'est pas celui du client
    assert leads.code_of(_msg(_seloger("x").replace(b"bob.contacts+x@gmail.com", b"autre+parc-1a2b@gmail.com"))) is None


def test_gmail_forwarding_code():
    raw = (b"From: Gmail Team <forwarding-noreply@google.com>\nTo: bob.contacts+parc-1a2b@gmail.com\n"
           b"Subject: (#123456789) Gmail Forwarding Confirmation - Receive Mail from agence@parc.fr\n\nConfirmation code: 123456789\n")
    message = _msg(raw)
    assert leads.gmail_confirmation(message, leads.body_text(message)) == "123456789"


# --- Parcours complet ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_draft_waits_for_validation_by_default(db_session):
    tenant = await _agency(db_session)
    tenant_email, tenant_id = tenant.email, tenant.id
    outbox, alerts = Outbox(), Alerts()
    llm = FakeLLMClient([text_response("Bonjour Claire,\n\n" + GOOD + "\n\nCordialement,\nBob")])
    report = await _run(db_session, [_seloger(tenant.lead_inbox_code)], llm=llm, outbox=outbox, alerts=alerts)
    assert report["leads"] == 1 and outbox.sent == []  # rien ne part sans validation
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "DRAFT" and lead.source == "SELOGER" and lead.reply_subject == "Votre demande — T3 lumineux rue Garibaldi"
    text = lead.reply_text
    assert text.startswith("Bonjour Claire,\n\nOui, le T3") and "Cordialement" not in text
    assert text.split("https://")[0].count("Bonjour") == 1
    assert "Écrire sur WhatsApp : https://wa.me/33600000099?text=Bonjour%2C%20je%20vous%20%C3%A9cris%20au%20sujet%20de%20T3" in text
    assert "Bien : T3 lumineux rue Garibaldi\nEn bref : À louer · 3 pièces · 68 m² · Lyon 3e\nLoyer charges comprises : 1\u202f150 € / mois" in text
    assert "Référence : MAND-1234" in text and "L'équipe Agence du Parc\nTél. 04 78 00 00 00" in text
    assert "r%C3%A9f.%20MAND-1234" in text and "en réponse à votre demande (SeLoger)" in text
    assert "FICHE" in llm.received_messages[0][0]["content"] and "Loyer mensuel charges comprises : 1 150 EUR" in llm.received_messages[0][0]["content"]
    assert "jamais d'estimation" in llm.received_systems[0].lower() or "Jamais d'estimation" in llm.received_systems[0]
    assert alerts.items[0][0] == tenant_email and alerts.items[0][1].startswith("Réponse prête à valider : Claire Martin")
    customer = (await db_session.execute(select(Customer).where(Customer.tenant_id == tenant_id))).scalar_one()
    assert customer.whatsapp_number == "33612345678" and customer.acquisition_source == "PORTAL"
    assert customer.email == "claire.martin@exemple.fr" and "demande SeLoger" in customer.notes and lead.customer_id == customer.id


@pytest.mark.asyncio
async def test_automatic_sending_in_the_name_of_the_agency(db_session):
    tenant = await _agency(db_session, auto=True)
    tenant_email = tenant.email
    outbox, alerts = Outbox(), Alerts()
    await _run(db_session, [_seloger(tenant.lead_inbox_code)], outbox=outbox, alerts=alerts)
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "SENT" and lead.sent_by == "BOB" and lead.replied_at is not None
    msg = outbox.sent[0]
    assert msg["To"] == "claire.martin@exemple.fr" and msg["Reply-To"] == tenant_email
    assert msg["From"] == f"Agence du Parc <{BOX}>" and "Oui, le T3" in msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "Écrire sur WhatsApp</a>" in html and "https://cdn.example.com/t3.jpg" in html  # bouton et photo du bien
    assert "Loyer charges comprises" in html and "<script" not in html
    assert alerts.items[0][1].startswith("Bob a répondu à Claire Martin")
    assert leads.response_seconds([lead]) is not None


@pytest.mark.asyncio
async def test_no_whatsapp_link_without_a_connected_number_and_no_customer_without_phone(db_session):
    tenant = await _agency(db_session, phone=None)
    tenant_id = tenant.id
    body = "Email : jean@dupont.fr\nMessage : Bonjour, des infos sur vos locations ?"
    await _run(db_session, [_seloger(tenant.lead_inbox_code, reply_to="jean@dupont.fr", body=body)])
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "DRAFT" and "wa.me" not in lead.reply_text and lead.customer_id is None and lead.product_id is None
    assert "Bien :" not in lead.reply_text
    assert (await db_session.execute(select(Customer).where(Customer.tenant_id == tenant_id))).first() is None


@pytest.mark.asyncio
async def test_unreadable_email_goes_to_the_agent(db_session):
    tenant = await _agency(db_session, auto=True)
    outbox = Outbox()
    body = "Quelqu'un a consulté votre annonce. Connectez-vous à votre espace pour voir ses coordonnées."
    await _run(db_session, [_seloger(tenant.lead_inbox_code, reply_to="no-reply@seloger.com", body=body)], outbox=outbox)
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "TO_HANDLE" and "introuvable" in lead.reason and lead.reply_text is None and outbox.sent == []


@pytest.mark.asyncio
async def test_valuation_or_loan_figure_is_never_sent(db_session):
    tenant = await _agency(db_session, auto=True)
    outbox = Outbox()
    llm = FakeLLMClient([text_response("Votre maison vaut environ 350 000 €. Votre mensualité serait de 1 200 € par mois.")])
    await _run(db_session, [_seloger(tenant.lead_inbox_code)], llm=llm, outbox=outbox)
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "TO_HANDLE" and outbox.sent == [] and "350 000" not in lead.reply_text
    assert leads.SAFE_REPLY in lead.reply_text and "estimation" in lead.reason


@pytest.mark.asyncio
async def test_forbidden_document_line_is_removed(db_session):
    tenant = await _agency(db_session)
    llm = FakeLLMClient([text_response("Le T3 est disponible.\nMerci de nous envoyer vos 3 derniers relevés bancaires.\n"
                                       "Quelles sont vos disponibilités ?")])
    await _run(db_session, [_seloger(tenant.lead_inbox_code)], llm=llm)
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "DRAFT" and "relevés bancaires" not in lead.reply_text and "Ligne retirée" in lead.reason


@pytest.mark.asyncio
async def test_ai_outage_prepares_a_simple_reply_for_the_agent(db_session):
    tenant = await _agency(db_session, auto=True)

    class Down:
        model = "x"

        async def create_message(self, **kwargs):
            raise TimeoutError

    outbox = Outbox()
    await _run(db_session, [_seloger(tenant.lead_inbox_code)], llm=Down(), outbox=outbox)
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "TO_HANDLE" and leads.SAFE_REPLY in lead.reply_text and outbox.sent == []


@pytest.mark.asyncio
async def test_what_is_ignored(db_session):
    tenant = await _agency(db_session)
    shop = await _agency(db_session, business_type=ONLINE_STORE)
    free = await _agency(db_session, plan=TenantPlan.FREE)
    code = tenant.lead_inbox_code
    mid = "<same@seloger.com>"
    raws = [
        _seloger(code, mid=mid), _seloger(code, mid=mid),  # le même email deux fois
        _seloger("inconnu-0000"),  # adresse d'aucune agence
        _seloger(shop.lead_inbox_code), _seloger(free.lead_inbox_code),  # boutique, plan gratuit
        _seloger(code, frm=f"Agence du Parc <{BOX}>"),  # nos propres envois (boucle)
        _seloger(code, frm="Mail Delivery Subsystem <mailer-daemon@googlemail.com>"),  # retour d'erreur
        _seloger(code, extra="X-Autoreply: yes\n"),  # réponse d'absence
    ]
    ids = (tenant.id, shop.id, free.id)
    report = await _run(db_session, raws)
    assert report["leads"] == 1 and report["ignored"] == 7
    assert len(await _leads(db_session, ids[0])) == 1
    assert await _leads(db_session, ids[1]) == [] and await _leads(db_session, ids[2]) == []


@pytest.mark.asyncio
async def test_monthly_cap_and_test_emails(db_session, monkeypatch):
    tenant = await _agency(db_session, auto=True)

    async def tiny(db, t):
        return 1

    monkeypatch.setattr(leads, "quota", tiny)
    code, tenant_email = tenant.lead_inbox_code, tenant.email
    test = _seloger(code, frm=f"SeLoger (test Bob) <{BOX}>", reply_to=tenant_email,
                    extra=f"X-Bob-Test: {leads.test_token(code)}\n",
                    body=f"Réf. MAND-1234\nNom : Claire Martin (test)\nEmail : {tenant_email}\nMessage : Toujours disponible ?")
    outbox = Outbox()
    await _run(db_session, [test, _seloger(code), _seloger(code)], outbox=outbox)
    rows = await _leads(db_session, tenant)
    assert [r.is_test for r in rows] == [True, False, False]
    assert rows[0].status == "SENT" and rows[0].contact_email == tenant_email.lower() and rows[0].customer_id is None
    assert rows[1].status == "SENT" and rows[2].status == "TO_HANDLE" and "Plafond" in rows[2].reason
    assert [m["To"] for m in outbox.sent] == [tenant_email.lower(), "claire.martin@exemple.fr"]
    forged = _seloger(code, frm=f"x <{BOX}>", extra="X-Bob-Test: faux\n")
    assert (await _run(db_session, [forged]))["ignored"] == 1


@pytest.mark.asyncio
async def test_gmail_code_is_kept_for_the_agency(db_session):
    tenant = await _agency(db_session)
    raw = (f"From: Gmail Team <forwarding-noreply@google.com>\nTo: bob.contacts+{tenant.lead_inbox_code}@gmail.com\n"
           "Subject: (#987654321) Gmail Forwarding Confirmation\n\nCode de confirmation : 987654321\n").encode()
    assert (await _run(db_session, [raw]))["gmail_codes"] == 1
    tenant_id = tenant.id
    db_session.expire_all()
    assert (await db_session.get(Tenant, tenant_id)).lead_gmail_code == "987654321"


@pytest.mark.asyncio
async def test_failed_send_is_kept_and_purge_after_a_year(db_session):
    tenant = await _agency(db_session, auto=True)
    await _run(db_session, [_seloger(tenant.lead_inbox_code)], outbox=Outbox(fail=True))
    lead = (await _leads(db_session, tenant))[0]
    assert lead.status == "FAILED" and "OSError" in lead.reason
    lead.received_at = datetime.now(timezone.utc) - timedelta(days=400)
    await db_session.commit()
    assert await leads.purge(db_session) == 1


@pytest.mark.asyncio
async def test_nothing_without_the_mailbox_settings(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "leads_smtp_password", "")
    tenant = await _agency(db_session)
    assert (await _run(db_session, [_seloger(tenant.lead_inbox_code)]))["leads"] == 0


# --- API -----------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_settings_list_edit_send_ignore(client, db_session, monkeypatch):
    tenant = await _agency(db_session, code=None)
    tenant.lead_inbox_code = None
    tenant_id, tenant_email = tenant.id, tenant.email
    await db_session.commit()
    headers = await _headers(client, tenant)
    s = (await client.get("/api/v1/leads/settings", headers=headers)).json()
    assert s["available"] and s["configured"] and s["auto_send"] is False and s["quota"] == 5000 and s["whatsapp_connected"]
    assert s["address"].startswith("bob.contacts+agence-du-parc-") and s["address"].endswith("@gmail.com")
    assert (await client.put("/api/v1/leads/settings", headers=headers, json={"auto_send": True})).json() == {"auto_send": True}
    await client.put("/api/v1/leads/settings", headers=headers, json={"auto_send": False})

    db_session.expire_all()
    code = (await db_session.get(Tenant, tenant_id)).lead_inbox_code
    await _run(db_session, [_seloger(code), _seloger(code)])
    rows = (await client.get("/api/v1/leads", headers=headers)).json()
    assert len(rows) == 2 and rows[0]["status"] == "DRAFT" and rows[0]["product_name"] == "T3 lumineux rue Garibaldi"
    assert len((await client.get("/api/v1/leads?status=SENT", headers=headers)).json()) == 0
    lead_id = rows[0]["id"]
    edited = await client.put(f"/api/v1/leads/{lead_id}", headers=headers, json={"reply_subject": "Votre visite", "reply_text": "Bonjour, oui !"})
    assert edited.json()["reply_text"] == "Bonjour, oui !"
    outbox = Outbox()
    monkeypatch.setattr(leads, "smtp_send", outbox)
    sent = await client.post(f"/api/v1/leads/{lead_id}/send", headers=headers)
    assert sent.status_code == 200 and sent.json()["status"] == "SENT" and sent.json()["sent_by"] == tenant_email
    assert outbox.sent[0]["Subject"] == "Votre visite" and outbox.sent[0].get_body(("plain",)).get_content().strip() == "Bonjour, oui !"
    assert (await client.post(f"/api/v1/leads/{lead_id}/send", headers=headers)).status_code == 409
    assert (await client.post(f"/api/v1/leads/{rows[1]['id']}/ignore", headers=headers)).json()["status"] == "IGNORED"
    assert (await client.post(f"/api/v1/leads/{rows[1]['id']}/send", headers=headers)).status_code == 409
    stats = (await client.get("/api/v1/leads/settings", headers=headers)).json()
    assert stats["by_source"] == [{"label": "SeLoger", "count": 2}] and stats["month_used"] == 2


@pytest.mark.asyncio
async def test_api_is_limited_to_the_agency_and_paid_plans(client, db_session):
    free = await _agency(db_session, plan=TenantPlan.FREE)
    assert (await client.get("/api/v1/leads/settings", headers=await _headers(client, free))).json() == {
        "available": False, "message": leads.NOT_AVAILABLE}
    assert (await client.get("/api/v1/leads", headers=await _headers(client, free))).status_code == 403
    shop = await _agency(db_session, business_type=ONLINE_STORE)
    assert (await client.get("/api/v1/leads/settings", headers=await _headers(client, shop))).status_code == 403
    a, b = await _agency(db_session), await _agency(db_session)
    b_headers = await _headers(client, b)
    await _run(db_session, [_seloger(a.lead_inbox_code)])
    lead_id = (await _leads(db_session, a))[0].id
    assert (await client.post(f"/api/v1/leads/{lead_id}/ignore", headers=b_headers)).status_code == 404


# --- Tableau de bord ------------------------------------------------------------------------------------------------

def test_dashboard_tab():
    assert 'data-tab="leads"' in HTML and 'id="tab-leads"' in HTML and 'if (name === "leads") loadLeads();' in HTML
    nav = HTML[HTML.index('data-tab="leads"') - 20:HTML.index('data-tab="leads"') + 200]
    assert 'class="estate-only"' in nav
    js = HTML[HTML.index("// ---- Lot 83"):HTML.index("// ---- Lot 81")]
    assert "bobConfirm(" in js and "confirm(" not in js.replace("bobConfirm(", "")
    assert "esc(l.reply_text)" in js and "esc(l.message)" in js and "jsArg(l.id)" in js and "esc(s.gmail_code)" in js
    assert "/api/v1/leads/test" in js and "lead-auto" in js and "Sinon, vous validez chaque réponse" in js


@pytest.mark.asyncio
async def test_sending_is_capped_per_hour(db_session, monkeypatch):
    monkeypatch.setattr(leads, "TENANT_HOURLY_LIMIT", 1)
    tenant = await _agency(db_session, auto=True)
    code = tenant.lead_inbox_code
    outbox = Outbox()
    await _run(db_session, [_seloger(code), _seloger(code)], outbox=outbox)
    rows = await _leads(db_session, tenant)
    assert [r.status for r in rows] == ["SENT", "DRAFT"] and len(outbox.sent) == 1  # la 2e attend : l'agent l'enverra
