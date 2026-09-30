"""Lot 36 — issue du rendez-vous (concession) : vendu, à relancer, pas intéressé, absent ; relance unique."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.security import hash_password
from app.models.appointment_request import AppointmentRequest
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, Message, MessageSender
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import appointment_outcome
from app.services.appointment_outcome import OutcomeError, record_outcome
from app.services.business_type import CAR_DEALERSHIP
from app.services.local_time import tenant_zone

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # lundi midi à Dakar


async def _setup(db, email="c@example.com", customer_email=None, consent=False, wrote_hours_ago=None, scheduled=None):
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=CAR_DEALERSHIP)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    car = Product(tenant_id=tenant.id, sku=f"P{uuid.uuid4().hex[:5]}", name="Peugeot 5008", price=1, currency="XOF", stock_quantity=1)
    db.add(car)
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}", email=customer_email,
                        marketing_consent=consent)
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id)
    db.add(conversation)
    await db.flush()
    if wrote_hours_ago is not None:
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER, message_type="text",
                       content="Merci", created_at=NOW - timedelta(hours=wrote_hours_ago)))
    appointment = AppointmentRequest(tenant_id=tenant.id, conversation_id=conversation.id, customer_id=customer.id, product_id=car.id,
                                     kind="ESSAI", vehicle_label="Peugeot 5008", availability="x", status="CONFIRMED",
                                     scheduled_at=scheduled or NOW - timedelta(hours=2))
    db.add(appointment)
    await db.commit()
    return tenant, customer, car, appointment


class _Outbox:
    def __init__(self, ok=True):
        self.emails, self.whatsapp, self.ok = [], [], ok

    def email(self, **kw):
        self.emails.append(kw)
        return self.ok

    async def wa(self, db, tenant, conversation, customer, text):
        self.whatsapp.append(text)
        return self.ok


# --- Saisie de l'issue ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sold_makes_the_vehicle_unavailable(db_session):
    tenant, customer, car, appointment = await _setup(db_session)
    result = await record_outcome(db_session, tenant, appointment, "SOLD", "u1", now=NOW)
    assert result == {"vehicle_unavailable": True, "followup_channel": None}
    assert (appointment.outcome, appointment.outcome_by) == ("SOLD", "u1") and car.stock_quantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change, message", [
    ({"status": "CANCELLED"}, "confirmé"), ({"status": "REQUESTED"}, "confirmé"),
    ({"scheduled_at": NOW + timedelta(hours=3)}, "pas encore eu lieu"),
])
async def test_outcome_only_for_a_past_confirmed_appointment(db_session, change, message):
    tenant, customer, car, appointment = await _setup(db_session)
    for key, value in change.items():
        setattr(appointment, key, value)
    with pytest.raises(OutcomeError, match=message):
        await record_outcome(db_session, tenant, appointment, "SOLD", "u1", now=NOW)
    with pytest.raises(OutcomeError, match="inconnue"):
        await record_outcome(db_session, tenant, appointment, "PERDU", "u1", now=NOW)
    assert car.stock_quantity == 1


# --- Absent : relance immédiate, dans l'ordre WhatsApp → email → tâche -------------------------

@pytest.mark.asyncio
async def test_no_show_gets_whatsapp_when_the_conversation_is_open(db_session):
    tenant, customer, car, appointment = await _setup(db_session, customer_email="a@example.com", wrote_hours_ago=3)
    out = _Outbox()
    result = await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=NOW, send_email=out.email, send_whatsapp=out.wa)
    assert result["followup_channel"] == "WHATSAPP" and out.emails == []
    assert out.whatsapp == ["Bonjour, nous ne vous avons pas vu pour votre essai (Peugeot 5008) chez Auto Plus. "
                            "Souhaitez-vous choisir un autre créneau ? Répondez simplement à ce message."]
    assert car.stock_quantity == 1


@pytest.mark.asyncio
async def test_no_show_gets_an_email_when_whatsapp_is_closed_even_without_consent(db_session):
    tenant, customer, car, appointment = await _setup(db_session, customer_email="a@example.com", wrote_hours_ago=30)
    out = _Outbox()
    result = await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=NOW, send_email=out.email, send_whatsapp=out.wa)
    assert result["followup_channel"] == "EMAIL" and out.whatsapp == []
    assert out.emails[0]["to"] == "a@example.com" and out.emails[0]["from_name"] == "Auto Plus"


@pytest.mark.asyncio
async def test_no_show_becomes_a_task_without_whatsapp_nor_email(db_session):
    tenant, customer, car, appointment = await _setup(db_session, wrote_hours_ago=30)
    out = _Outbox()
    result = await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=NOW, send_email=out.email, send_whatsapp=out.wa)
    assert result["followup_channel"] == "TASK" and appointment.followup_sent_at == NOW
    assert appointment_outcome.open_tasks([appointment], NOW + timedelta(days=6)) == [appointment]
    assert appointment_outcome.open_tasks([appointment], NOW + timedelta(days=8)) == []


@pytest.mark.asyncio
async def test_refused_whatsapp_falls_back_to_email(db_session):
    tenant, customer, car, appointment = await _setup(db_session, customer_email="a@example.com", wrote_hours_ago=1)
    refused = _Outbox(ok=False)
    result = await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=NOW,
                                  send_email=_Outbox().email, send_whatsapp=refused.wa)
    assert result["followup_channel"] == "EMAIL"


@pytest.mark.asyncio
async def test_the_followup_is_never_sent_twice(db_session):
    tenant, customer, car, appointment = await _setup(db_session, customer_email="a@example.com", wrote_hours_ago=30)
    out = _Outbox()
    await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=NOW, send_email=out.email, send_whatsapp=out.wa)
    again = await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=NOW, send_email=out.email, send_whatsapp=out.wa)
    assert again["followup_channel"] is None and len(out.emails) == 1


# --- À relancer : deux jours après, entre 9 h et 19 h ; email seulement avec accord -----------

@pytest.mark.asyncio
async def test_follow_up_is_due_two_days_later_during_the_day(db_session):
    tenant, customer, car, appointment = await _setup(db_session)
    await record_outcome(db_session, tenant, appointment, "FOLLOW_UP", "u1", now=NOW)
    zone = tenant_zone(tenant)
    assert appointment.followup_sent_at is None
    assert not appointment_outcome.followup_due(appointment, NOW + timedelta(days=1), zone)
    assert appointment_outcome.followup_due(appointment, NOW + timedelta(days=2), zone)
    assert not appointment_outcome.followup_due(appointment, NOW + timedelta(days=2, hours=10), zone)  # 22 h à Dakar


@pytest.mark.asyncio
@pytest.mark.parametrize("consent, expected", [(True, "EMAIL"), (False, "TASK")])
async def test_follow_up_email_needs_the_offers_consent(db_session, consent, expected):
    tenant, customer, car, appointment = await _setup(db_session, customer_email="a@example.com", consent=consent, wrote_hours_ago=100)
    await record_outcome(db_session, tenant, appointment, "FOLLOW_UP", "u1", now=NOW)
    out = _Outbox()
    channel = await appointment_outcome.send_followup(db_session, tenant, appointment, NOW + timedelta(days=2),
                                                      send_email=out.email, send_whatsapp=out.wa)
    assert channel == expected and len(out.emails) == (1 if consent else 0)


@pytest.mark.asyncio
async def test_worker_sends_the_follow_up_once():
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        tenant, customer, car, appointment = await _setup(db, customer_email="a@example.com", consent=True, wrote_hours_ago=100)
        await record_outcome(db, tenant, appointment, "FOLLOW_UP", "u1", now=NOW)
        await db.commit()
    out = _Outbox()
    later = NOW + timedelta(days=2, minutes=5)
    first = await appointment_reminders.send_due_reminders(session_factory=factory, now=later, send=out.email, send_whatsapp=out.wa)
    second = await appointment_reminders.send_due_reminders(session_factory=factory, now=later, send=out.email, send_whatsapp=out.wa)
    assert first["followups"] == 1 and second["followups"] == 0
    assert [m["subject"] for m in out.emails] == ["Merci pour votre visite chez Auto Plus"]
    await engine.dispose()


# --- API, accueil, statistiques ----------------------------------------------------------------

async def _auth(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_outcome_api_and_appointment_page(client, db_session, unique_email, monkeypatch):
    monkeypatch.setattr("app.services.email_service.send_email", lambda **kw: True)
    tenant, customer, car, appointment = await _setup(db_session, email=unique_email, scheduled=datetime.now(timezone.utc) - timedelta(hours=2))
    headers = await _auth(client, unique_email)

    listed = (await client.get("/api/v1/appointments?view=closed", headers=headers)).json()["items"]
    assert listed[0]["can_set_outcome"] is True and listed[0]["outcome"] is None

    r = await client.post(f"/api/v1/appointments/{appointment.id}/outcome", json={"outcome": "SOLD"}, headers=headers)
    assert r.status_code == 200 and r.json()["vehicle_unavailable"] is True
    assert r.json()["appointment"]["outcome_label"] == "Venu, vendu"
    bad = await client.post(f"/api/v1/appointments/{appointment.id}/outcome", json={"outcome": "PERDU"}, headers=headers)
    assert bad.status_code == 422
    other = await client.post(f"/api/v1/appointments/{uuid.uuid4()}/outcome", json={"outcome": "SOLD"}, headers=headers)
    assert other.status_code == 404


@pytest.mark.asyncio
async def test_home_lists_past_appointments_without_outcome_and_callbacks(client, db_session, unique_email):
    tenant, customer, car, appointment = await _setup(db_session, email=unique_email, scheduled=datetime.now(timezone.utc) - timedelta(hours=3))
    headers = await _auth(client, unique_email)
    todo = (await client.get("/api/v1/analytics/home", headers=headers)).json()["todo"]
    assert any(t["kind"] == "OUTCOME" and t["customer"] == "1 rendez-vous passé" for t in todo)

    await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=datetime.now(timezone.utc),
                         send_email=_Outbox().email, send_whatsapp=_Outbox().wa)
    await db_session.commit()
    todo = (await client.get("/api/v1/analytics/home", headers=headers)).json()["todo"]
    assert not any(t["kind"] == "OUTCOME" for t in todo)
    [callback] = [t for t in todo if t["kind"] == "CALLBACK"]
    assert callback["detail"].startswith("Absent au rendez-vous : à rappeler au +")


@pytest.mark.asyncio
async def test_visits_and_sales_by_source_and_by_commercial(client, db_session, unique_email):
    tenant, customer, car, appointment = await _setup(db_session, email=unique_email, scheduled=datetime.now(timezone.utc) - timedelta(hours=3))
    cp = ContactPoint(tenant_id=tenant.id, code=f"m{uuid.uuid4().hex[:5]}", name="Lien Moussa", greeting="B", channel="COMMERCIAL",
                      owner_name="Moussa", owner_email="moussa@example.com")
    db_session.add(cp)
    await db_session.flush()
    customer.acquisition_source, customer.acquisition_contact_point_id, customer.referred_contact_point_id = "LINK", cp.id, cp.id
    await record_outcome(db_session, tenant, appointment, "SOLD", "u1", now=datetime.now(timezone.utc))
    await db_session.commit()
    headers = await _auth(client, unique_email)

    sources = (await client.get("/api/v1/analytics/sources?period=all", headers=headers)).json()
    [commercial_row] = [r for r in sources["channels"] if r["channel"] == "COMMERCIAL"]
    assert (commercial_row["appointments"], commercial_row["visits"], commercial_row["vehicles_sold"]) == (1, 1, 1)

    rows = (await client.get("/api/v1/analytics/commercials?period=all", headers=headers)).json()["rows"]
    assert rows == [{"name": "Moussa", "link": "Lien Moussa", "active": True, "prospects": 1, "appointments": 1,
                     "visits": 1, "vehicles_sold": 1, "conversion_pct": 100}]


@pytest.mark.asyncio
async def test_commercial_stats_are_for_dealerships_only(client, db_session, unique_email):
    db_session.add(Tenant(name="Shop", country="SN", currency="XOF", email=unique_email, plan=TenantPlan.PRO))
    await db_session.flush()
    shop = (await db_session.execute(select(Tenant).where(Tenant.email == unique_email))).scalar_one()
    db_session.add(User(tenant_id=shop.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    r = await client.get("/api/v1/analytics/commercials", headers=await _auth(client, unique_email))
    assert r.status_code == 403


def test_dashboard_outcome_ui_is_escaped():
    html = open("app/static/dashboard/index.html", encoding="utf-8").read()
    assert "${esc(a.outcome_label)}" in html and "setAppointmentOutcome(" in html and "${esc(r.name)}" in html
    assert "body:not(.dealership) .dealer-only { display: none !important; }" in html
    assert 'class="card dealer-only"' in html and "if (item.kind === \"OUTCOME\")" in html


@pytest.mark.asyncio
async def test_a_vehicle_of_another_shop_is_never_touched(db_session):
    tenant, customer, car, appointment = await _setup(db_session)
    other_tenant, _, other_car, _ = await _setup(db_session, email="autre@example.com")
    appointment.product_id = other_car.id
    result = await record_outcome(db_session, tenant, appointment, "SOLD", "u1", now=NOW)
    assert result["vehicle_unavailable"] is False and other_car.stock_quantity == 1


@pytest.mark.asyncio
async def test_a_no_show_is_not_a_visit(client, db_session, unique_email):
    tenant, customer, car, appointment = await _setup(db_session, email=unique_email, scheduled=datetime.now(timezone.utc) - timedelta(hours=3))
    await record_outcome(db_session, tenant, appointment, "NO_SHOW", "u1", now=datetime.now(timezone.utc),
                         send_email=_Outbox().email, send_whatsapp=_Outbox().wa)
    await db_session.commit()
    sources = (await client.get("/api/v1/analytics/sources?period=all", headers=await _auth(client, unique_email))).json()
    [row] = sources["channels"]
    assert (row["appointments"], row["visits"], row["vehicles_sold"]) == (1, 0, 0)


@pytest.mark.asyncio
async def test_worker_waits_for_daytime_to_follow_up():
    from app.workers import appointment_reminders

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        tenant, customer, car, appointment = await _setup(db, customer_email="a@example.com", consent=True, wrote_hours_ago=100)
        await record_outcome(db, tenant, appointment, "FOLLOW_UP", "u1", now=NOW)
        await db.commit()
    out = _Outbox()
    night = NOW + timedelta(days=2, hours=10)  # 22 h à Dakar
    report = await appointment_reminders.send_due_reminders(session_factory=factory, now=night, send=out.email, send_whatsapp=out.wa)
    assert report["followups"] == 0 and out.emails == []
    await engine.dispose()
