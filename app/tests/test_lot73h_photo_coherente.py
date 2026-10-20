"""
Lot 73 h — retour réel du 20/10 (MS4 Africa, moto InfinX Fatbike) : Bob proposait d'envoyer une photo alors que la carte
partait dans la même réponse, écrivait « Voici la photo » sans photo, puis « pas encore de photo, un conseiller vous
enverra les visuels » juste après l'avoir envoyée. Bob sait maintenant ce que le client a déjà vu, et le code accorde
son texte avec ce qui part vraiment.
"""
import pytest

from app.models.conversation import Message, MessageSender
from app.services.product_cards import photo_mentions_fixed
from app.tests.fakes import FakeLLMClient, text_response

DENIAL = ("Désolé, je n’ai pas encore de photo pour l’*InfinX Fatbike*. Un conseiller vous enverra les visuels et les "
          "détails techniques dès que possible.\n\nQuand seriez-vous disponible pour un essai ? 😊")
OFFER = ("Nous avons l’*InfinX Fatbike*, spécialement conçue pour les professionnels comme vous. Elle est disponible en "
         "stock.\n\nSouhaitez-vous que je vous envoie une photo ou des détails techniques ? 😊")
ANNOUNCE = "Voici la photo de l’*InfinX Fatbike* 😊.\n\nQuand seriez-vous disponible pour un essai ?"


def test_real_texts_of_the_20_10_are_fixed():
    assert photo_mentions_fixed(DENIAL, cards_going=False, photo_known=True) == "Quand seriez-vous disponible pour un essai ? 😊"
    assert photo_mentions_fixed(OFFER, cards_going=True, photo_known=False) == (
        "Nous avons l’*InfinX Fatbike*, spécialement conçue pour les professionnels comme vous. Elle est disponible en stock.")
    assert photo_mentions_fixed(ANNOUNCE, cards_going=False, photo_known=True) == "Quand seriez-vous disponible pour un essai ?"


def test_nothing_changes_when_it_is_true():
    # La carte part vraiment : « Voici la photo » reste ; un produit vraiment sans photo : Bob peut le dire.
    assert photo_mentions_fixed(ANNOUNCE, cards_going=True, photo_known=True) == ANNOUNCE
    no_photo = "Ce modèle n'a pas encore de photo. Il est disponible en rouge."
    assert photo_mentions_fixed(no_photo, cards_going=False, photo_known=False) == no_photo
    assert photo_mentions_fixed("Voici la photo.", cards_going=False, photo_known=True) == "Voici la photo."  # jamais vide


async def _with_card_shown(db_session, unique_email):
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    moto = _product(tenant, "FB1", name="InfinX Fatbike")
    db_session.add(moto)
    await db_session.flush()
    db_session.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI,
                           message_type="product_cards", content="🛍 Cartes envoyées : InfinX Fatbike",
                           message_metadata={"product_ids": [str(moto.id)], "image_urls": [moto.image_url]}))
    await db_session.commit()
    return tenant, conversation, moto


@pytest.mark.asyncio
async def test_bob_knows_the_photo_was_shown_and_never_denies_it(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed

    tenant, conversation, _ = await _with_card_shown(db_session, unique_email)
    outbox: list = []
    llm = FakeLLMClient([text_response(DENIAL)])
    reply, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                incoming_text="Okay", llm_client=llm, image_outbox=outbox)
    assert "PHOTOS DÉJÀ MONTRÉES À CE CLIENT" in llm.received_systems[0] and "InfinX Fatbike" in llm.received_systems[0]
    assert reply == "Quand seriez-vous disponible pour un essai ? 😊" and outbox == []


@pytest.mark.asyncio
async def test_interest_tap_gets_no_second_card_and_no_fake_voici_la_photo(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed

    tenant, conversation, _ = await _with_card_shown(db_session, unique_email)
    outbox: list = []
    llm = FakeLLMClient([text_response(ANNOUNCE)])
    reply, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                incoming_text="Ça m'intéresse : InfinX Fatbike", llm_client=llm,
                                                image_outbox=outbox)
    assert outbox == [] and reply == "Quand seriez-vous disponible pour un essai ?"


@pytest.mark.asyncio
async def test_first_presentation_sends_the_card_without_offering_a_photo(db_session, unique_email):
    from app.agents.orchestrator import generate_ai_reply_detailed
    from app.tests.test_product_images import _product, _shop

    tenant, conversation = await _shop(db_session, unique_email)
    db_session.add(_product(tenant, "FB1", name="InfinX Fatbike"))
    await db_session.commit()
    outbox: list = []
    llm = FakeLLMClient([text_response(OFFER)])
    reply, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                                incoming_text="Livraison", llm_client=llm, image_outbox=outbox)
    assert [i["name"] for i in outbox] == ["InfinX Fatbike"]
    assert "photo" not in reply.lower() and "Elle est disponible en stock." in reply
    assert "PHOTOS DÉJÀ MONTRÉES" not in llm.received_systems[0]
