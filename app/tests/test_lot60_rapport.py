"""
Lot 60 — rapport mensuel aux administrateurs (email avec graphiques, page « Rapports »), choix du 10/10 :
7 jours avant le renouvellement (avec le rappel d'échéance) ou en début de mois ; ce que Bob mesure séparé
de ce que le commerçant déclare ; conseil par règles fixes ; temps libéré en heures, jamais en argent.
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.appointment_request import AppointmentRequest
from app.models.audit_log import AuditLog
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import monthly_report, report_email
from app.services.business_type import CAR_DEALERSHIP, INSURANCE_BROKER, ONLINE_STORE

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
# Samedi 10 octobre 2026, minuit à Abidjan (UTC+0) : la période de 30 jours va du 10 septembre au 9 octobre inclus.
END = datetime(2026, 10, 10, tzinfo=timezone.utc)
START = END - timedelta(days=30)


def _function(name):
    body = HTML[HTML.index(f"function {name}("):]
    return body[:body.index("\n}\n") + 2]


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


async def _tenant(db, sector=ONLINE_STORE, name="Boutique Awa", **extra) -> Tenant:
    email = f"t{uuid.uuid4().hex[:8]}@l60.ci"
    tenant = Tenant(name=name, country="CI", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=sector,
                    business_type_chosen_at=at(1, 8, month=6), created_at=at(1, 8, month=6), **extra)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Awa Koné", role=Role.OWNER))
    await db.commit()
    return tenant


async def _talk(db, tenant, customer_at: datetime, ai_after: int | None = 9, number=None, source=None, created=None):
    """Un client écrit à customer_at ; Bob répond ai_after secondes plus tard (None : pas de réponse)."""
    customer = Customer(tenant_id=tenant.id, whatsapp_number=number or f"2250{uuid.uuid4().int % 10**9:09d}",
                        acquisition_source=source, created_at=created or customer_at)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.flush()
    db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER, content="Bonjour",
                   message_type="text", created_at=customer_at))
    if ai_after is not None:
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI, content="Bonjour !",
                       message_type="text", created_at=customer_at + timedelta(seconds=ai_after)))
    await db.commit()
    return customer, conversation


# --- Règles de base -----------------------------------------------------------------------------------------

def test_office_hours_and_formats():
    from zoneinfo import ZoneInfo

    abidjan, paris = ZoneInfo("Africa/Abidjan"), ZoneInfo("Europe/Paris")
    assert monthly_report.is_off_hours(at(9, 7, 59), abidjan) and not monthly_report.is_off_hours(at(9, 8), abidjan)
    assert not monthly_report.is_off_hours(at(9, 18, 59), abidjan) and monthly_report.is_off_hours(at(9, 19), abidjan)
    assert monthly_report.is_off_hours(at(10, 11), abidjan)  # samedi : tout le week-end est hors heures
    assert monthly_report.is_off_hours(at(9, 17, 30), paris)  # 19 h 30 à Paris
    assert monthly_report.number(1245000) == "1 245 000" and monthly_report.money(5000, "XOF") == "5 000 XOF"
    assert monthly_report.duration(1368) == "22 h 48" and monthly_report.duration(14) == "14 min"
    assert monthly_report.delta(142, 120) == "+18 %" and monthly_report.delta(80, 100) == "−20 %"
    assert monthly_report.delta(7, 5) == "+2" and monthly_report.delta(3, 5) == "−2" and monthly_report.delta(4, 0) == "+4"
    assert monthly_report.delta(0, 0) == "" and monthly_report.delta(5, None) == ""
    assert monthly_report.period_label(START, END, abidjan) == "10 septembre au 9 octobre 2026"
    assert monthly_report.period_label(at(15, 0, month=12) - timedelta(days=30), at(15, 0, month=12), abidjan) \
        == "15 novembre au 14 décembre 2026"


def test_report_window():
    tenant = Tenant(name="x")
    assert monthly_report.report_window(tenant, END) == (START, END)
    tenant.report_period_end = END - timedelta(days=24, hours=3)
    assert monthly_report.report_window(tenant, END) == (END - timedelta(days=24), END)
    tenant.report_period_end = END - timedelta(days=3)  # trop récent : 30 jours
    assert monthly_report.report_window(tenant, END)[0] == START
    tenant.report_period_end = END - timedelta(days=90)  # trop ancien : 30 jours
    assert monthly_report.report_window(tenant, END)[0] == START


def test_monthly_due_and_next_date():
    tenant = Tenant(name="x", country="CI", active=True, is_demo=False, created_at=at(1, 8, month=6))
    first = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    assert monthly_report.monthly_due(tenant, first)
    assert not monthly_report.monthly_due(tenant, datetime(2026, 11, 1, 7, tzinfo=timezone.utc))  # avant 8 h
    assert monthly_report.monthly_due(tenant, datetime(2026, 11, 3, 20, tzinfo=timezone.utc))  # rattrapage jusqu'au 3
    assert not monthly_report.monthly_due(tenant, datetime(2026, 11, 4, 9, tzinfo=timezone.utc))
    tenant.report_sent_at = datetime(2026, 10, 20, tzinfo=timezone.utc)  # rapport de renouvellement 12 jours avant
    assert not monthly_report.monthly_due(tenant, first)
    tenant.report_sent_at = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    assert monthly_report.monthly_due(tenant, first)
    tenant.paid_until = date(2026, 11, 12)  # échéance dans 11 jours : le rapport partira avec l'email d'échéance
    assert not monthly_report.monthly_due(tenant, first)
    assert monthly_report.next_send_on(tenant, first) == date(2026, 11, 5)
    tenant.paid_until = date(2027, 6, 30)  # abonnement long : rapport de début de mois
    assert monthly_report.monthly_due(tenant, first)
    assert monthly_report.next_send_on(tenant, datetime(2026, 11, 10, tzinfo=timezone.utc)) == date(2026, 12, 1)
    assert monthly_report.next_send_on(tenant, first) == date(2026, 11, 1)
    for change in ({"is_demo": True}, {"active": False}, {"created_at": datetime(2026, 10, 28, tzinfo=timezone.utc)}):
        other = Tenant(name="y", country="CI", active=True, is_demo=False, created_at=at(1, 8, month=6))
        for key, value in change.items():
            setattr(other, key, value)
        assert not monthly_report.monthly_due(other, first), change
    assert monthly_report.next_send_on(Tenant(name="d", active=True, is_demo=True), first) is None
    paused = Tenant(name="p", country="CI", active=True, is_demo=False, paid_until=date(2026, 10, 20))
    assert monthly_report.next_send_on(paused, first) is None and not monthly_report.monthly_due(paused, first)


def test_advice_rules():
    advice = monthly_report.advice
    assert "Aucun client ne vous a écrit" in advice([], [], 0, 0, 0, False, False)
    strategies = [("Venir voir / essayer", 12, 6, ""), ("Valoriser l'équipement", 8, 1, ""), ("Solutions", 6, 1, "")]
    text = advice(strategies, [], 50, 5, 40, True, False)
    assert text.startswith("« Venir voir / essayer » obtient un rendez-vous une fois sur deux")
    assert advice([("A", 4, 4, ""), ("B", 4, 0, "")], [], 50, 5, 40, True, False) is None  # trop peu d'utilisations
    assert advice([("A", 10, 3, ""), ("B", 10, 2, "")], [], 50, 5, 40, False, False) is None  # pas deux fois mieux
    assert "une vente" in advice([("A", 10, 4, ""), ("B", 10, 1, "")], [], 50, 5, 40, False, False)
    assert "une cotation ou un rendez-vous" in advice([("A", 10, 4, ""), ("B", 10, 1, "")], [], 50, 5, 40, True, True)
    assert advice([], [], 50, 20, 40, False, False).startswith("50 % de vos clients vous écrivent le soir")
    assert advice([], [], 50, 15, 40, False, False) is None


# --- Données du rapport ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_for_a_shop(db_session):
    tenant = await _tenant(db_session)
    # Période : 4 clients, dont 2 hors heures (soir, samedi), 1 importé (pas un nouveau client « Bob »).
    awa, conv = await _talk(db_session, tenant, at(8, 21), 7, source="DIRECT")
    await _talk(db_session, tenant, at(3, 10, month=10), 11, source="AD_FACEBOOK")
    await _talk(db_session, tenant, at(26, 11, month=9), 30, source="AD_FACEBOOK")  # samedi 26 septembre
    await _talk(db_session, tenant, at(1, 9), None, source="IMPORT")
    # Période précédente : 2 clients.
    await _talk(db_session, tenant, at(20, 10, month=8), 5)
    await _talk(db_session, tenant, at(25, 10, month=8), 5)
    # Avant la période (n'apparaît nulle part) et après (non plus).
    await _talk(db_session, tenant, at(1, 10, month=7), 5)
    db_session.add_all([
        Order(tenant_id=tenant.id, customer_id=awa.id, conversation_id=conv.id, status=OrderStatus.PAID, total_amount=25000,
              currency="XOF", paid_at=at(8, 22), created_at=at(8, 21, 30)),
        Order(tenant_id=tenant.id, customer_id=awa.id, status=OrderStatus.PAID, total_amount=10000, currency="XOF",
              paid_at=at(25, 12, month=8), created_at=at(25, 11, month=8)),
        Order(tenant_id=tenant.id, customer_id=awa.id, status=OrderStatus.PENDING, total_amount=8000, currency="XOF",
              created_at=at(20, 12, month=9)),
    ])
    await db_session.commit()

    d = await monthly_report.build(db_session, tenant, START, END)
    assert d["headline"] == "4 conversations, dont 3 réponses hors heures" and d["subject"] == "Votre mois avec Bob : " + d["headline"]
    assert d["period"] == "10 septembre au 9 octobre 2026"
    assert d["tiles"] == [("Conversations", "4", "+2", False), ("Nouveaux clients", "3", "+1", False),  # l'import n'est pas compté
                          ("Encaissé (XOF)", "25\u202f000", "+150 %", True), ("Commandes payées", "1", "+0", True)]
    night = d["night"]
    assert night["replies"] == 3  # 21 h, et deux samedis (tout le week-end est hors heures)
    assert night["stats"] == [(3, "clients ont écrit hors heures"), (3, "nouveaux clients arrivés hors heures"),
                              (2, "commandes passées hors heures")]  # 21 h 30 et un dimanche
    assert sum(night["hours"]) == 4 and night["hours"][21] == 1 and night["hours"][9] == 1
    bob = d["bob"]
    assert bob["presence"] == [("30 / 30", "jours de présence, dont 8 de week-end"), ("24 h / 24", "720 heures de présence"),
                               ("16 s", "pour répondre, en moyenne")]  # (7 + 11 + 30) / 3 ; l'import sans réponse ne compte pas
    assert bob["savings"] == [("Messages auxquels Bob a répondu seul", "3"), ("Temps de réponse évité (2 min par message)", "6 min")]
    assert bob["value"] == "6 min" and bob["compare"] == "pour la même présence 24 h / 24, il faudrait 4 personnes en relais"
    assert d["to_validate"]["items"] == [("1 commande", "en attente : si vous avez été payé, cliquez « Paiement reçu »"),
                                         ("1 commande", "en attente depuis plus de 7 jours : payées ou à annuler ?")]
    assert d["to_validate"]["url"].endswith("/dashboard/?tab=orders")
    assert d["funnel_title"] == "Vos ventes"
    assert d["funnel"][2:] == [("Commandes", 2, ""), ("Payées", 1, "25\u202f000\u00a0XOF déclarés · 1 en attente (8\u202f000\u00a0XOF)")]
    assert sum(d["daily"]) == 4 and len(d["daily"]) == 30 and d["daily_dates"][0] == 10 and d["daily_dates"][-1] == 9
    assert d["best_day"] == "samedi 26 septembre"
    assert d["weekend_days"] == {i for i in range(30) if (START + timedelta(days=i)).weekday() >= 5}
    assert [(label, n) for label, n, _ in d["sources"]] == [("Pub Facebook", 2), ("Direct", 1), ("Import", 1)]
    assert d["advice"].startswith("75 % de vos clients vous écrivent le soir ou le week-end")
    assert d["commercials"] == [] and d["renewal"] is None and d["url"].endswith("/dashboard/?tab=reports")


@pytest.mark.asyncio
async def test_build_for_a_dealership(db_session):
    tenant = await _tenant(db_session, CAR_DEALERSHIP, name="Auto <Prestige>")
    link = ContactPoint(tenant_id=tenant.id, code="AICHA", name="Aïcha", greeting="Bonjour", channel="COMMERCIAL",
                        owner_name="Aïcha <b>", owner_email="aicha@l60.ci", active=True)
    db_session.add(link)
    await db_session.commit()
    koffi, conv = await _talk(db_session, tenant, at(5, 20), 8)
    koffi.referred_contact_point_id = link.id
    db_session.add_all([
        AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=koffi.id, kind="ESSAI", availability="samedi",
                           status="CONFIRMED", scheduled_at=at(7, 10), outcome="SOLD", outcome_at=at(7, 12), created_at=at(5, 20, 5)),
        AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=koffi.id, kind="VISITE", availability="lundi",
                           status="CONFIRMED", scheduled_at=at(9, 10), created_at=at(6, 9)),  # passé, sans issue
        AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=koffi.id, kind="VISITE", availability="mardi",
                           status="REQUESTED", created_at=at(9, 15)),
    ])
    await db_session.commit()
    d = await monthly_report.build(db_session, tenant, START, END)
    labels = [t[0] for t in d["tiles"]]
    assert labels == ["Conversations", "Nouveaux clients", "Rendez-vous demandés", "Véhicules vendus"]
    assert d["tiles"][3][1] == "1" and d["tiles"][3][3] is True and d["tiles"][2][1] == "3"
    assert d["night"]["stats"][2] == (1, "rendez-vous demandés hors heures")
    assert ("Rendez-vous obtenus sans déplacement", "3 rendez-vous") in d["bob"]["savings"]
    assert d["to_validate"]["items"] == [("1 rendez-vous passé", "sans issue indiquée (vendu, à relancer, pas intéressé, absent)")]
    assert d["to_validate"]["url"].endswith("?tab=appointments")
    assert "1 rendez-vous à confirmer" in d["todo"]
    assert d["commercials"] == [("Aïcha <b>", 1, "3 rendez-vous · 1 vendu")]
    assert d["obtained_label"] == "Rendez-vous obtenus" and d["funnel_title"] == "Vos résultats"
    html = report_email.report({**d, "first_name": "Koffi <i>"})
    assert "Auto &lt;Prestige&gt;" in html and "Aïcha &lt;b&gt;" in html and "Koffi &lt;i&gt;" in html
    assert "<b>Prestige" not in html and "Aïcha <b>" not in html and "<script" not in html.lower()
    assert "Résultats par commercial" in html and "Pendant que vous étiez fermé" in html and "✍️ déclaré par vous" in html


@pytest.mark.asyncio
async def test_build_for_a_broker(db_session):
    from app.models.insurance_contract import InsuranceContract
    from app.models.quote_request import QuoteRequest

    tenant = await _tenant(db_session, INSURANCE_BROKER, name="Cabinet Kouassi")
    awa, conv = await _talk(db_session, tenant, at(6, 22), 6)
    db_session.add_all([
        QuoteRequest(tenant_id=tenant.id, customer_id=awa.id, conversation_id=conv.id, branch="AUTO", client_type="PARTICULIER",
                     status="WON", submitted_at=at(6, 22, 5), closed_at=at(8, 10)),
        QuoteRequest(tenant_id=tenant.id, customer_id=awa.id, branch="SANTE", client_type="PARTICULIER", status="HANDLED",
                     submitted_at=at(1, 10, month=8), handled_at=at(2, 10, month=8)),
        QuoteRequest(tenant_id=tenant.id, customer_id=awa.id, branch="HABITATION", client_type="PARTICULIER", status="SUBMITTED",
                     submitted_at=at(9, 10)),
        InsuranceContract(tenant_id=tenant.id, customer_id=awa.id, branch="AUTO", expires_on=date(2026, 10, 30), term="ANNUEL",
                          status="ACTIVE"),
    ])
    await db_session.commit()
    d = await monthly_report.build(db_session, tenant, START, END)
    assert [t[0] for t in d["tiles"]] == ["Conversations", "Demandes de cotation", "Rendez-vous et appels", "Contrats souscrits"]
    assert d["tiles"][1][1] == "2" and d["tiles"][3][1] == "1" and d["tiles"][3][3] is True
    assert d["night"]["stats"][2] == (1, "demandes de cotation transmises hors heures")
    assert d["to_validate"]["items"] == [("1 cotation", "sans suite depuis plus de 30 jours : souscrit ou perdu ?")]
    assert d["to_validate"]["url"].endswith("?tab=quotes")
    assert "1 contrat arrive à échéance dans les 30 prochains jours" in d["todo"]
    assert "1 demande de cotation à prendre en charge" in d["todo"]
    assert "votre cabinet est fermé." in d["night"]["sentence"] and "souscrire" in d["bob"]["sentence"]


@pytest.mark.asyncio
async def test_empty_period_and_new_shop(db_session):
    tenant = await _tenant(db_session)
    tenant.created_at = END - timedelta(days=40)  # pas de période précédente complète : pas de comparaison
    await db_session.commit()
    await _talk(db_session, tenant, at(1, 10), 5)
    d = await monthly_report.build(db_session, tenant, START, END)
    assert all(t[2] == "" for t in d["tiles"]) and d["tiles_sub"] == "Votre première période avec Bob"
    empty = await _tenant(db_session)
    d = await monthly_report.build(db_session, empty, START, END)
    assert d["subject"] == "Votre mois avec Bob" and d["advice"].startswith("Aucun client ne vous a écrit")
    html = report_email.report({**d, "first_name": None})
    assert "Pendant que vous étiez fermé" not in html and "jour par jour" not in html and "Bonjour," in html
    assert "✅ <b>Tout est à jour</b>" in html and "Voir le détail dans Bob" in html


@pytest.mark.asyncio
async def test_offer_reminder_and_waiting_humans(db_session):
    from app.models.followup_settings import TenantFollowupSettings

    tenant = await _tenant(db_session)
    _, conv = await _talk(db_session, tenant, at(9, 10), 5)
    conv.status = ConversationStatus.WAITING_HUMAN
    db_session.add(TenantFollowupSettings(tenant_id=tenant.id, offer_text="-10 %", offer_ends_on=date(2026, 9, 28)))
    await db_session.commit()
    d = await monthly_report.build(db_session, tenant, START, END)
    assert d["todo"] == ["1 client attend la réponse d'un humain",
                         "Votre offre du moment est terminée depuis le 28 septembre 2026 : pensez à en saisir une nouvelle"]


# --- Envoi --------------------------------------------------------------------------------------------------

class Mailbox:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, **mail):
        self.sent.append(mail)
        return self.ok


@pytest.mark.asyncio
async def test_recipients_are_owner_and_admins(db_session):
    tenant = await _tenant(db_session)
    for role, active, name in ((Role.ADMIN, True, "Serge Yao"), (Role.MANAGER, True, "M"), (Role.AGENT, True, "A"),
                               (Role.ADMIN, False, "Ancien")):
        db_session.add(User(tenant_id=tenant.id, email=f"{name.split()[0].lower()}@l60.ci", hashed_password="x",
                            full_name=name, role=role, active=active))
    await db_session.commit()
    people = await monthly_report.recipients(db_session, tenant)
    assert sorted(people) == sorted([(tenant.email, "Awa"), ("serge@l60.ci", "Serge")])
    tenant.email = "boutique@l60.ci"
    await db_session.commit()
    assert ("boutique@l60.ci", None) in await monthly_report.recipients(db_session, tenant)


@pytest.mark.asyncio
async def test_send_report_marks_the_period(db_session):
    tenant = await _tenant(db_session)
    await _talk(db_session, tenant, at(8, 21), 7)
    mailbox = Mailbox()
    assert await monthly_report.send_report(db_session, tenant, END, send=mailbox) == 1
    mail = mailbox.sent[0]
    assert mail["subject"].startswith("Votre mois avec Bob") and mail["from_name"] == "Bob"
    assert "Bonjour Awa," in mail["body"] and "PENDANT QUE VOUS ÉTIEZ FERMÉ" in mail["body"]
    assert mail["html"].startswith("<!DOCTYPE html>") and "Bonjour Awa," in mail["html"]
    assert tenant.report_sent_at == END and tenant.report_period_end == END
    # Envoi d'essai : à une seule personne, la date du prochain rapport ne bouge pas.
    tenant.report_sent_at = tenant.report_period_end = None
    assert await monthly_report.send_report(db_session, tenant, END, send=mailbox, only_to=("x@l60.ci", "X")) == 1
    assert mailbox.sent[-1]["to"] == "x@l60.ci" and tenant.report_sent_at is None
    # Échec d'envoi : rien n'est noté (il repartira).
    assert await monthly_report.send_report(db_session, tenant, END, send=Mailbox(ok=False)) == 0
    assert tenant.report_sent_at is None

    def boom(**mail):
        raise RuntimeError("SMTP")

    assert await monthly_report.send_report(db_session, tenant, END, send=boom) == 0


@pytest.mark.asyncio
async def test_renewal_report_replaces_the_warning_email(db_session):
    from app.workers.billing import check_billing

    tenant = await _tenant(db_session, name="Auto Abidjan", paid_until=date(2026, 10, 17))
    db_session.add(User(tenant_id=tenant.id, email="admin@l60.ci", hashed_password="x", full_name="Serge", role=Role.ADMIN))
    await db_session.commit()
    mailbox = Mailbox()
    assert await check_billing(db_session, END, mailbox) == [("Auto Abidjan", "WARNING")]
    assert sorted(m["to"] for m in mailbox.sent) == sorted([tenant.email, "admin@l60.ci"])
    for mail in mailbox.sent:
        assert mail["subject"] == "Votre mois avec Bob"
        assert "Votre abonnement se termine le 17 octobre 2026" in mail["html"]
        assert "sans renouvellement, Bob se mettra en pause le 19 octobre 2026" in mail["body"]
    fresh = (await db_session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
    assert fresh.billing_notice_stage == "WARNING" and fresh.report_sent_at is not None
    assert await check_billing(db_session, END + timedelta(hours=3), mailbox) == []  # une seule fois


@pytest.mark.asyncio
async def test_renewal_falls_back_to_the_plain_warning(db_session, monkeypatch):
    from app.workers.billing import check_billing

    await _tenant(db_session, name="Auto Abidjan", paid_until=date(2026, 10, 17))

    async def broken(*args, **kwargs):
        raise RuntimeError("calcul impossible")

    monkeypatch.setattr(monthly_report, "build", broken)
    mailbox = Mailbox()
    assert await check_billing(db_session, END, mailbox) == [("Auto Abidjan", "WARNING")]
    assert [m["subject"] for m in mailbox.sent] == ["Votre abonnement Bob se termine le 17 octobre 2026"]


@pytest.mark.asyncio
async def test_monthly_worker(db_session):
    from app.workers.reports import check_monthly_reports

    shop = await _tenant(db_session, name="Boutique Awa")
    await _tenant(db_session, name="Démo", is_demo=True)
    await _tenant(db_session, name="Bientôt", paid_until=date(2026, 11, 10))
    await _tenant(db_session, name="Suspendue", active=False)
    mailbox = Mailbox()
    first = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    assert await check_monthly_reports(db_session, first, mailbox) == ["Boutique Awa"]
    assert [m["to"] for m in mailbox.sent] == [shop.email]
    assert await check_monthly_reports(db_session, first + timedelta(hours=1), mailbox) == []
    assert await check_monthly_reports(db_session, datetime(2026, 12, 1, 9, tzinfo=timezone.utc), mailbox) == ["Boutique Awa"]
    fresh = (await db_session.execute(select(Tenant).where(Tenant.id == shop.id))).scalar_one()
    assert fresh.report_period_end.replace(tzinfo=timezone.utc) == datetime(2026, 12, 1, tzinfo=timezone.utc)  # minuit
    assert "du 1er novembre au 30 novembre 2026" in mailbox.sent[-1]["body"]  # reprend à la fin du rapport précédent


def test_worker_is_scheduled():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.reports" in TASK_MODULES
    assert celery_app.conf.beat_schedule["monthly-reports"]["task"] == "app.workers.reports.check_monthly_reports_task"


# --- API ----------------------------------------------------------------------------------------------------

async def _login(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _member(db, tenant, role):
    email = f"{role.value.lower()}{uuid.uuid4().hex[:6]}@l60.ci"
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Serge Yao", role=role))
    await db.commit()
    return email


@pytest.mark.asyncio
async def test_api_rights_preview_and_test_send(client, db_session, monkeypatch):
    tenant = await _tenant(db_session, name="Boutique <Awa>")
    owner = await _login(client, tenant.email)
    admin_email = await _member(db_session, tenant, Role.ADMIN)
    admin = await _login(client, admin_email)
    manager = await _login(client, await _member(db_session, tenant, Role.MANAGER))

    info = (await client.get("/api/v1/reports/info", headers=manager)).json()
    assert info["can_view"] is False and info["recipients"] == [] and info["next_send_on"]
    info = (await client.get("/api/v1/reports/info", headers=owner)).json()
    assert info["can_view"] is True and sorted(info["recipients"]) == sorted([tenant.email, admin_email])
    assert (await client.get("/api/v1/reports/preview", headers=manager)).status_code == 403
    assert (await client.post("/api/v1/reports/send-test", headers=manager)).status_code == 403
    assert (await client.get("/api/v1/reports/preview?period=7", headers=owner)).status_code == 422
    for period in ("30", "90", "since_last"):
        page = await client.get(f"/api/v1/reports/preview?period={period}", headers=admin)
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert "Bonjour Serge," in page.text and "Boutique &lt;Awa&gt;" in page.text and "Boutique <Awa>" not in page.text

    sent = []
    monkeypatch.setattr("app.services.email_service.send_email", lambda **mail: sent.append(mail) or True)
    for _ in range(3):
        r = await client.post("/api/v1/reports/send-test", headers=admin)
        assert r.status_code == 200 and r.json() == {"sent_to": admin_email}
    assert [m["to"] for m in sent] == [admin_email] * 3 and "html" in sent[0]
    limited = await client.post("/api/v1/reports/send-test", headers=admin)
    assert limited.status_code == 429 and "3 rapports d'essai" in limited.json()["detail"]
    fresh = (await db_session.execute(select(Tenant).where(Tenant.id == tenant.id))).scalar_one()
    assert fresh.report_sent_at is None  # un essai ne compte pas comme le rapport du mois
    assert len((await db_session.execute(select(AuditLog).where(AuditLog.action == "REPORT_TEST_SENT"))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_failed_test_send(client, db_session, monkeypatch):
    tenant = await _tenant(db_session)
    owner = await _login(client, tenant.email)
    monkeypatch.setattr("app.services.email_service.send_email", lambda **mail: False)
    r = await client.post("/api/v1/reports/send-test", headers=owner)
    assert r.status_code == 502 and "réessayez" in r.json()["detail"]


@pytest.mark.asyncio
async def test_sources_and_commercials_respect_the_end_of_the_period(db_session):
    from app.services.acquisition import commercials_summary, sources_summary

    tenant = await _tenant(db_session, CAR_DEALERSHIP)
    link = ContactPoint(tenant_id=tenant.id, code="S", name="Serge", greeting="Bonjour", channel="COMMERCIAL",
                        owner_name="Serge", owner_email="s@l60.ci", active=True)
    db_session.add(link)
    await db_session.commit()
    for when in (at(5, 10), at(10, 15)):  # dans la période, puis après sa fin
        customer, conv = await _talk(db_session, tenant, when, 5)
        customer.referred_contact_point_id = link.id
        db_session.add(AppointmentRequest(tenant_id=tenant.id, conversation_id=conv.id, customer_id=customer.id, kind="VISITE",
                                          availability="x", status="REQUESTED", created_at=when))
    await db_session.commit()
    assert (await sources_summary(db_session, tenant.id, START, END))["total_customers"] == 1
    assert (await sources_summary(db_session, tenant.id, START))["total_customers"] == 2
    rows = await commercials_summary(db_session, tenant.id, START, END)
    assert rows[0]["prospects"] == 1 and rows[0]["appointments"] == 1
    assert (await commercials_summary(db_session, tenant.id, START))[0]["prospects"] == 2


# --- Email : mise en page --------------------------------------------------------------------------------------

def test_email_layout_is_email_safe():
    source = (ROOT / "services" / "report_email.py").read_text(encoding="utf-8")
    assert "<script" not in source and "<svg" not in source and "<style" not in source  # retirés par Gmail / Outlook
    assert re.search(r"from html import escape as e", source)


def test_send_email_accepts_its_own_html():
    from app.services.email_service import build_message

    msg = build_message("a@b.ci", "Sujet", "Texte", html="<p>Rapport</p>")
    parts = [p.get_payload(decode=True).decode() for p in msg.get_payload()]
    assert parts == ["Texte", "<p>Rapport</p>"]
    default = build_message("a@b.ci", "Sujet", "Texte")
    assert "<!DOCTYPE html>" in default.get_payload()[1].get_payload(decode=True).decode()


# --- Tableau de bord ----------------------------------------------------------------------------------------

def test_dashboard_reports_page():
    assert 'data-tab="reports" data-label="Rapports"' in HTML and '<section id="tab-reports" class="hidden">' in HTML
    reports = HTML[HTML.index('<section id="tab-reports"'):HTML.index('<section id="tab-integrations"')]
    for el in ('id="report-card"', 'id="report-info"', 'id="report-preview"', 'id="report-send-btn"',
               "loadReportPreview('since_last')", "loadReportPreview('30')", "loadReportPreview('90')",
               'id="sources-card"', 'id="commercials-card"', 'id="signals-card"', 'id="strategies-card"', 'id="sales-card"'):
        assert el in reports, el
    customers = HTML[HTML.index('<section id="tab-customers"'):HTML.index('<section id="tab-products"')]
    assert 'id="sources-card"' not in customers and 'id="commercials-card"' not in customers
    preview = _function("loadReportPreview")
    assert 'sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"' in preview
    assert "allow-scripts" not in preview and "Authorization" in preview and "token" not in preview.split("fetch(")[1].split(",")[0]
    assert '<base target="_blank">' in preview
    show = _function("showTab")
    assert 'if (name === "reports") loadReports();' in show and "loadSources" not in show
    loads = _function("loadReports")
    for name in ("loadReportInfo()", "loadReportPreview(currentReportPeriod)", "loadAnalyticsGrid()", "loadSales()",
                 "loadSignals()", "loadStrategies()", "loadSources(currentSourcesPeriod)"):
        assert name in loads, name
    assert "loadSales" not in _function("loadOverview")
    assert "/api/v1/reports/send-test" in _function("sendTestReport")
    for name in ("currentReportPeriod", "reportInfo", "currentSourcesPeriod"):
        assert re.search(rf"^var {name} =", HTML, re.M), name  # utilisables dès le démarrage (lot 56)


# --- Cas limites (mutations) ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reply_delays_and_office_hours(db_session):
    tenant = await _tenant(db_session)
    _, conv = await _talk(db_session, tenant, at(6, 10), 10)  # mardi 10 h : réponse en heures de bureau
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conv.id, sender=MessageSender.AI, content="Autre chose ?",
                           message_type="text", created_at=at(6, 10, 5)))  # Bob qui enchaîne : pas une réponse au client
    await _talk(db_session, tenant, at(7, 10), 7200)  # répondu 2 h plus tard (panne) : pas compté dans le délai
    await db_session.commit()
    d = await monthly_report.build(db_session, tenant, START, END)
    assert d["night"]["replies"] == 0 and d["headline"] == "2 conversations"
    assert d["bob"]["presence"][2] == ("10 s", "pour répondre, en moyenne")
    assert d["bob"]["savings"][0] == ("Messages auxquels Bob a répondu seul", "3")


@pytest.mark.asyncio
async def test_presence_stops_with_the_pause(db_session):
    tenant = await _tenant(db_session, paid_until=date(2026, 10, 1))  # pause à partir du 3 octobre
    d = await monthly_report.build(db_session, tenant, START, END)
    assert d["bob"]["presence"][0] == ("23 / 30", "jours de présence, dont 6 de week-end")


@pytest.mark.asyncio
async def test_validation_and_todo_edge_cases(db_session):
    from app.models.followup_settings import TenantFollowupSettings

    shop = await _tenant(db_session)
    customer, _ = await _talk(db_session, shop, at(8, 10), 5)
    db_session.add(Order(tenant_id=shop.id, customer_id=customer.id, status=OrderStatus.PENDING, total_amount=5000, currency="XOF",
                         created_at=at(8, 11)))  # en attente depuis 2 jours seulement
    db_session.add(TenantFollowupSettings(tenant_id=shop.id, offer_text="-10 %", offer_ends_on=date(2026, 10, 31)))  # en cours
    await db_session.commit()
    d = await monthly_report.build(db_session, shop, START, END)
    assert [n for n, _ in d["to_validate"]["items"]] == ["1 commande"] and d["todo"] == []

    dealer = await _tenant(db_session, CAR_DEALERSHIP)
    customer, conv = await _talk(db_session, dealer, at(5, 10), 5)
    db_session.add(AppointmentRequest(tenant_id=dealer.id, conversation_id=conv.id, customer_id=customer.id, kind="VISITE",
                                      availability="x", status="CANCELLED", scheduled_at=at(7, 10), created_at=at(5, 10)))
    await db_session.commit()
    assert (await monthly_report.build(db_session, dealer, START, END))["to_validate"]["items"] == []  # annulé : rien à indiquer

    broker = await _tenant(db_session, INSURANCE_BROKER)
    from app.models.quote_request import QuoteRequest

    customer, conv = await _talk(db_session, broker, at(5, 10), 5)
    db_session.add_all([
        AppointmentRequest(tenant_id=broker.id, conversation_id=conv.id, customer_id=customer.id, kind="CABINET", availability="x",
                           status="CONFIRMED", scheduled_at=at(7, 10), outcome="SOLD", outcome_at=at(7, 11), created_at=at(5, 10)),
        QuoteRequest(tenant_id=broker.id, customer_id=customer.id, branch="AUTO", client_type="PARTICULIER", status="WON",
                     submitted_at=at(5, 11), closed_at=at(7, 12)),
    ])
    await db_session.commit()
    tiles = (await monthly_report.build(db_session, broker, START, END))["tiles"]
    assert tiles[3][:2] == ("Contrats souscrits", "1")  # le même client, compté une fois


def test_preview_windows_include_today():
    tenant = Tenant(name="x", country="CI")
    now = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)
    assert monthly_report.days_window(tenant, now, 30) == (START, END)
    assert monthly_report.days_window(tenant, now, 30, include_today=True) == (START + timedelta(days=1), END + timedelta(days=1))
    paris = Tenant(name="p", country="FR")
    assert monthly_report.days_window(paris, now, 7)[1] == datetime(2026, 10, 9, 22, tzinfo=timezone.utc)  # minuit à Paris


def test_dashboard_preview_does_not_jump_and_links_stay_inside():
    """Retour du 10/10 : la carte sautait au changement de période, et « Voir le détail » rechargeait l'aperçu."""
    preview = _function("loadReportPreview")
    assert 'box.classList.add("report-loading")' in preview and "if (!frame)" in preview  # l'aperçu reste en place
    assert "period !== currentReportPeriod" in preview  # le dernier choix gagne
    assert 'frame.contentDocument.addEventListener("click", reportLinkClicked)' in preview
    assert "body.scrollHeight" in _function("fitReportFrame")  # la hauteur ne grandit pas à chaque changement
    links = _function("reportLinkClicked")
    assert "event.preventDefault()" in links and 'getElementById("reports-details").scrollIntoView' in links
    assert "showTab(match[1])" in links and 'document.getElementById("tab-" + match[1])' in links
    assert 'id="reports-details">Analyses détaillées</h2>' in HTML and ".report-loading {" in HTML
