import asyncio
import logging
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.agents.classifier import get_message_classifier
from app.agents.dependency import get_llm_client
from app.agents.llm_client import LLMClient
from app.agents.orchestrator import _create_with_retries, is_transient_llm_error
from app.core.config import get_settings
from app.core.security import hash_password
from app.integrations.whatsapp import client as wa_module
from app.integrations.whatsapp.client import WhatsAppClient, WhatsAppSendError, describe_meta_error
from app.main import app
from app.models.conversation import Conversation, ConversationStatus, Message
from app.models.handoff_settings import TenantHandoffSettings
from app.models.message_signal import MessageSignal
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.services.handoff_rules import OUTAGE_CALLBACK_MESSAGE, OUTAGE_RETRY_LATER_MESSAGE, TRANSFER_MESSAGE
from app.tests.fakes import text_response, tool_use_response
from app.tests.test_message_signals import FakeClassifier


class ApiError(Exception):
    """Imite une erreur HTTP du SDK (status_code porté par raw_response)."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.raw_response = SimpleNamespace(status_code=status)


class NoResponseError(Exception):
    pass


class ScriptedLLM(LLMClient):
    """Chaque élément du script : une réponse (dict) ou une exception à lever."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def create_message(self, *, system, messages, tools, max_tokens=1024):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_retry_delays", [0.0, 0.0])


# --- Erreurs passagères ou définitives ------------------------------------------------

@pytest.mark.parametrize("exc", [ApiError(503), ApiError(429), ApiError(500), ApiError(502), ApiError(504),
                                 asyncio.TimeoutError(), httpx.ConnectError("coupure"), httpx.ReadTimeout("lent"),
                                 NoResponseError()])
def test_transient_errors(exc):
    assert is_transient_llm_error(exc)


@pytest.mark.parametrize("exc", [ApiError(400), ApiError(401), ApiError(422), ValueError("bug"), KeyError("x")])
def test_definitive_errors(exc):
    assert not is_transient_llm_error(exc)


def test_status_code_attribute_is_also_understood():
    exc = Exception("x")
    exc.status_code = 529
    assert is_transient_llm_error(exc)


@pytest.mark.asyncio
async def test_retries_then_succeeds():
    llm = ScriptedLLM([ApiError(503), ApiError(503), text_response("ok")])
    assert (await _create_with_retries(llm, system="", messages=[], tools=[]))["content"][0]["text"] == "ok"
    assert llm.calls == 3


@pytest.mark.asyncio
async def test_gives_up_after_two_retries():
    llm = ScriptedLLM([ApiError(503)])
    with pytest.raises(ApiError):
        await _create_with_retries(llm, system="", messages=[], tools=[])
    assert llm.calls == 3  # 1 essai + 2 nouveaux essais


@pytest.mark.asyncio
async def test_definitive_error_is_not_retried():
    llm = ScriptedLLM([ApiError(401), text_response("jamais atteint")])
    with pytest.raises(ApiError):
        await _create_with_retries(llm, system="", messages=[], tools=[])
    assert llm.calls == 1


# --- Webhook ---------------------------------------------------------------------------

async def _setup(db_session, email, pnid, outage_policy=None):
    tenant = Tenant(name="Boutique Awa", country="SN", currency="XOF", email=email, plan=TenantPlan.INDEPENDANT)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id=pnid, system_user_token="t"))
    if outage_policy:
        db_session.add(TenantHandoffSettings(tenant_id=tenant.id, refund_transfer=True, complaint_policy="TRY_FIRST",
                                             discount_policy="FIXED_PRICES", ai_outage_policy=outage_policy))
    await db_session.commit()
    return tenant


def _payload(pnid, sender, text):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pnid}, "messages": [
        {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "type": "text", "text": {"body": text}, "timestamp": "1"}]}}]}]}


@pytest.fixture
def wire(monkeypatch):
    class _Silent:
        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, *a, **kw):
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)
    outbox: list[dict] = []
    monkeypatch.setattr("app.api.webhooks.whatsapp.send_email", lambda **kw: outbox.append(kw) or True)

    def use(llm, classification=None):
        app.dependency_overrides[get_llm_client] = lambda: llm
        app.dependency_overrides[get_message_classifier] = lambda: (FakeClassifier(classification) if classification else None)
        return outbox

    yield use
    app.dependency_overrides.pop(get_llm_client, None)
    app.dependency_overrides.pop(get_message_classifier, None)


async def _statuses(db_session, tenant_id):
    return [c.status for c in (await db_session.execute(select(Conversation).where(Conversation.tenant_id == tenant_id)
                                                        .execution_options(populate_existing=True))).scalars().all()]


async def _system(db_session, tenant_id, kind):
    return (await db_session.execute(select(Message.content).where(
        Message.tenant_id == tenant_id, Message.message_type == kind))).scalars().all()


@pytest.mark.asyncio
async def test_brief_outage_goes_unnoticed_by_the_customer(client, db_session, unique_email, wire):
    """Le cas du 26/09 : un 503 passager ne doit plus produire de message d'excuse."""
    await _setup(db_session, unique_email, "pn-res-1")
    llm = ScriptedLLM([ApiError(503), text_response("Bonjour, le sac à main est disponible !")])
    outbox = wire(llm)

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-res-1", "221700000201", "Je suis intéressée par le sac à main"))

    assert r.json()["ai_reply"] == "Bonjour, le sac à main est disponible !"
    assert llm.calls == 2
    assert outbox == []


@pytest.mark.asyncio
async def test_outage_default_honest_message_no_transfer_one_email_per_hour(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-res-2")
    outbox = wire(ScriptedLLM([ApiError(503)]))

    first = await client.post("/webhooks/whatsapp", json=_payload("pn-res-2", "221700000202", "Bonjour"))
    second = await client.post("/webhooks/whatsapp", json=_payload("pn-res-2", "221700000203", "Allô"))

    assert first.json()["ai_reply"] == OUTAGE_RETRY_LATER_MESSAGE == second.json()["ai_reply"]
    assert "conseiller" not in OUTAGE_RETRY_LATER_MESSAGE  # aucune promesse que personne ne tiendra
    assert set(await _statuses(db_session, tenant.id)) == {ConversationStatus.ACTIVE}  # pas de transfert
    assert [e["subject"] for e in outbox] == ["Bob a rencontré une panne technique"]  # une seule fois pour 2 clients
    assert len(await _system(db_session, tenant.id, "ai_outage")) == 2


@pytest.mark.asyncio
async def test_outage_callback_email_per_customer_with_number(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-res-3", outage_policy="CALLBACK")
    outbox = wire(ScriptedLLM([ApiError(503)]))

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-res-3", "221700000204", "Je veux commander"))
    await client.post("/webhooks/whatsapp", json=_payload("pn-res-3", "221700000204", "Vous êtes là ?"))  # même client
    await client.post("/webhooks/whatsapp", json=_payload("pn-res-3", "221700000205", "Bonjour"))  # autre client

    assert r.json()["ai_reply"] == OUTAGE_CALLBACK_MESSAGE
    assert [e["subject"] for e in outbox] == ["À rappeler : +221700000204", "À rappeler : +221700000205"]
    assert "Numéro à appeler : +221700000204" in outbox[0]["body"]
    assert "« Je veux commander »" in outbox[0]["body"]
    assert all(e["to"] == unique_email for e in outbox)
    assert set(await _statuses(db_session, tenant.id)) == {ConversationStatus.ACTIVE}


@pytest.mark.asyncio
async def test_outage_is_traced_on_the_message_labels(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-res-4")
    wire(ScriptedLLM([ApiError(503)]), classification={"intents": ["RECHERCHE_PRODUIT"], "objections": []})

    await client.post("/webhooks/whatsapp", json=_payload("pn-res-4", "221700000206", "Vous avez des robes ?"))

    signal = (await db_session.execute(select(MessageSignal).where(MessageSignal.tenant_id == tenant.id)
                                       .execution_options(populate_existing=True))).scalar_one()
    assert signal.applied_rule == "AI_OUTAGE_RETRY_LATER"


@pytest.mark.asyncio
async def test_definitive_error_gives_outage_message_without_retry(client, db_session, unique_email, wire):
    await _setup(db_session, unique_email, "pn-res-5")
    llm = ScriptedLLM([ApiError(401)])
    wire(llm)

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-res-5", "221700000207", "Bonjour"))

    assert r.json()["ai_reply"] == OUTAGE_RETRY_LATER_MESSAGE
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_tool_loop_is_a_real_transfer_with_alert(client, db_session, unique_email, wire):
    tenant = await _setup(db_session, unique_email, "pn-res-6")
    outbox = wire(ScriptedLLM([tool_use_response("search_products", {"query": "robe"})]))  # ne finit jamais

    r = await client.post("/webhooks/whatsapp", json=_payload("pn-res-6", "221700000208", "Une robe rouge"))

    assert r.json()["ai_reply"] == TRANSFER_MESSAGE
    assert await _statuses(db_session, tenant.id) == [ConversationStatus.WAITING_HUMAN]
    assert await _system(db_session, tenant.id, "handoff") == ["Transfert vers un humain : Règle — IA bloquée (aucune réponse finale)"]
    assert len(outbox) == 1 and outbox[0]["subject"].startswith("Un client attend votre réponse")


# --- Erreurs Meta lisibles ------------------------------------------------------------

def _meta_response(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://graph.facebook.com/x"))


def test_meta_error_is_described_with_code_and_reason():
    response = _meta_response(400, {"error": {"message": "(#131030) Recipient phone number not in allowed list",
                                              "type": "OAuthException", "code": 131030,
                                              "error_data": {"details": "Recipient phone number not in allowed list"}}})
    assert describe_meta_error(response) == (
        "code 131030 : (#131030) Recipient phone number not in allowed list — Recipient phone number not in allowed list"
    )


def test_non_json_meta_error_is_still_described():
    response = httpx.Response(502, text="Bad Gateway", request=httpx.Request("POST", "https://graph.facebook.com/x"))
    assert describe_meta_error(response) == "Bad Gateway"


@pytest.mark.asyncio
async def test_refused_send_is_logged_without_the_token(monkeypatch, caplog):
    def handler(request):
        return _meta_response(400, {"error": {"message": "Re-engagement message", "code": 131047}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(wa_module.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    client = WhatsAppClient(phone_number_id="1190108494175159", system_user_token="JETON-SECRET-123")

    with caplog.at_level(logging.WARNING), pytest.raises(WhatsAppSendError) as err:
        await client.send_text_message(to="221700000000", body="Bonjour")

    assert err.value.status_code == 400
    assert "code 131047" in caplog.text and "Re-engagement message" in caplog.text
    assert "1190108494175159" in caplog.text
    assert "JETON-SECRET-123" not in caplog.text and "JETON-SECRET-123" not in str(err.value)


# --- Réglage ---------------------------------------------------------------------------

async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_outage_setting_default_update_and_validation(client, db_session, unique_email):
    await _setup(db_session, unique_email, "pn-res-7")
    headers = await _headers(client, unique_email)

    assert (await client.get("/api/v1/tenants/me/handoff-settings", headers=headers)).json()["ai_outage_policy"] == "RETRY_LATER"
    updated = await client.put("/api/v1/tenants/me/handoff-settings", json={"ai_outage_policy": "CALLBACK"}, headers=headers)
    assert updated.json()["ai_outage_policy"] == "CALLBACK"
    assert (await client.put("/api/v1/tenants/me/handoff-settings", json={"ai_outage_policy": "NUMERO_SECOURS"}, headers=headers)).status_code == 422
