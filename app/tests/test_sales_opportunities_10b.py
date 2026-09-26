from datetime import timedelta

import pytest

from app.models.order import OrderStatus
from app.services.opportunity_service import sales_summary
from app.tests.test_sales_opportunities import NOW, _headers, _shop, ago


# --- Commandes saisies à la main (sans conversation) --------------------------------

@pytest.mark.asyncio
async def test_manual_order_while_customer_is_active_joins_the_opportunity(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    shop.order(customer, None, ago(2), status=OrderStatus.PAID, total=560000)  # saisie à la main

    [opp] = await shop.compute()

    assert opp.outcome == "PAID"
    assert float(opp.paid_amount) == 560000
    s = await sales_summary(db_session, shop.tenant.id, now=NOW)
    assert s["outside_conversation_orders"] == 0


@pytest.mark.asyncio
async def test_manual_order_long_after_last_message_stays_outside(db_session):
    """Jamais de réécriture rétroactive : un épisode abandonné ne devient pas « payé » des semaines plus tard."""
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(25))
    shop.order(customer, None, ago(5), status=OrderStatus.PAID, total=560000)

    [opp] = await shop.compute()

    assert opp.outcome == "ABANDONED"
    s = await sales_summary(db_session, shop.tenant.id, now=NOW)
    assert s["outside_conversation_orders"] == 1
    assert s["outside_conversation_paid"] == 560000
    assert s["revenue_paid"] == 560000  # la trésorerie, elle, compte cette vente


@pytest.mark.asyncio
async def test_manual_order_for_customer_without_conversation_is_outside(db_session):
    """Le cas réel du compte démo : 560 000 XOF payés, jusqu'ici invisibles dans la carte."""
    shop = await _shop(db_session)
    customer, _ = await shop.customer("221700000001")  # conversation sans aucun message du client
    shop.order(customer, None, ago(5), status=OrderStatus.PAID, total=560000)
    await shop.compute()

    s = await sales_summary(db_session, shop.tenant.id, now=NOW)

    assert s["revenue_paid"] == 560000
    assert s["outside_conversation_orders"] == 1
    assert s["total"] == 0  # aucune opportunité inventée


@pytest.mark.asyncio
async def test_manual_order_before_first_message_is_outside(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.order(customer, None, ago(6), status=OrderStatus.PAID)
    shop.msg(conv, ago(4))

    [opp] = await shop.compute()

    assert opp.outcome == "IN_PROGRESS"
    assert (await sales_summary(db_session, shop.tenant.id, now=NOW))["outside_conversation_orders"] == 1


@pytest.mark.asyncio
async def test_manual_order_goes_to_the_most_recent_matching_episode(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(30))
    shop.msg(conv, ago(3))
    shop.order(customer, None, ago(2), status=OrderStatus.PENDING, total=9000)

    first, second = await shop.compute()

    assert first.outcome == "ABANDONED"
    assert second.outcome == "ORDER_UNPAID"


# --- Trésorerie de la période ---------------------------------------------------------

@pytest.mark.asyncio
async def test_revenue_uses_payment_date_when_known(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(40))
    shop.order(customer, conv, ago(40), status=OrderStatus.PAID, total=7000)
    await db_session.flush()
    from sqlalchemy import select
    from app.models.order import Order
    order = (await db_session.execute(select(Order))).scalars().one()
    order.paid_at = ago(5)  # commande ancienne, payée récemment
    await shop.compute()

    s = await sales_summary(db_session, shop.tenant.id, now=NOW)

    assert s["revenue_paid"] == 7000


@pytest.mark.asyncio
async def test_old_orders_outside_period_are_not_counted(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(45))
    shop.order(customer, conv, ago(45), status=OrderStatus.PAID, total=1000)     # sans date de paiement
    shop.order(customer, conv, ago(44), status=OrderStatus.PENDING, total=2000)
    await shop.compute()

    s = await sales_summary(db_session, shop.tenant.id, now=NOW)

    assert s["revenue_paid"] == 0
    assert s["revenue_pending"] == 0


@pytest.mark.asyncio
async def test_pending_inside_paid_opportunity_is_explained(db_session):
    """Le cas réel : 560 000 encaissés et 1 680 000 en attente dans la MÊME opportunité payée."""
    shop = await _shop(db_session)
    customer, conv = await shop.customer("33744177430")
    shop.msg(conv, ago(5))
    shop.order(customer, conv, ago(5), status=OrderStatus.PENDING, total=1400000)
    shop.order(customer, conv, ago(4), status=OrderStatus.PENDING, total=280000)
    shop.order(customer, conv, ago(3), status=OrderStatus.PAID, total=280000)
    shop.order(customer, conv, ago(3), status=OrderStatus.PAID, total=280000)
    other, other_conv = await shop.customer("221700000009")
    shop.msg(other_conv, ago(2))
    shop.order(other, other_conv, ago(2), status=OrderStatus.PENDING, total=5000)  # opportunité non payée
    await shop.compute()

    s = await sales_summary(db_session, shop.tenant.id, now=NOW)

    assert s["revenue_paid"] == 560000
    assert s["revenue_pending"] == 1685000
    assert s["revenue_pending_in_paid_opportunities"] == 1680000  # sans les 5 000 de l'autre client
    assert s["outcomes"]["PAID"] == 1 and s["outcomes"]["ORDER_UNPAID"] == 1


@pytest.mark.asyncio
async def test_cash_summary_is_isolated_per_merchant(db_session):
    shop_a = await _shop(db_session, "a@cash.sn")
    shop_b = await _shop(db_session, "b@cash.sn")
    customer_b, _ = await shop_b.customer()
    shop_b.order(customer_b, None, ago(3), status=OrderStatus.PAID, total=99000)
    await shop_a.compute()
    await shop_b.compute()

    s = await sales_summary(db_session, shop_a.tenant.id, now=NOW)

    assert s["revenue_paid"] == 0
    assert s["outside_conversation_orders"] == 0


# --- Liste des commandes : dates transmises au dashboard -----------------------------

@pytest.mark.asyncio
async def test_orders_list_exposes_dates(client, db_session, unique_email):
    from datetime import datetime, timezone

    shop = await _shop(db_session, unique_email)
    customer, conv = await shop.customer()
    shop.order(customer, conv, datetime.now(timezone.utc) - timedelta(days=9), status=OrderStatus.PENDING)
    await db_session.commit()

    r = await client.get("/api/v1/orders", headers=await _headers(client, unique_email))

    assert r.status_code == 200
    [order] = r.json()
    assert order["created_at"] is not None
    assert order["paid_at"] is None
