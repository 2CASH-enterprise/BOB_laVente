"""Lot 31 — le Markdown de l'IA devient la mise en forme WhatsApp, par le code, juste avant l'envoi."""
import uuid

import pytest
from sqlalchemy import select

from app.agents.dependency import get_llm_client
from app.core.security import hash_password
from app.integrations.whatsapp.client import WhatsAppClient
from app.integrations.whatsapp.formatting import to_whatsapp
from app.main import app
from app.models.conversation import Message, MessageSender
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.models.whatsapp_account import WhatsAppAccount
from app.tests.fakes import FakeLLMClient, text_response


@pytest.mark.parametrize("markdown, whatsapp", [
    ("Voici la **Peugeot 5008 S (2021)** disponible", "Voici la *Peugeot 5008 S (2021)* disponible"),
    ("Le **prix** et le **stock**", "Le *prix* et le *stock*"),
    ("__Important__", "*Important*"),
    ("Réf. **PEU_5008**", "Réf. *PEU_5008*"),
    ("** Prix ** : 10 €", "*Prix* : 10 €"),
    ("**Livraison :** 2 jours", "*Livraison :* 2 jours"),
    ("### Nos horaires", "*Nos horaires*"),
    ("## **Offre** ##", "*Offre*"),
    ("- **Prix** : 25 000 XOF\n- Stock : 1\n  - Diesel", "• *Prix* : 25 000 XOF\n• Stock : 1\n  • Diesel"),
    ("* premier\n+ second", "• premier\n• second"),
    ("~~30 000~~ 25 000", "~30 000~ 25 000"),
    ("Voir [notre site](https://example.com/a)", "Voir notre site (https://example.com/a)"),
    ("[https://example.com](https://example.com)", "https://example.com"),
    ("Titre\n---\nSuite", "Titre\n\nSuite"),
    ("A\n\n\n\nB", "A\n\nB"),
])
def test_markdown_becomes_whatsapp(markdown, whatsapp):
    assert to_whatsapp(markdown) == whatsapp


@pytest.mark.parametrize("text", [
    "Déjà *gras* WhatsApp, _italique_ et ~barré~",
    "Total : 2 * 3 = 6",
    "_Propulsé par Bob 🤖_",
    "Bonjour ! Votre essai est confirmé le samedi 3 octobre à 10 h.",
    "Lien : https://bob.example/pay?id=a_b_c",
])
def test_whatsapp_text_is_left_unchanged(text):
    assert to_whatsapp(text) == text


def test_code_blocks_are_left_alone_and_empty_values_kept():
    assert to_whatsapp("```\n**brut**\n```\napres **gras**") == "```\n**brut**\n```\napres *gras*"
    assert to_whatsapp("") == "" and to_whatsapp(None) is None


@pytest.mark.asyncio
async def test_every_text_sent_by_the_client_is_converted(monkeypatch):
    sent = []

    async def fake_post(self, path, payload):
        sent.append(payload)
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    client = WhatsAppClient(phone_number_id="p", system_user_token="t")
    await client.send_text_message(to="1", body="**Prix** : 10 €")
    await client.send_image_message(to="1", link="https://x/y.jpg", caption="**Peugeot 5008** — 25 000 XOF")

    assert sent[0]["text"]["body"] == "*Prix* : 10 €"
    assert sent[1]["image"]["caption"] == "*Peugeot 5008* — 25 000 XOF"


@pytest.mark.asyncio
async def test_bob_reply_is_stored_as_sent(client, db_session, unique_email, monkeypatch):
    tenant = Tenant(name="Boutique", country="SN", currency="XOF", email=unique_email, plan=TenantPlan.PRO)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=unique_email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-fmt-1", system_user_token="t"))
    await db_session.commit()
    sent = []

    async def fake_post(self, path, payload):
        sent.append(payload["text"]["body"])
        return {}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    app.dependency_overrides[get_llm_client] = lambda: FakeLLMClient([text_response("Voici la **robe** :\n- **Prix** : 15 000 XOF")])
    try:
        await client.post("/webhooks/whatsapp", json={"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "pn-fmt-1"},
            "messages": [{"from": "221700004001", "id": f"wamid.{uuid.uuid4().hex}", "type": "text",
                          "text": {"body": "Prix de la robe ?"}, "timestamp": "1"}]}}]}]})
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    expected = "Voici la *robe* :\n• *Prix* : 15 000 XOF"
    assert sent == [expected]
    stored = (await db_session.execute(select(Message.content).where(
        Message.tenant_id == tenant.id, Message.sender == MessageSender.AI))).scalars().all()
    assert stored == [expected]


def test_dashboard_shows_whatsapp_bold_after_escaping():
    html = open("app/static/dashboard/index.html", encoding="utf-8").read()
    assert "${waFormat(m.content)}" in html and "return esc(value).replace(" in html
