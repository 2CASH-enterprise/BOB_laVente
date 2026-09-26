"""
Lot 19 — accueil du tableau de bord : chaque chiffre vérifié sur un cas précis, jamais inventé.
"""
import pytest
from sqlalchemy import select

from app.models.contact_point import ContactPoint
from app.models.conversation import ConversationStatus, MessageSender
from app.models.message_signal import MessageSignal
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.services.handoff_rules import RULE_LABELS
from app.services.home_service import home_summary
from app.services.opportunity_service import sales_summary
from app.tests.test_sales_opportunities import NOW, _headers, _shop, ago


def paid_order(shop, customer, conversation, created, paid, total, created_by="IA"):
    order = Order(tenant_id=shop.tenant.id, customer_id=customer.id,
                  conversation_id=conversation.id if conversation else None, status=OrderStatus.PAID,
                  total_amount=total, currency="XOF", created_at=created, paid_at=paid, created_by=created_by)
    shop.db.add(order)
    return order


async def summary(shop, **kw):
    await shop.compute()
    return await home_summary(shop.db, shop.tenant.id, now=NOW, **kw)


# --- Indicateurs et évolution --------------------------------------------------------------

@pytest.mark.asyncio
async def test_paid_amount_is_compared_with_the_previous_30_days(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(40))
    paid_order(shop, customer, conv, ago(40), ago(39), 100000)   # période précédente
    shop.msg(conv, ago(5))
    paid_order(shop, customer, conv, ago(5), ago(4), 150000)     # période en cours

    s = await summary(shop)

    assert s["kpis"]["paid"] == {"value": 150000, "delta_pct": 50.0}


@pytest.mark.asyncio
async def test_no_previous_data_means_no_comparison_rather_than_a_fake_percentage(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(5))
    paid_order(shop, customer, conv, ago(5), ago(4), 150000)

    s = await summary(shop)

    assert s["kpis"]["paid"]["delta_pct"] is None
    assert s["kpis"]["conversations"]["delta_pct"] is None
    assert s["kpis"]["conversion"]["delta_points"] is None


@pytest.mark.asyncio
async def test_pending_amount_and_count(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(3))
    shop.order(customer, conv, ago(3), status=OrderStatus.PENDING, total=8000)
    shop.order(customer, conv, ago(2), status=OrderStatus.PENDING, total=2000)
    shop.order(customer, conv, ago(45), status=OrderStatus.PENDING, total=99999)  # hors période

    s = await summary(shop)

    assert s["kpis"]["pending"] == {"value": 10000, "count": 2}


@pytest.mark.asyncio
async def test_conversations_and_those_bob_handled_alone(db_session):
    shop = await _shop(db_session)
    _, a = await shop.customer("221700000001")
    _, b = await shop.customer("221700000002")
    _, c = await shop.customer("221700000003")
    shop.msg(a, ago(2))
    shop.msg(b, ago(3))
    shop.msg(b, ago(3, hours=-1), sender=MessageSender.SYSTEM, kind="handoff", content="Transfert vers un humain : x")
    shop.msg(c, ago(35))  # période précédente
    shop.msg(a, ago(2, hours=-1), sender=MessageSender.AI, content="Bonjour !")
    shop.msg(b, ago(3, hours=-2), sender=MessageSender.AI, content="Je transmets.")

    s = await summary(shop)

    assert s["kpis"]["conversations"] == {"value": 2, "delta_pct": 100.0, "bob_only": 1}
    assert s["ai_messages"] == 2


@pytest.mark.asyncio
async def test_conversion_matches_the_sales_card(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(10))
    paid_order(shop, customer, conv, ago(10), ago(9), 5000)

    s = await summary(shop)
    sales = await sales_summary(db_session, shop.tenant.id, now=NOW)

    assert s["kpis"]["conversion"]["value"] == sales["conversion_rate_pct"]
    assert [f["value"] for f in s["funnel"]] == [1, sales["total"], sales["with_order"], sales["paid"]]


# --- À traiter maintenant ------------------------------------------------------------------

async def _waiting(shop, number, transfer_content, when, kind="handoff", text="Vous acceptez les retours ?"):
    customer, conv = await shop.customer(number, first_name="Fatou", last_name="Koné")
    conv.status = ConversationStatus.WAITING_HUMAN
    shop.msg(conv, when, content=text)
    shop.msg(conv, when, sender=MessageSender.SYSTEM, kind=kind, content=transfer_content)
    return conv


@pytest.mark.asyncio
@pytest.mark.parametrize("content, kind, label", [
    (f"Transfert vers un humain : Règle — {RULE_LABELS['MISSING_CONDITIONS']}", "handoff", "Question transmise par Bob"),
    (f"Transfert vers un humain : Règle — {RULE_LABELS['PROMISE_KEPT']}", "handoff", "Bob a promis un suivi"),
    (f"Transfert vers un humain : Règle — {RULE_LABELS['HUMAN_REQUEST']}", "handoff", "Demande à parler à quelqu'un"),
    (f"Transfert vers un humain : Règle — {RULE_LABELS['REFUND']}", "handoff", "Demande de remboursement"),
    ("Négociation : offre 200 000, plancher 250 000", "negotiation_escalated", "Négociation sans accord"),
    ("Transfert vers un humain : Colis perdu — décision de Bob", "handoff", "Transmis par Bob"),
])
async def test_waiting_conversations_show_why(db_session, content, kind, label):
    shop = await _shop(db_session)
    conv = await _waiting(shop, "221700000011", content, ago(0, hours=1), kind=kind)

    [item] = (await summary(shop))["todo"]

    assert item["kind"] == "CONVERSATION" and item["conversation_id"] == str(conv.id)
    assert item["reason"] == label
    assert item["customer"] == "Fatou Koné"
    assert item["detail"] == "Vous acceptez les retours ?"


@pytest.mark.asyncio
async def test_long_customer_message_is_shortened(db_session):
    shop = await _shop(db_session)
    await _waiting(shop, "221700000012", "Transfert vers un humain : x", ago(0, hours=1), text="a" * 400)

    [item] = (await summary(shop))["todo"]

    assert len(item["detail"]) == 120 and item["detail"].endswith("…")


@pytest.mark.asyncio
async def test_orders_unpaid_for_seven_days_or_more(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer(first_name="Koffi")
    shop.msg(conv, ago(9))
    shop.order(customer, conv, ago(8), status=OrderStatus.PENDING, total=450000)
    shop.order(customer, conv, ago(6), status=OrderStatus.PENDING, total=1000)  # trop récente

    [item] = (await summary(shop))["todo"]

    assert item["kind"] == "ORDER"
    assert item["reason"] == "Non payée depuis 8 jours"
    assert item["detail"] == "Commande de 450 000 XOF"


async def _outage(shop, number, when):
    customer, conv = await shop.customer(number, first_name="Aminata")
    shop.msg(conv, when)
    shop.msg(conv, when, sender=MessageSender.SYSTEM, kind="ai_outage",
             content=f"Panne du service d'IA — {RULE_LABELS['AI_OUTAGE_CALLBACK']}")
    return conv


@pytest.mark.asyncio
async def test_customer_to_call_back_after_an_outage(db_session):
    shop = await _shop(db_session)
    conv = await _outage(shop, "221700000021", ago(0, hours=3))

    [item] = (await summary(shop))["todo"]

    assert item["kind"] == "CALLBACK" and item["conversation_id"] == str(conv.id)
    assert "+221700000021" in item["detail"]


@pytest.mark.asyncio
async def test_callback_disappears_once_a_human_answered_or_after_48_hours(db_session):
    shop = await _shop(db_session)
    answered = await _outage(shop, "221700000022", ago(0, hours=3))
    shop.msg(answered, ago(0, hours=2), sender=MessageSender.HUMAN, content="Je vous appelle")
    await _outage(shop, "221700000023", ago(3))
    retry_later_customer, retry = await shop.customer("221700000024")
    shop.msg(retry, ago(0, hours=1), sender=MessageSender.SYSTEM, kind="ai_outage",
             content=f"Panne du service d'IA — {RULE_LABELS['AI_OUTAGE_RETRY_LATER']}")

    assert (await summary(shop))["todo"] == []


@pytest.mark.asyncio
async def test_todo_order_conversations_first_oldest_first(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer("221700000031")
    shop.msg(conv, ago(10))
    shop.order(customer, conv, ago(9), status=OrderStatus.PENDING)
    recent = await _waiting(shop, "221700000032", "Transfert vers un humain : x", ago(0, hours=1))
    old = await _waiting(shop, "221700000033", "Transfert vers un humain : x", ago(0, hours=5))

    todo = (await summary(shop))["todo"]

    assert [t.get("conversation_id") for t in todo[:2]] == [str(old.id), str(recent.id)]
    assert todo[2]["kind"] == "ORDER"


# --- Par jour et activité ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_daily_series_marks_days_with_a_paid_sale(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer()
    shop.msg(conv, ago(2))
    shop.msg(conv, ago(2, hours=1))
    paid_order(shop, customer, conv, ago(2), ago(1), 5000)

    daily = (await summary(shop))["daily"]

    assert len(daily) == 30 and daily[-1]["date"] == NOW.date().isoformat()
    by_date = {d["date"]: d for d in daily}
    assert by_date[ago(2).date().isoformat()]["conversations"] == 1  # une conversation, même avec 2 messages
    assert by_date[ago(1).date().isoformat()]["sale"] is True
    assert sum(d["sale"] for d in daily) == 1


@pytest.mark.asyncio
async def test_recent_activity(db_session):
    shop = await _shop(db_session)
    cp = ContactPoint(tenant_id=shop.tenant.id, code="fb01", name="Page Facebook", greeting="Bonjour")
    db_session.add(cp)
    await db_session.flush()
    customer, conv = await shop.customer(first_name="Awa", acquisition_source="LINK", acquisition_contact_point_id=cp.id)
    customer.created_at = ago(0, hours=5)
    shop.msg(conv, ago(0, hours=5), content="Trop cher")
    paid_order(shop, customer, conv, ago(0, hours=4), ago(0, hours=1), 280000)
    shop.msg(conv, ago(0, hours=2), sender=MessageSender.SYSTEM, kind="handoff",
             content=f"Transfert vers un humain : Règle — {RULE_LABELS['MISSING_CONDITIONS']}")
    old_customer, _ = await shop.customer("221700000099")
    old_customer.created_at = ago(20)  # hors des 7 jours
    await db_session.commit()
    from app.models.conversation import Message

    message = (await db_session.execute(select(Message).where(Message.conversation_id == conv.id, Message.content == "Trop cher"))).scalar_one()
    db_session.add(MessageSignal(tenant_id=shop.tenant.id, conversation_id=conv.id, message_id=message.id,
                                 intents=["AUTRE"], objections=["PRIX_TROP_ELEVE"], model="t", taxonomy_version="v1.2",
                                 message_created_at=ago(0, hours=5), strategy="PRIX_ALTERNATIVE"))

    activity = (await summary(shop))["activity"]

    assert [(a["kind"], a["title"]) for a in activity] == [
        ("PAYMENT", "Paiement reçu"),
        ("TRANSFER", "Transmis à un humain"),
        ("ORDER", "Commande créée par Bob"),
        ("CUSTOMER", "Nouveau client"),
        ("OBJECTION", "Objection : prix trop élevé"),
    ]
    assert activity[0]["detail"] == "Awa · 280 000 XOF"
    assert activity[1]["detail"] == "Question transmise par Bob"
    assert activity[3]["detail"] == "Awa · Arrivé par le lien « Page Facebook »"
    assert activity[4]["detail"] == "Réponse de Bob : Alternative"


# --- Isolation et API --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_another_shop_is_never_counted(db_session):
    other = await _shop(db_session, email="autre@opp.sn")
    oc, oconv = await other.customer("221700000555")
    oconv.status = ConversationStatus.WAITING_HUMAN
    other.msg(oconv, ago(1))
    paid_order(other, oc, oconv, ago(1), ago(1), 999999)
    other.order(oc, oconv, ago(9), status=OrderStatus.PENDING)
    await other.compute()
    shop = await _shop(db_session, email="moi@opp.sn")

    s = await summary(shop)

    assert s["kpis"]["paid"]["value"] == 0 and s["kpis"]["conversations"]["value"] == 0
    assert s["todo"] == [] and s["activity"] == [] and s["waiting_humans"] == 0


@pytest.mark.asyncio
async def test_first_name_comes_from_the_connected_user(db_session):
    shop = await _shop(db_session)
    user = (await db_session.execute(select(User).where(User.tenant_id == shop.tenant.id))).scalar_one()
    user.full_name = "Awa Traoré"
    other = await _shop(db_session, email="x@opp.sn")
    stranger = (await db_session.execute(select(User).where(User.tenant_id == other.tenant.id))).scalar_one()

    assert (await summary(shop, user_id=user.id))["first_name"] == "Awa"
    assert (await summary(shop, user_id=stranger.id))["first_name"] is None


@pytest.mark.asyncio
async def test_api_requires_login_and_returns_the_summary(client, db_session, unique_email):
    shop = await _shop(db_session, unique_email)
    _, conv = await shop.customer()
    shop.msg(conv, ago(0, hours=1))
    await db_session.commit()

    assert (await client.get("/api/v1/analytics/home")).status_code == 401
    r = await client.get("/api/v1/analytics/home", headers=await _headers(client, unique_email))

    assert r.status_code == 200
    body = r.json()
    assert body["first_name"] == "U" and body["currency"] == "XOF" and len(body["daily"]) == 30
    assert body["kpis"]["conversations"]["value"] == 1
