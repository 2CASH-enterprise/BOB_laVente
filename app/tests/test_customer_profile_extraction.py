import pytest

from app.agents.tools import ToolExecutor
from app.core.security import hash_password
from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.customer_memory_service import build_customer_memory


async def _setup(db_session, email: str):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER)
    )
    customer = Customer(tenant_id=tenant.id, whatsapp_number="221700000000")
    db_session.add(customer)
    await db_session.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(customer)
    await db_session.refresh(conversation)
    return tenant, customer, conversation


@pytest.mark.asyncio
async def test_update_customer_profile_saves_name_and_city(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    result = await executor.execute("update_customer_profile", {"first_name": "Fatou", "city": "Dakar"})
    assert result["status"] == "saved"

    await db_session.refresh(customer)
    assert customer.first_name == "Fatou"
    assert customer.city == "Dakar"


@pytest.mark.asyncio
async def test_update_customer_profile_merges_preferences(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    await executor.execute("update_customer_profile", {"need": "smartphone"})
    await executor.execute("update_customer_profile", {"brand": "Samsung", "budget_max": 250000})

    await db_session.refresh(customer)
    assert customer.detected_preferences["need"] == "smartphone"  # conservé du premier appel
    assert customer.detected_preferences["brand"] == "Samsung"
    assert customer.detected_preferences["budget_max"] == 250000


@pytest.mark.asyncio
async def test_update_customer_profile_never_erases_with_empty_fields(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)

    await executor.execute("update_customer_profile", {"first_name": "Fatou"})
    await executor.execute("update_customer_profile", {"city": "Dakar"})  # ne doit pas effacer first_name

    await db_session.refresh(customer)
    assert customer.first_name == "Fatou"
    assert customer.city == "Dakar"


@pytest.mark.asyncio
async def test_detected_preferences_appear_in_customer_memory(db_session, unique_email):
    tenant, customer, conversation = await _setup(db_session, unique_email)
    executor = ToolExecutor(db_session, tenant.id, conversation)
    await executor.execute("update_customer_profile", {"need": "smartphone", "brand": "Samsung", "budget_max": 250000})
    await db_session.commit()

    memory = await build_customer_memory(db_session, tenant.id, customer.id)
    assert "smartphone" in memory
    assert "Samsung" in memory
    assert "INFÉRENCE" in memory  # jamais présenté comme un fait vérifié
