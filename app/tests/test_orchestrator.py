import pytest

from app.agents.orchestrator import FALLBACK_MESSAGE, generate_ai_reply
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import Role, User
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique Test", country="SN", currency="XOF", email=email)
    db_session.add(tenant)
    await db_session.flush()

    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    product = Product(tenant_id=tenant.id, sku="SAM-A56", name="Samsung A56", price=280000, currency="XOF", stock_quantity=12)
    db_session.add(product)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(tenant)
    await db_session.refresh(product)
    await db_session.refresh(conversation)
    return tenant, product, conversation


@pytest.mark.asyncio
async def test_direct_text_reply_without_tool_use(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    fake = FakeLLMClient([text_response("Bonjour, comment puis-je vous aider ?")])

    reply = await generate_ai_reply(
        db=db_session, tenant=tenant, conversation=conversation, history=[], incoming_text="Bonjour", llm_client=fake
    )
    assert reply == "Bonjour, comment puis-je vous aider ?"
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_tool_use_round_trip_then_final_answer(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    fake = FakeLLMClient(
        [
            tool_use_response("search_products", {"query": "Samsung"}),
            text_response("Nous avons le Samsung A56 à 280 000 XOF, disponible."),
        ]
    )

    reply = await generate_ai_reply(
        db=db_session,
        tenant=tenant,
        conversation=conversation,
        history=[],
        incoming_text="Avez-vous un Samsung ?",
        llm_client=fake,
    )
    assert "280 000" in reply
    assert fake.call_count == 2

    # Le deuxième appel au LLM doit contenir le résultat RÉEL de l'outil (pas une donnée inventée)
    second_call_messages = fake.received_messages[1]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert str(product.id) in tool_result_content
    assert "280000.0" in tool_result_content


@pytest.mark.asyncio
async def test_handoff_tool_changes_conversation_status_via_orchestrator(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    fake = FakeLLMClient(
        [
            tool_use_response("handoff_to_human", {"reason": "Négociation hors limites"}),
            text_response("Je vous mets en relation avec un conseiller."),
        ]
    )

    reply = await generate_ai_reply(
        db=db_session,
        tenant=tenant,
        conversation=conversation,
        history=[],
        incoming_text="Je veux 50% de réduction",
        llm_client=fake,
    )
    assert "conseiller" in reply
    assert conversation.status == ConversationStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_orchestrator_injects_active_knowledge_into_system_prompt(db_session, unique_email):
    """L'agent doit réellement s'appuyer sur la base de connaissances active (section 29)."""
    from app.models.knowledge_entry import KnowledgeCategory, KnowledgeEntry

    tenant, product, conversation = await _setup(db_session, unique_email)
    db_session.add(
        KnowledgeEntry(
            tenant_id=tenant.id,
            category=KnowledgeCategory.OBJECTION,
            title="Trop cher",
            content="Mettre en avant la garantie 2 ans incluse.",
            active=True,
        )
    )
    db_session.add(
        KnowledgeEntry(
            tenant_id=tenant.id,
            category=KnowledgeCategory.FAQ,
            title="Entrée désactivée",
            content="NE DOIT JAMAIS APPARAÎTRE",
            active=False,
        )
    )
    await db_session.commit()

    fake = FakeLLMClient([text_response("Réponse")])
    await generate_ai_reply(
        db=db_session, tenant=tenant, conversation=conversation, history=[], incoming_text="C'est trop cher", llm_client=fake
    )

    system_prompt_used = fake.received_systems[0]
    assert "BASE DE CONNAISSANCES" in system_prompt_used
    assert "Trop cher" in system_prompt_used
    assert "garantie 2 ans" in system_prompt_used
    assert "NE DOIT JAMAIS APPARAÎTRE" not in system_prompt_used


@pytest.mark.asyncio
async def test_conversation_history_passed_to_llm(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)
    fake = FakeLLMClient([text_response("D'accord.")])

    history = [
        Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER, content="Bonjour"),
        Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.AI, content="Bonjour à vous !"),
    ]

    await generate_ai_reply(
        db=db_session,
        tenant=tenant,
        conversation=conversation,
        history=history,
        incoming_text="Je cherche un téléphone",
        llm_client=fake,
    )

    sent_messages = fake.received_messages[0]
    assert sent_messages[0] == {"role": "user", "content": "Bonjour"}
    assert sent_messages[1] == {"role": "assistant", "content": "Bonjour à vous !"}
    assert sent_messages[2] == {"role": "user", "content": "Je cherche un téléphone"}


@pytest.mark.asyncio
async def test_max_tool_iterations_falls_back_gracefully(db_session, unique_email):
    """Le LLM ne doit jamais pouvoir boucler indéfiniment sur des outils (section 34)."""
    tenant, product, conversation = await _setup(db_session, unique_email)
    # Toujours des appels d'outils, jamais de réponse finale : simule une boucle infinie potentielle.
    fake = FakeLLMClient([tool_use_response("search_products", {"query": "x"}) for _ in range(20)])

    reply = await generate_ai_reply(
        db=db_session, tenant=tenant, conversation=conversation, history=[], incoming_text="test", llm_client=fake
    )
    assert reply == FALLBACK_MESSAGE
    assert fake.call_count <= 5  # max_tool_iterations par défaut


@pytest.mark.asyncio
async def test_llm_exception_falls_back_gracefully(db_session, unique_email):
    tenant, product, conversation = await _setup(db_session, unique_email)

    class BrokenLLMClient:
        async def create_message(self, **kwargs):
            raise RuntimeError("Panne réseau simulée")

    reply = await generate_ai_reply(
        db=db_session,
        tenant=tenant,
        conversation=conversation,
        history=[],
        incoming_text="Bonjour",
        llm_client=BrokenLLMClient(),
    )
    assert reply == FALLBACK_MESSAGE
