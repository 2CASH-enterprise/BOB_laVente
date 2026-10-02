"""
Lot 50 — coûts de l'IA : cache de Mistral (une clé par boutique / par activité, consignes inchangées),
mesure des tokens par boutique, coût dans le Super Admin, limite de messages par client, alerte de
budget, relances réactivées (email, conversations de moins de 14 jours).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.classifier import MistralMessageClassifier, build_classifier_prompt
from app.agents.llm_client import MistralLLMClient
from app.agents.orchestrator import generate_ai_reply_detailed
from app.agents.prompts import build_system_prompt
from app.models.conversation import Conversation, ConversationStatus, Message, MessageSender
from app.models.customer import Customer
from app.models.followup_settings import TenantFollowupSettings
from app.models.llm_usage import LlmBudgetAlert, LlmUsage
from app.models.superadmin_user import SuperAdminUser
from app.models.tenant import Tenant, TenantPlan
from app.services import usage_guard
from app.services.business_type import CAR_DEALERSHIP
from app.services.followup_service import find_eligible_conversations
from app.services.llm_costs import (
    classifier_cache_key,
    cost_usd,
    price_for,
    reply_cache_key,
    usage_from_mistral,
)
from app.services.signal_service import classify_and_store
from app.tests.fakes import FakeLLMClient, text_response, tool_use_response

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


# --- Prix et clés de cache ------------------------------------------------------------------------------------

def test_prices_by_model_prefix():
    assert price_for("mistral-small-latest").input == 0.15 and price_for("mistral-small-2603").output == 0.60
    assert price_for("mistral-large-latest").input == 0.50 and price_for("MISTRAL-MEDIUM-latest").output == 2.00
    assert price_for("claude-sonnet-4-5") is None and price_for(None) is None
    assert price_for("ministral-8b-latest").input == 0.15


def test_cost_with_cache_at_ten_percent():
    # 1 M envoyés dont 600 000 servis par le cache, 100 000 reçus, Small 4
    assert cost_usd("mistral-small-latest", 1_000_000, 600_000, 100_000) == pytest.approx(0.4 * 0.15 + 0.6 * 0.015 + 0.1 * 0.60)
    assert cost_usd("mistral-small-latest", 100, 500, 0) == pytest.approx(100 * 0.015 / 1e6)  # cache ≤ envoyés
    assert cost_usd("modele-inconnu", 1000, 0, 10) is None


def test_cache_keys_are_stable_opaque_and_separate():
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    assert reply_cache_key(tenant_a) == reply_cache_key(str(tenant_a)) != reply_cache_key(tenant_b)
    assert str(tenant_a) not in reply_cache_key(tenant_a) and str(tenant_a).replace("-", "") not in reply_cache_key(tenant_a)
    assert classifier_cache_key(None) == classifier_cache_key("ONLINE_STORE") != classifier_cache_key(CAR_DEALERSHIP)
    assert len(reply_cache_key(tenant_a)) < 64


def test_usage_from_mistral():
    class Details:
        cached_tokens = 700

    class Usage:
        prompt_tokens, completion_tokens, prompt_tokens_details = 1000, 50, Details()

    assert usage_from_mistral(Usage()) == {"prompt_tokens": 1000, "cached_tokens": 700, "completion_tokens": 50}
    assert usage_from_mistral({"prompt_tokens": 10, "completion_tokens": 2}) == {"prompt_tokens": 10, "cached_tokens": 0, "completion_tokens": 2}
    assert usage_from_mistral(None) is None and usage_from_mistral({"completion_tokens": 3}) is None


# --- Le cache ne change rien à ce que Bob reçoit --------------------------------------------------------------

@pytest.mark.parametrize("business_type", ["ONLINE_STORE", CAR_DEALERSHIP])
def test_fixed_part_of_the_prompt_comes_first(business_type):
    """Deux clients d'une même boutique : seules la fin (mémoire du client) diffère — le début se met en cache."""
    tenant = Tenant(name="Auto Plus", country="SN", currency="XOF", business_type=business_type)
    base = build_system_prompt(tenant, [], "", now=NOW)
    awa = build_system_prompt(tenant, [], "\nMÉMOIRE CLIENT\nAwa, cherche un SUV\n", now=NOW)
    moussa = build_system_prompt(tenant, [], "\nMÉMOIRE CLIENT\nMoussa, budget 10 M\n", now=NOW)
    common = os.path.commonprefix([awa, moussa])
    assert len(common) >= len(base) - 1 and len(common) > 0.9 * len(base)


class _SdkChat:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def complete_async(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _sdk_response(text=None, tool=None, usage=None):
    class Fn:
        name, arguments = (tool or ("x", "{}"))

    class Call:
        id, function = "c1", Fn()

    class Msg:
        content = text
        tool_calls = [Call()] if tool else None

    class Choice:
        message, finish_reason = Msg(), "stop"

    class R:
        choices = [Choice()]

    R.usage = usage
    return R()


@pytest.mark.asyncio
async def test_mistral_client_sends_the_key_and_returns_tokens():
    client = MistralLLMClient(api_key="k", model="mistral-small-latest")
    chat = _SdkChat(_sdk_response(text="Bonjour", usage={"prompt_tokens": 900, "completion_tokens": 12,
                                                          "prompt_tokens_details": {"cached_tokens": 800}}))
    client._client = type("C", (), {"chat": chat})()

    with_key = await client.create_message(system="S", messages=[{"role": "user", "content": "Salut"}], tools=[], cache_key="bob-reply-x")
    without = await client.create_message(system="S", messages=[{"role": "user", "content": "Salut"}], tools=[])

    assert chat.calls[0]["prompt_cache_key"] == "bob-reply-x" and "prompt_cache_key" not in chat.calls[1]
    assert chat.calls[0]["messages"] == chat.calls[1]["messages"]  # la clé ne change rien au contenu envoyé
    assert with_key["usage"] == {"prompt_tokens": 900, "cached_tokens": 800, "completion_tokens": 12}
    assert with_key["content"][0]["text"] == without["content"][0]["text"] == "Bonjour"
    chat.response = _sdk_response(tool=("search_products", '{"query": "SUV"}'), usage={"prompt_tokens": 5, "completion_tokens": 1})
    tool = await client.create_message(system="S", messages=[], tools=[], cache_key="k")
    assert tool["stop_reason"] == "tool_use" and tool["usage"]["prompt_tokens"] == 5


# --- Mesure dans la boucle de Bob ---------------------------------------------------------------------------

class CachingFake(FakeLLMClient):
    supports_cache_key = True
    model = "mistral-small-latest"

    def __init__(self, responses):
        super().__init__(responses)
        self.cache_keys = []

    async def create_message(self, *, system, messages, tools, max_tokens=1024, cache_key=None):
        self.cache_keys.append(cache_key)
        response = await super().create_message(system=system, messages=messages, tools=tools, max_tokens=max_tokens)
        return {**response, "usage": {"prompt_tokens": 6000, "cached_tokens": 5000, "completion_tokens": 40}}


async def _shop(db, email, business_type="ONLINE_STORE"):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=email, plan=TenantPlan.PRO, business_type=business_type)
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, whatsapp_number=f"2217{uuid.uuid4().int % 10**8:08d}")
    db.add(customer)
    await db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    await db.commit()
    return tenant, customer, conversation


@pytest.mark.asyncio
async def test_every_call_uses_the_shop_key_and_is_measured(db_session):
    tenant, _, conversation = await _shop(db_session, "m1@l50.sn")
    fake = CachingFake([tool_use_response("search_products", {"query": "robe"}), text_response("Voici nos robes.")])

    text, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                               incoming_text="Vous avez des robes ?", llm_client=fake)
    await db_session.commit()

    assert text == "Voici nos robes."
    assert fake.cache_keys == [reply_cache_key(tenant.id)] * 2
    rows = (await db_session.execute(select(LlmUsage).where(LlmUsage.tenant_id == tenant.id))).scalars().all()
    assert len(rows) == 2 and {r.kind for r in rows} == {"REPLY"} and {r.model for r in rows} == {"mistral-small-latest"}
    assert all(r.conversation_id == conversation.id and r.cached_tokens == 5000 for r in rows)


@pytest.mark.asyncio
async def test_clients_without_cache_or_usage_are_untouched(db_session):
    tenant, _, conversation = await _shop(db_session, "m2@l50.sn")
    fake = FakeLLMClient([text_response("Bonjour !")])  # pas de clé de cache, pas de tokens
    text, _ = await generate_ai_reply_detailed(db=db_session, tenant=tenant, conversation=conversation, history=[],
                                               incoming_text="Bonjour", llm_client=fake)
    await db_session.commit()
    assert text == "Bonjour !"
    assert (await db_session.execute(select(LlmUsage))).scalars().all() == []


@pytest.mark.asyncio
async def test_classifier_uses_one_key_per_activity_and_is_measured(db_session):
    tenant, customer, conversation = await _shop(db_session, "c1@l50.sn", CAR_DEALERSHIP)
    message = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=MessageSender.CUSTOMER,
                      message_type="text", content="Vous faites le crédit ?")
    db_session.add(message)
    await db_session.commit()
    classifier = MistralMessageClassifier(api_key="k", model="mistral-small-latest")

    class Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": '{"intents": ["PAIEMENT"], "objections": ["FINANCEMENT"]}'})()})()]
        usage = {"prompt_tokens": 1500, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 1400}}

    chat = _SdkChat(Resp())
    classifier._client = type("C", (), {"chat": chat})()

    signal = await classify_and_store(db_session, classifier, message, business_type=CAR_DEALERSHIP)
    await db_session.commit()

    assert signal.objections == ["FINANCEMENT"]
    assert chat.calls[0]["prompt_cache_key"] == classifier_cache_key(CAR_DEALERSHIP)
    assert chat.calls[0]["messages"][0]["content"] == build_classifier_prompt(CAR_DEALERSHIP)
    [row] = (await db_session.execute(select(LlmUsage))).scalars().all()
    assert (row.kind, row.tenant_id, row.cached_tokens) == ("CLASSIFIER", tenant.id, 1400)

    await classifier.classify("Bonjour")  # boutique en ligne : l'autre clé
    assert chat.calls[1]["prompt_cache_key"] == classifier_cache_key("ONLINE_STORE")


# --- Limite par client -------------------------------------------------------------------------------------------

async def _messages(db, tenant, conversation, count, minutes_ago=10, sender=MessageSender.CUSTOMER, kind="text"):
    for i in range(count):
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender=sender, message_type=kind,
                       content="x", created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago, seconds=i)))
    await db.commit()


@pytest.mark.asyncio
async def test_rate_limit_states(db_session):
    tenant, customer, conversation = await _shop(db_session, "r1@l50.sn")
    await _messages(db_session, tenant, conversation, 30)
    assert await usage_guard.customer_rate_limit(db_session, tenant.id, customer.id) == usage_guard.OK
    await _messages(db_session, tenant, conversation, 1, minutes_ago=1)
    assert await usage_guard.customer_rate_limit(db_session, tenant.id, customer.id) == usage_guard.NOTIFY
    await _messages(db_session, tenant, conversation, 1, minutes_ago=0, sender=MessageSender.SYSTEM, kind="rate_limited")
    assert await usage_guard.customer_rate_limit(db_session, tenant.id, customer.id) == usage_guard.SILENT
    # Messages de Bob, d'une autre heure ou d'un autre client : jamais comptés
    other_tenant, other_customer, other_conv = await _shop(db_session, "r2@l50.sn")
    await _messages(db_session, other_tenant, other_conv, 40, minutes_ago=90)
    await _messages(db_session, other_tenant, other_conv, 40, sender=MessageSender.AI)
    assert await usage_guard.customer_rate_limit(db_session, other_tenant.id, other_customer.id) == usage_guard.OK


@pytest.mark.asyncio
async def test_rate_limit_through_the_webhook(client, db_session, monkeypatch):
    from app.agents.classifier import get_message_classifier
    from app.agents.dependency import get_llm_client
    from app.main import app
    from app.models.whatsapp_account import WhatsAppAccount
    from app.tests.test_message_signals import FakeClassifier
    from app.tests.test_strategies import _payload

    class _Silent:
        sent = []

        def __init__(self, *a, **kw):
            pass

        async def send_text_message(self, to, body):
            _Silent.sent.append(body)
            return {}

    monkeypatch.setattr("app.api.webhooks.whatsapp.WhatsAppClient", _Silent)
    tenant, customer, conversation = await _shop(db_session, "w1@l50.sn")
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-l50", system_user_token="t"))
    await _messages(db_session, tenant, conversation, 30)
    llm, classifier = FakeLLMClient([text_response("Ne doit pas servir")]), FakeClassifier({"intents": ["AUTRE"]})
    app.dependency_overrides[get_llm_client] = lambda: llm
    app.dependency_overrides[get_message_classifier] = lambda: classifier
    try:
        await client.post("/webhooks/whatsapp", json=_payload("pn-l50", customer.whatsapp_number, "Encore une question"))
        await client.post("/webhooks/whatsapp", json=_payload("pn-l50", customer.whatsapp_number, "Et encore"))
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
        app.dependency_overrides.pop(get_message_classifier, None)

    assert llm.call_count == 0 and classifier.calls == []  # aucun appel à l'IA, ni analyse
    assert _Silent.sent == [usage_guard.RATE_LIMIT_MESSAGE]  # une seule fois
    notes = (await db_session.execute(select(Message).where(Message.message_type == "rate_limited"))).scalars().all()
    assert len(notes) == 1
    stored = (await db_session.execute(select(Message.content).where(
        Message.sender == MessageSender.CUSTOMER, Message.content.in_(["Encore une question", "Et encore"])))).scalars().all()
    assert sorted(stored) == ["Encore une question", "Et encore"]  # visibles par la boutique


def test_rate_limit_message_promises_nothing():
    from app.services.promise_guard import contains_human_promise

    for text in (usage_guard.RATE_LIMIT_MESSAGE, usage_guard.RATE_LIMIT_MESSAGE_TU):
        assert not contains_human_promise(text) and "heure" in text


# --- Coûts par boutique, Super Admin, alerte -------------------------------------------------------------------

async def _usage(db, tenant_id, conversation_id, prompt, cached, completion, model="mistral-small-latest", when=NOW, kind="REPLY"):
    db.add(LlmUsage(tenant_id=tenant_id, conversation_id=conversation_id, kind=kind, model=model,
                    prompt_tokens=prompt, cached_tokens=cached, completion_tokens=completion, created_at=when))
    await db.commit()


async def _admin_headers(client):
    r = await client.post("/api/v1/superadmin/bootstrap", json={"email": "admin@bob.internal", "password": "supersecret123", "full_name": "Admin"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_superadmin_costs_per_shop(client, db_session):
    tenant, _, conv = await _shop(db_session, "s1@l50.sn")
    other, _, other_conv = await _shop(db_session, "s2@l50.sn")
    conv2 = Conversation(tenant_id=tenant.id, customer_id=conv.customer_id, status=ConversationStatus.ACTIVE)
    db_session.add(conv2)
    await db_session.commit()
    month = datetime.now(timezone.utc)
    await _usage(db_session, tenant.id, conv.id, 1_000_000, 800_000, 10_000, when=month)
    await _usage(db_session, tenant.id, conv2.id, 1_000_000, 0, 10_000, when=month)
    await _usage(db_session, tenant.id, conv.id, 100_000, 0, 1_000, kind="CLASSIFIER", when=month)
    await _usage(db_session, other.id, other_conv.id, 50, 0, 5, model="claude-x", when=month)
    await _usage(db_session, tenant.id, conv.id, 9_000_000, 0, 0, when=month - timedelta(days=40))  # autre mois

    r = await client.get("/api/v1/superadmin/ai-costs", headers=await _admin_headers(client))

    assert r.status_code == 200
    body = r.json()
    [main, unpriced] = body["rows"]
    assert main["tenant"] == "Boutique" and main["conversations"] == 2 and main["calls"] == 3
    assert main["prompt_tokens"] == 2_100_000 and main["cached_tokens"] == 800_000 and main["cache_rate_pct"] == 38.1
    expected = (1_300_000 * 0.15 + 800_000 * 0.015 + 21_000 * 0.60) / 1e6
    assert main["cost_usd"] == pytest.approx(expected, abs=1e-4)
    assert main["cost_per_conversation_usd"] == pytest.approx(expected / 2, abs=1e-4)
    assert unpriced["unpriced_models"] == ["claude-x"] and unpriced["cost_usd"] == 0
    assert body["totals"]["conversations"] == 3 and body["threshold_usd"] == 20.0


@pytest.mark.asyncio
async def test_superadmin_costs_require_superadmin_and_valid_month(client, db_session):
    assert (await client.get("/api/v1/superadmin/ai-costs")).status_code == 401
    headers = await _admin_headers(client)
    assert (await client.get("/api/v1/superadmin/ai-costs?month=2026-13", headers=headers)).status_code == 422
    r = await client.get("/api/v1/superadmin/ai-costs?month=2026-09", headers=headers)
    assert r.json()["month"] == "2026-09" and r.json()["rows"] == []


@pytest.mark.asyncio
async def test_budget_alert_is_sent_once_per_month(db_session, monkeypatch):
    from app.core.config import get_settings
    from app.workers.llm_budget import check_budgets

    monkeypatch.setattr(get_settings(), "llm_monthly_alert_usd", 1.0)
    big, _, big_conv = await _shop(db_session, "b1@l50.sn")
    small, _, small_conv = await _shop(db_session, "b2@l50.sn")
    db_session.add(SuperAdminUser(email="admin@bob.internal", hashed_password="x", full_name="Admin"))
    await db_session.commit()
    await _usage(db_session, big.id, big_conv.id, 10_000_000, 0, 0)  # 1,50 $
    await _usage(db_session, small.id, small_conv.id, 1_000_000, 0, 0)  # 0,15 $
    sent = []

    first = await check_budgets(db_session, now=NOW, send=lambda **mail: sent.append(mail) or True)
    second = await check_budgets(db_session, now=NOW, send=lambda **mail: sent.append(mail) or True)

    assert first == ["Boutique"] and second == []
    assert [m["to"] for m in sent] == ["admin@bob.internal"] and "1.50 $" in sent[0]["body"]
    assert sent[0]["subject"] == "Budget IA dépassé — Boutique (2026-10)"
    [alert] = (await db_session.execute(select(LlmBudgetAlert))).scalars().all()
    assert alert.tenant_id == big.id and alert.month == "2026-10"
    next_month = await check_budgets(db_session, now=NOW + timedelta(days=30), send=lambda **mail: sent.append(mail) or True)
    assert next_month == []  # rien consommé en novembre


# --- Relances réactivées, jamais pour une vieille conversation ---------------------------------------------------

@pytest.mark.asyncio
async def test_old_conversations_are_never_relanced(db_session):
    tenant, customer, _ = await _shop(db_session, "f1@l50.sn")
    settings = TenantFollowupSettings(tenant_id=tenant.id, enabled=True, first_followup_hours=24, second_followup_hours=72)
    recent = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
                          last_message_at=NOW - timedelta(days=13))
    old = Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.ACTIVE,
                       last_message_at=NOW - timedelta(days=15))
    db_session.add_all([settings, recent, old])
    await db_session.commit()
    eligible = await find_eligible_conversations(db_session, tenant.id, settings, now=NOW)
    assert recent in eligible and old not in eligible


@pytest.mark.asyncio
async def test_followup_worker_sends_emails(db_session):
    from app.workers.followups import check_followups

    tenant, customer, conversation = await _shop(db_session, "f2@l50.sn")
    customer.email, customer.marketing_consent = "client@example.com", True
    conversation.last_message_at = datetime.now(timezone.utc) - timedelta(hours=30)
    db_session.add(TenantFollowupSettings(tenant_id=tenant.id, enabled=True, first_followup_hours=24, second_followup_hours=72))
    await db_session.commit()
    sent = []
    assert await check_followups(db_session, send_email=lambda **mail: sent.append(mail) or True) == 1
    assert [m["to"] for m in sent] == ["client@example.com"]


def test_superadmin_page_shows_ai_costs():
    html = (ROOT / "static" / "superadmin" / "index.html").read_text(encoding="utf-8")
    assert 'id="ai-costs"' in html and "loadAiCosts();" in html and "/api/v1/superadmin/ai-costs" in html
    body = html[html.index("async function loadAiCosts()"):html.index("const PLANS")]
    assert "esc(r.tenant)" in body and "r.cost_per_conversation_usd" in body and "d.threshold_usd" in body
