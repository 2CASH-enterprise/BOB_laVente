"""Lot 74 — « Propulsé par Bob » : une seule fois, en dernière ligne, jamais dans la mémoire de Bob."""
from pathlib import Path

import pytest

from app.agents.dependency import get_llm_client
from app.agents.orchestrator import _history_to_anthropic_messages
from app.main import app
from app.models.conversation import Message, MessageSender
from app.services.powered_by import WHATSAPP_POWERED_BY, add_powered_by, strip_powered_by
from app.tests.fakes import FakeLLMClient, text_response
from app.tests.test_freemium_webhook import _payload, _setup

DASHBOARD = Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html"


class RecordingLLM(FakeLLMClient):
    def __init__(self, responses):
        super().__init__(responses)
        self.seen: list[list[dict]] = []

    async def create_message(self, *, system, messages, tools, max_tokens=1024):
        self.seen.append(list(messages))
        return await super().create_message(system=system, messages=messages, tools=tools, max_tokens=max_tokens)


@pytest.mark.parametrize("text", [
    "Voici notre produit.\n\n_Propulsé par Bob 🤖_",
    "Voici notre produit.\n\n_Propulsé par Bob 🤖_\n\n_Propulsé par Bob 🤖_",
    "Voici notre produit.\n*Propulsé par Bob*",
    "Voici notre produit.\n— propulsée par Bob AI",
])
def test_strip_removes_every_copy(text):
    assert strip_powered_by(text) == "Voici notre produit."


def test_strip_keeps_ordinary_sentences():
    text = "Notre site est propulsé par Bob et Awa.\nMerci"
    assert strip_powered_by("Bonjour\n" + text) == "Bonjour\n" + text
    assert strip_powered_by(None) == ""


def test_add_puts_a_single_mention_on_the_last_line():
    out = add_powered_by("Bonjour.\n\n_Propulsé par Bob 🤖_")
    assert out == f"Bonjour.\n\n{WHATSAPP_POWERED_BY}" and out.count("Propulsé") == 1


def test_memory_never_shows_the_mention_to_bob():
    history = [Message(sender=MessageSender.CUSTOMER, content="Bonjour"),
               Message(sender=MessageSender.AI, content="Salut !\n\n_Propulsé par Bob 🤖_"),
               Message(sender=MessageSender.HUMAN, content="Je reviens vers vous")]
    msgs = _history_to_anthropic_messages(history)
    assert [m["content"] for m in msgs] == ["Bonjour", "Salut !", "Je reviens vers vous"]


@pytest.mark.asyncio
async def test_free_plan_reply_has_the_mention_once_even_if_bob_copies_it(client, db_session, unique_email):
    tenant = await _setup(db_session, unique_email, "phone_lot74_free", is_paid=False)
    from app.models.product import Product

    db_session.add(Product(tenant_id=tenant.id, sku="P93", name="Pagne", price=5000, currency="XOF", stock_quantity=1))  # lot 93 : un prix annoncé par Bob existe dans le catalogue
    await db_session.commit()
    fake = RecordingLLM([text_response("Voici notre produit."),
                         text_response("Il coûte 5 000 F.\n\n_Propulsé par Bob 🤖_")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        first = (await client.post("/webhooks/whatsapp", json=_payload("phone_lot74_free", "221700007401", "Bonjour"))).json()
        payload = _payload("phone_lot74_free", "221700007401", "Le prix ?")
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.lot74.2"
        second = (await client.post("/webhooks/whatsapp", json=payload)).json()
    finally:
        del app.dependency_overrides[get_llm_client]
    for reply in (first["ai_reply"], second["ai_reply"]):
        assert reply.count("Propulsé par Bob") == 1 and reply.endswith(WHATSAPP_POWERED_BY)
    assert second["ai_reply"] == f"Il coûte 5 000 F.\n\n{WHATSAPP_POWERED_BY}"
    # Au 2e message, la mémoire de Bob contient sa première réponse… sans la mention.
    memory = " ".join(str(m["content"]) for m in fake.seen[-1])
    assert "Voici notre produit." in memory and "Propulsé" not in memory


@pytest.mark.asyncio
async def test_paid_plan_reply_drops_a_copied_mention(client, db_session, unique_email):
    await _setup(db_session, unique_email, "phone_lot74_paid", is_paid=True)
    fake = FakeLLMClient([text_response("Voici.\n\n_Propulsé par Bob 🤖_")])
    app.dependency_overrides[get_llm_client] = lambda: fake
    try:
        reply = (await client.post("/webhooks/whatsapp", json=_payload("phone_lot74_paid", "221700007402", "Bonjour"))).json()["ai_reply"]
    finally:
        del app.dependency_overrides[get_llm_client]
    assert reply == "Voici."


def test_dashboard_shows_the_mention_small_and_once():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert ".msg-powered {" in html and "font-size: 11px" in html.split(".msg-powered {", 1)[1].split("}", 1)[0]
    assert 'class="msg-powered"' in html.split("function waFormat", 1)[1].split("\n}", 1)[0]
