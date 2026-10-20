"""
Lot 73 i — « nouvelle visite » : un client qui revient après 6 h ou plus est accueilli comme tel ; Bob ne reprend pas
l'ancien sujet de lui-même et les photos déjà vues lors d'une visite précédente peuvent repartir.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.tools import ToolExecutor
from app.models.conversation import Message, MessageSender
from app.services import visits
from app.tests.fakes import FakeLLMClient, text_response

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _msg(hours_ago, sender=MessageSender.CUSTOMER, kind="text"):
    return Message(sender=sender, content="…", message_type=kind, created_at=NOW - timedelta(hours=hours_ago))


def test_visit_boundaries():
    # Premier message : pas une « nouvelle visite »
    assert visits.visit_info([], NOW)[0] is False
    # Dernier message il y a 2 h : même visite, commencée après la dernière pause de 6 h ou plus
    new, gap, start = visits.visit_info([_msg(30), _msg(29), _msg(3), _msg(2)], NOW)
    assert (new, gap, start) == (False, timedelta(hours=2), NOW - timedelta(hours=3))
    # Retour après 7 h : nouvelle visite qui commence maintenant
    new, gap, start = visits.visit_info([_msg(9), _msg(7)], NOW)
    assert new is True and gap == timedelta(hours=7) and start == NOW
    # Exactement 6 h : nouvelle visite ; 5 h 59 : non
    assert visits.visit_info([_msg(6)], NOW)[0] is True
    assert visits.visit_info([_msg(5.99)], NOW)[0] is False
    # Les échanges repris du téléphone (sans date) ne comptent pas
    assert visits.visit_info([Message(sender=MessageSender.CUSTOMER, content="x", message_type="phone_history")], NOW)[0] is False


@pytest.mark.parametrize("hours, text", [(7, "7 heures"), (26, "un jour"), (60, "2 jours"), (24 * 90, "plusieurs mois")])
def test_elapsed_time_in_words(hours, text):
    assert visits.elapsed_text(timedelta(hours=hours)) == text


async def _setup(db_session, unique_email, shown_hours_ago):
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    moto = _product(tenant, "FB1", name="InfinX Fatbike")
    db_session.add(moto)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    old = [Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                   content="Je cherche une moto", created_at=now - timedelta(hours=shown_hours_ago + 0.1)),
           Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI,
                   content="Quand seriez-vous disponible pour un essai ?", created_at=now - timedelta(hours=shown_hours_ago))]
    db_session.add_all(old + [Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI,
                                      message_type="product_cards", content="🛍 Cartes envoyées : InfinX Fatbike",
                                      message_metadata={"product_ids": [str(moto.id)]},
                                      created_at=now - timedelta(hours=shown_hours_ago))])
    await db_session.commit()
    return tenant, conversation, moto, old


@pytest.mark.asyncio
async def test_returning_the_next_morning_is_a_new_visit(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed

    tenant, conversation, moto, old = await _setup(db_session, unique_email, shown_hours_ago=18)
    outbox: list = []
    llm = FakeLLMClient([text_response("Bonjour et bon retour ! La InfinX Fatbike est toujours en stock.")])
    await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=old,
                                     incoming_text="Elle est toujours dispo ?", llm_client=llm, image_outbox=outbox)
    system = llm.received_systems[0]
    assert "NOUVELLE VISITE : ce client revient après 18 heures" in system
    assert "PHOTOS DÉJÀ MONTRÉES" not in system  # vue lors de la visite précédente
    assert [i["product_id"] for i in outbox] == [str(moto.id)]  # la carte peut repartir


@pytest.mark.asyncio
async def test_within_the_same_visit_nothing_changes(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed

    tenant, conversation, _, old = await _setup(db_session, unique_email, shown_hours_ago=1)
    outbox: list = []
    llm = FakeLLMClient([text_response("La InfinX Fatbike est toujours en stock.")])
    await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=old,
                                     incoming_text="Elle est toujours dispo ?", llm_client=llm, image_outbox=outbox)
    system = llm.received_systems[0]
    assert "NOUVELLE VISITE" not in system and "PHOTOS DÉJÀ MONTRÉES À CE CLIENT" in system
    assert outbox == []


@pytest.mark.asyncio
async def test_executor_uses_the_visit_start(db_session, unique_email):
    tenant, conversation, moto, _ = await _setup(db_session, unique_email, shown_hours_ago=3)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    executor.incoming_text = "C'est combien ?"
    executor.visit_start = datetime.now(timezone.utc) - timedelta(hours=2)  # visite commencée après la carte
    await executor.execute("send_product_images", {"product_ids": [str(moto.id)]})
    assert [i["product_id"] for i in executor.pending_images] == [str(moto.id)]
    assert await executor.shown_photo_names() == []
