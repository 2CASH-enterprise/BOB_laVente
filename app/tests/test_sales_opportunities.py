import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.models.sales_opportunity import SalesOpportunity
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import opportunity_service
from app.services.opportunity_service import (
    opportunity_id,
    recompute_tenant_opportunities,
    sales_summary,
    split_into_episodes,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def ago(days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


# --- Fonctions pures -----------------------------------------------------------------

def test_split_into_episodes_on_seven_day_gaps():
    times = [ago(30), ago(29), ago(28), ago(20), ago(19.5), ago(2)]
    assert split_into_episodes(times) == [ago(30), ago(20), ago(2)]


def test_six_days_of_silence_stays_in_same_episode():
    assert split_into_episodes([ago(10), ago(4)]) == [ago(10)]


def test_exactly_seven_days_starts_a_new_episode():
    assert split_into_episodes([ago(10), ago(3)]) == [ago(10), ago(3)]


def test_opportunity_id_is_deterministic_and_naive_safe():
    conv = uuid.uuid4()
    aware = ago(5)
    assert opportunity_id(conv, aware) == opportunity_id(conv, aware.replace(tzinfo=None))
    assert opportunity_id(conv, aware) != opportunity_id(conv, ago(4))
    assert opportunity_id(conv, aware) != opportunity_id(uuid.uuid4(), aware)


# --- Mise en place -------------------------------------------------------------------

class Shop:
    def __init__(self, db, tenant):
        self.db = db
        self.tenant = tenant

    async def customer(self, number="221700000001", **fields):
        customer = Customer(tenant_id=self.tenant.id, whatsapp_number=number, **fields)
        self.db.add(customer)
        await self.db.flush()
        conversation = Conversation(tenant_id=self.tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
        self.db.add(conversation)
        await self.db.flush()
        return customer, conversation

    def msg(self, conversation, when, sender=MessageSender.CUSTOMER, kind="text", content="Bonjour"):
        self.db.add(Message(tenant_id=self.tenant.id, conversation_id=conversation.id, sender=sender,
                            message_type=kind, content=content, created_at=when))

    def order(self, customer, conversation, when, status=OrderStatus.PAID, total=10000):
        self.db.add(Order(tenant_id=self.tenant.id, customer_id=customer.id,
                          conversation_id=conversation.id if conversation else None,
                          status=status, total_amount=total, currency="XOF", created_at=when))

    async def compute(self):
        await self.db.commit()
        await recompute_tenant_opportunities(self.db, self.tenant.id, now=NOW)
        rows = (await self.db.execute(
            select(SalesOpportunity).where(SalesOpportunity.tenant_id == self.tenant.id)
            .order_by(SalesOpportunity.started_at).execution_options(populate_existing=True)
        )).scalars().all()
        return list(rows)


async def _shop(db_session, email="shop@opp.sn", plan=TenantPlan.INDEPENDANT) -> Shop:
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=plan)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    return Shop(db_session, tenant)


# --- Issues --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paid_order_gives_paid_outcome(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    shop.order(customer, conv, ago(3, hours=-1), status=OrderStatus.PAID, total=25000)
    [opp] = await shop.compute()
    assert opp.outcome == "PAID"
    assert float(opp.paid_amount) == 25000
    assert float(opp.order_amount) == 25000


@pytest.mark.asyncio
async def test_pending_order_gives_order_unpaid(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    shop.order(customer, conv, ago(3, hours=-1), status=OrderStatus.PENDING, total=8000)
    [opp] = await shop.compute()
    assert opp.outcome == "ORDER_UNPAID"
    assert float(opp.paid_amount) == 0


@pytest.mark.asyncio
async def test_cancelled_only_gives_cancelled_and_no_amount(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    shop.order(customer, conv, ago(3, hours=-1), status=OrderStatus.CANCELLED)
    [opp] = await shop.compute()
    assert opp.outcome == "CANCELLED"
    assert float(opp.order_amount) == 0


@pytest.mark.asyncio
async def test_paid_wins_over_cancelled_and_pending(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    shop.order(customer, conv, ago(3, hours=-1), status=OrderStatus.CANCELLED)
    shop.order(customer, conv, ago(3, hours=-2), status=OrderStatus.PENDING, total=5000)
    shop.order(customer, conv, ago(3, hours=-3), status=OrderStatus.PAID, total=7000)
    [opp] = await shop.compute()
    assert opp.outcome == "PAID"
    assert float(opp.order_amount) == 12000  # hors annulée
    assert float(opp.paid_amount) == 7000
    assert opp.order_count == 3


@pytest.mark.asyncio
async def test_transfer_by_bob_gives_transferred_with_reason(db_session):
    shop = await _shop(db_session)
    _, conv = await shop.customer()
    shop.msg(conv, ago(10))
    shop.msg(conv, ago(10, hours=-1), sender=MessageSender.SYSTEM, kind="handoff",
             content="Transfert vers un humain : Demande de remboursement")
    [opp] = await shop.compute()
    assert opp.outcome == "TRANSFERRED"
    assert opp.transfer_reason == "Demande de remboursement"


@pytest.mark.asyncio
async def test_negotiation_escalation_counts_as_transfer(db_session):
    shop = await _shop(db_session)
    _, conv = await shop.customer()
    shop.msg(conv, ago(1))
    shop.msg(conv, ago(1, hours=-1), sender=MessageSender.SYSTEM, kind="negotiation_escalated", content="Négociation transférée")
    [opp] = await shop.compute()
    assert opp.outcome == "TRANSFERRED"


@pytest.mark.asyncio
async def test_transfer_then_paid_order_is_paid(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(4))
    shop.msg(conv, ago(4, hours=-1), sender=MessageSender.SYSTEM, kind="handoff", content="Transfert vers un humain : Remise")
    shop.order(customer, conv, ago(4, hours=-5), status=OrderStatus.PAID)
    [opp] = await shop.compute()
    assert opp.outcome == "PAID"


@pytest.mark.asyncio
async def test_manual_takeover_is_not_a_bob_transfer(db_session):
    shop = await _shop(db_session)
    _, conv = await shop.customer()
    shop.msg(conv, ago(10))
    shop.msg(conv, ago(10, hours=-1), sender=MessageSender.SYSTEM, kind="takeover", content="Prise de contrôle")
    [opp] = await shop.compute()
    assert opp.outcome == "ABANDONED"


@pytest.mark.asyncio
async def test_silence_decides_between_abandoned_and_in_progress(db_session):
    shop = await _shop(db_session)
    _, silent = await shop.customer("221700000001")
    _, recent = await shop.customer("221700000002")
    shop.msg(silent, ago(8))
    shop.msg(silent, ago(8, hours=-1), sender=MessageSender.AI)
    shop.msg(recent, ago(2))
    rows = await shop.compute()
    outcomes = {r.conversation_id: r.outcome for r in rows}
    assert outcomes[silent.id] == "ABANDONED"
    assert outcomes[recent.id] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_bob_messages_do_not_keep_an_opportunity_alive(db_session):
    """Le silence se mesure sur les messages DU CLIENT : une relance de Bob ne compte pas."""
    shop = await _shop(db_session)
    _, conv = await shop.customer()
    shop.msg(conv, ago(9))
    shop.msg(conv, ago(1), sender=MessageSender.AI, kind="followup", content="Toujours intéressé ?")
    [opp] = await shop.compute()
    assert opp.outcome == "ABANDONED"
    assert opp.followup_sent is True


@pytest.mark.asyncio
async def test_conversation_without_customer_message_has_no_opportunity(db_session):
    shop = await _shop(db_session)
    _, conv = await shop.customer()
    shop.msg(conv, ago(2), sender=MessageSender.AI)
    assert await shop.compute() == []


# --- Découpage en plusieurs opportunités ---------------------------------------------

@pytest.mark.asyncio
async def test_two_episodes_each_with_their_own_outcome(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer(acquisition_source="QR")
    shop.msg(conv, ago(40))
    shop.msg(conv, ago(39))
    shop.msg(conv, ago(5))
    shop.order(customer, conv, ago(4), status=OrderStatus.PAID, total=9000)
    first, second = await shop.compute()

    assert first.outcome == "ABANDONED" and second.outcome == "PAID"
    assert first.customer_message_count == 2 and second.customer_message_count == 1
    assert first.is_first_opportunity and not second.is_first_opportunity
    assert first.source == "QR" and second.source is None  # la source ne vaut que pour le 1er contact
    assert float(second.paid_amount) == 9000


@pytest.mark.asyncio
async def test_returning_buyer_flag(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(40))
    shop.order(customer, conv, ago(39), status=OrderStatus.PAID)
    shop.msg(conv, ago(3))
    first, second = await shop.compute()
    assert first.is_returning_buyer is False
    assert second.is_returning_buyer is True


@pytest.mark.asyncio
async def test_context_uses_known_facts_only(db_session):
    shop = await _shop(db_session)
    cp = ContactPoint(tenant_id=shop.tenant.id, code="ctx12345", name="Page Facebook", greeting="Bonjour")
    db_session.add(cp)
    await db_session.flush()
    _, conv = await shop.customer(city="Abidjan", acquisition_source="LINK", acquisition_contact_point_id=cp.id)
    shop.msg(conv, ago(2))
    [opp] = await shop.compute()
    assert opp.city == "Abidjan"
    assert opp.source == "LINK"
    assert opp.contact_point_id == cp.id


# --- Recalcul : idempotence et stabilité ---------------------------------------------

@pytest.mark.asyncio
async def test_recompute_is_idempotent(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(20))
    shop.msg(conv, ago(3))
    shop.order(customer, conv, ago(3), status=OrderStatus.PENDING)
    first = [(r.id, r.outcome, r.started_at) for r in await shop.compute()]
    second = [(r.id, r.outcome, r.started_at) for r in await shop.compute()]
    assert first == second
    assert len(first) == 2


@pytest.mark.asyncio
async def test_old_opportunity_keeps_its_id_when_new_messages_arrive(db_session):
    """Indispensable pour les phases suivantes : ce qui s'y rattache ne doit jamais devenir orphelin."""
    shop = await _shop(db_session)
    _, conv = await shop.customer()
    shop.msg(conv, ago(30))
    [before] = await shop.compute()

    shop.msg(conv, ago(2))
    rows = await shop.compute()

    assert rows[0].id == before.id
    assert rows[0].outcome == "ABANDONED"
    assert len(rows) == 2


# --- Isolation -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recompute_only_touches_its_own_tenant(db_session):
    shop_a = await _shop(db_session, "a@opp.sn")
    shop_b = await _shop(db_session, "b@opp.sn")
    _, conv_a = await shop_a.customer()
    _, conv_b = await shop_b.customer()
    shop_a.msg(conv_a, ago(2))
    shop_b.msg(conv_b, ago(2))
    await shop_b.compute()

    await shop_a.compute()

    rows = (await db_session.execute(select(SalesOpportunity))).scalars().all()
    assert {r.tenant_id for r in rows} == {shop_a.tenant.id, shop_b.tenant.id}
    summary_a = await sales_summary(db_session, shop_a.tenant.id, now=NOW)
    assert summary_a["total"] == 1


@pytest.mark.asyncio
async def test_order_of_another_conversation_is_not_counted(db_session):
    shop = await _shop(db_session)
    customer_a, conv_a = await shop.customer("221700000001")
    _, conv_b = await shop.customer("221700000002")
    shop.msg(conv_a, ago(10))
    shop.msg(conv_b, ago(10))
    shop.order(customer_a, conv_a, ago(10), status=OrderStatus.PAID)
    rows = await shop.compute()
    outcomes = {r.conversation_id: r.outcome for r in rows}
    assert outcomes == {conv_a.id: "PAID", conv_b.id: "ABANDONED"}


# --- Résumé du dashboard --------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_conversion_excludes_in_progress(db_session):
    shop = await _shop(db_session)
    specs = [("PAID", ago(10)), ("PAID", ago(9)), ("ABANDONED", ago(12)), ("IN_PROGRESS", ago(1))]
    for i, (kind, when) in enumerate(specs):
        customer, conv = await shop.customer(f"2217000001{i:02d}")
        shop.msg(conv, when)
        if kind == "PAID":
            shop.order(customer, conv, when, status=OrderStatus.PAID, total=10000)
    await shop.compute()

    s = await sales_summary(db_session, shop.tenant.id, now=NOW)

    assert s["total"] == 4
    assert s["terminated"] == 3
    assert s["paid"] == 2
    assert s["conversion_rate_pct"] == 66.7
    assert s["revenue_paid"] == 20000
    assert s["currency"] == "XOF"


@pytest.mark.asyncio
async def test_summary_separates_paid_and_pending_revenue(db_session):
    shop = await _shop(db_session)
    c1, conv1 = await shop.customer("221700000001")
    c2, conv2 = await shop.customer("221700000002")
    shop.msg(conv1, ago(5))
    shop.msg(conv2, ago(5))
    shop.order(c1, conv1, ago(5), status=OrderStatus.PAID, total=15000)
    shop.order(c2, conv2, ago(5), status=OrderStatus.PENDING, total=4000)
    await shop.compute()

    s = await sales_summary(db_session, shop.tenant.id, now=NOW)

    assert s["revenue_paid"] == 15000
    assert s["revenue_pending"] == 4000
    assert s["with_order"] == 2


@pytest.mark.asyncio
async def test_summary_period_filter(db_session):
    shop = await _shop(db_session)
    _, old = await shop.customer("221700000001")
    _, recent = await shop.customer("221700000002")
    shop.msg(old, ago(45))
    shop.msg(recent, ago(5))
    await shop.compute()
    assert (await sales_summary(db_session, shop.tenant.id, days=30, now=NOW))["total"] == 1
    assert (await sales_summary(db_session, shop.tenant.id, days=60, now=NOW))["total"] == 2


@pytest.mark.asyncio
async def test_summary_by_source_with_contact_point_names(db_session):
    shop = await _shop(db_session)
    cp = ContactPoint(tenant_id=shop.tenant.id, code="src12345", name="Page Facebook", greeting="Bonjour")
    db_session.add(cp)
    await db_session.flush()
    c1, conv1 = await shop.customer("221700000001", acquisition_source="LINK", acquisition_contact_point_id=cp.id)
    _, conv2 = await shop.customer("221700000002", acquisition_source="QR")
    _, conv3 = await shop.customer("221700000003")
    shop.msg(conv1, ago(10))
    shop.order(c1, conv1, ago(10), status=OrderStatus.PAID)
    shop.msg(conv2, ago(10))
    shop.msg(conv3, ago(10))
    shop.msg(conv3, ago(2))  # 2e opportunité → « Clients déjà venus »
    await shop.compute()

    s = await sales_summary(db_session, shop.tenant.id, now=NOW)
    buckets = {b["label"]: b for b in s["by_source"]}

    assert buckets["Lien : Page Facebook"]["paid"] == 1
    assert buckets["Lien : Page Facebook"]["conversion_rate_pct"] == 100.0
    assert buckets["QR code"]["conversion_rate_pct"] == 0.0
    assert buckets["Direct"]["opportunities"] == 1
    assert buckets["Clients déjà venus"]["conversion_rate_pct"] is None  # en cours : pas encore de taux


@pytest.mark.asyncio
async def test_summary_empty_account(db_session):
    shop = await _shop(db_session)
    s = await sales_summary(db_session, shop.tenant.id, now=NOW)
    assert s["total"] == 0
    assert s["conversion_rate_pct"] is None
    assert s["computed_at"] is None
    assert s["by_source"] == []


# --- API -----------------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_api_refresh_then_read(client, db_session, unique_email):
    shop = await _shop(db_session, unique_email)
    _, conv = await shop.customer()
    shop.msg(conv, datetime.now(timezone.utc) - timedelta(days=2))
    await db_session.commit()
    headers = await _headers(client, unique_email)

    before = (await client.get("/api/v1/analytics/sales", headers=headers)).json()
    refreshed = await client.post("/api/v1/analytics/sales/refresh", headers=headers)
    after = (await client.get("/api/v1/analytics/sales", headers=headers)).json()

    assert before["computed_at"] is None and before["total"] == 0
    assert refreshed.status_code == 200 and refreshed.json()["total"] == 1
    assert after["computed_at"] is not None
    assert after["outcomes"]["IN_PROGRESS"] == 1


@pytest.mark.asyncio
async def test_api_requires_authentication(client):
    assert (await client.get("/api/v1/analytics/sales")).status_code == 401
    assert (await client.post("/api/v1/analytics/sales/refresh")).status_code == 401


@pytest.mark.asyncio
async def test_api_isolation_between_merchants(client, db_session):
    shop_a = await _shop(db_session, "a@opp-api.sn")
    await _shop(db_session, "b@opp-api.sn")
    _, conv = await shop_a.customer()
    shop_a.msg(conv, datetime.now(timezone.utc) - timedelta(days=1))
    await db_session.commit()

    await client.post("/api/v1/analytics/sales/refresh", headers=await _headers(client, "a@opp-api.sn"))
    seen_by_b = (await client.post("/api/v1/analytics/sales/refresh", headers=await _headers(client, "b@opp-api.sn"))).json()

    assert seen_by_b["total"] == 0


@pytest.mark.asyncio
async def test_mark_paid_records_paid_at(client, db_session, unique_email):
    shop = await _shop(db_session, unique_email)
    customer, conv = await shop.customer()
    order = Order(tenant_id=shop.tenant.id, customer_id=customer.id, conversation_id=conv.id,
                  status=OrderStatus.PENDING, total_amount=5000, currency="XOF")
    db_session.add(order)
    await db_session.commit()

    r = await client.put(f"/api/v1/orders/{order.id}/mark-paid", headers=await _headers(client, unique_email))

    assert r.status_code == 200
    refreshed = (await db_session.execute(select(Order).where(Order.id == order.id)
                                          .execution_options(populate_existing=True))).scalar_one()
    assert refreshed.paid_at is not None


# --- Tâche de nuit ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nightly_task_isolates_failures(db_session, monkeypatch):
    from app.workers import opportunities as worker

    shop_ok = await _shop(db_session, "ok@opp-task.sn")
    shop_ko = await _shop(db_session, "ko@opp-task.sn")
    _, conv = await shop_ok.customer()
    shop_ok.msg(conv, ago(2))
    await db_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    real = opportunity_service.recompute_tenant_opportunities

    async def flaky(db, tenant_id, now=None):
        if tenant_id == shop_ko.tenant.id:
            raise RuntimeError("panne simulée")
        return await real(db, tenant_id, now=now)

    monkeypatch.setattr(worker, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(worker, "recompute_tenant_opportunities", flaky)

    total = await worker._recompute_all_async()

    assert total == 1  # le tenant en panne n'a pas empêché le calcul de l'autre
