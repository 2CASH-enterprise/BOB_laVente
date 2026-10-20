"""
Lot 73 g — retour réel du 19/10 (MS4 Africa) : Bob renvoyait la photo du produit à chaque réponse, même quand le
client ne la demandait pas. Une photo ou une carte déjà montrée dans la conversation ne repart pas pendant 24 h,
sauf si le client demande à la voir.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.tools import ToolExecutor
from app.models.conversation import Message, MessageSender
from app.tests.fakes import FakeLLMClient, text_response


async def _shown(db, tenant, conversation, product, hours_ago=0.2, kind="product_cards"):
    meta = {"product_ids": [str(product.id)]} if kind == "product_cards" else {"product_id": str(product.id)}
    db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI, message_type=kind,
                   content="🛍 Cartes envoyées", message_metadata=meta,
                   created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago)))
    await db.commit()


@pytest.mark.asyncio
async def test_follow_up_answers_naming_the_product_do_not_resend_its_card(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    pagne = _product(tenant, "P1", name="Pagne wax")
    db_session.add(pagne)
    await db_session.commit()
    await _shown(db_session, tenant, conversation, pagne)

    for question in ("C'est combien ?", "Vous livrez à Yopougon ?", "Il reste du stock ?"):
        outbox: list = []
        llm = FakeLLMClient([text_response("Le Pagne wax est disponible, livraison possible demain.")])
        await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                         incoming_text=question, llm_client=llm, image_outbox=outbox)
        assert outbox == [], question

    outbox = []
    llm = FakeLLMClient([text_response("Voici le Pagne wax.")])
    await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                     incoming_text="Je peux revoir la photo ?", llm_client=llm, image_outbox=outbox)
    assert [i["product_id"] for i in outbox] == [str(pagne.id)]


@pytest.mark.asyncio
async def test_after_24_hours_the_card_can_be_shown_again(db_session, unique_email):
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    sac = _product(tenant, "S1", name="Sac")
    db_session.add(sac)
    await db_session.commit()
    await _shown(db_session, tenant, conversation, sac, hours_ago=25, kind="image")
    executor = ToolExecutor(db_session, tenant.id, conversation)
    executor.incoming_text = "Vous avez des sacs ?"
    await executor.execute("send_product_images", {"product_ids": [str(sac.id)]})
    assert [i["product_id"] for i in executor.pending_images] == [str(sac.id)]


@pytest.mark.parametrize("message, wants", [
    ("Envoyez la photo", True), ("je peux voir ?", True), ("Montrez-moi les modèles", True), ("une image svp", True),
    ("Ça ressemble à quoi ?", True), ("C'est combien ?", False), ("Vous livrez ?", False), ("Je le veux", False),
    ("revoir le prix", False),
])
def test_what_counts_as_asking_to_see(message, wants):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.incoming_text = message
    assert executor.asks_to_see() is wants
