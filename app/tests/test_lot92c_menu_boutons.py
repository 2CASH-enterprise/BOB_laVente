"""
Lot 92c — menu de l'assistant en boutons (choix de Thérèse du 31/10, voie a, contrat 1.3.1 inchangé) :
« menu », « aide », « aide bob » → 2 messages de 3 boutons ; le 2e seulement après la réponse 200 de Naomy au 1er.
Les 6 boutons font exactement ce que font les commandes écrites, qui restent valables.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.naomy import AssistantOutbox
from app.models.product import Product
from app.services import merchant_assistant as ma
from app.tests.test_lot89_connecteur_naomy import naomy_env  # noqa: F401 — fixture
from app.tests.test_lot92_assistant import NOW, Sent, _linked, _msg, _order, _replies, _say, assistant_env  # noqa: F401


def test_titles_and_ids_follow_naomys_message():
    assert ma.MENU_1 == ("Je suis Bob, votre vendeur.", [("bob:menu:ventes", "📈 Mes ventes"),
                                                         ("bob:menu:a-confirmer", "⏳ Ventes à confirmer"),
                                                         ("bob:menu:vendu", "✅ Vendu")])
    assert ma.MENU_2 == ("Vos clients et vos produits. Pour ajouter un produit : envoyez sa photo, puis son nom et son prix "
                         "(par exemple : Sac cabas 12000).",
                         [("bob:menu:qui-attend", "👥 Qui attend"), ("bob:menu:stock", "📦 Stock"),
                          ("bob:menu:ajouter", "📸 Ajouter")])
    for _, title in ma.MENU_1[1] + ma.MENU_2[1]:
        assert len(title) <= 20 and ma.short(title, 20) == title  # « ⏳ Ventes à confirmer » : 20 tout juste, jamais coupé


@pytest.mark.parametrize("word", ["menu", "aide", "aide bob", "Menu"])
@pytest.mark.asyncio
async def test_menu_words_send_two_messages_of_three_buttons(client, db_session, assistant_env, word):
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _msg(word))
    first, second = await _replies(db_session, tenant)
    assert first.payload["texte"] == ma.MENU_1[0] and [b["id"] for b in first.payload["boutons"]] == \
        ["bob:menu:ventes", "bob:menu:a-confirmer", "bob:menu:vendu"]
    assert second.payload["texte"] == ma.MENU_2[0] and [b["id"] for b in second.payload["boutons"]] == \
        ["bob:menu:qui-attend", "bob:menu:stock", "bob:menu:ajouter"]
    assert first.message_id != second.message_id
    # Les deux sont partis, dans l'ordre, le 2e après le 200 du 1er.
    assert [c[1]["id_message_bob"] for c in assistant_env.sender.calls] == [first.message_id, second.message_id]
    assert first.status == second.status == "SENT"


@pytest.mark.asyncio
async def test_second_message_waits_for_the_first_and_is_dropped_if_it_never_leaves(db_session, assistant_env, client):
    tenant, _, headers, connection = await _linked(client, db_session)
    ma.send_menu(db_session, connection, NOW)
    await db_session.commit()
    first, second = await _replies(db_session, tenant)
    assert second.status == "WAITING" and second.ref == f"after:{first.message_id}"
    down = Sent(503)
    await ma.send_outbox(db_session, NOW, sender=down, mailer=assistant_env.mails)
    await db_session.refresh(second)
    assert [c[1]["id_message_bob"] for c in down.calls] == [first.message_id] and second.status == "WAITING"  # renvoi prévu
    refused = Sent(400)
    await ma.send_outbox(db_session, NOW + timedelta(minutes=2), sender=refused, mailer=assistant_env.mails)
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.status == "FAILED" and second.status == "SKIPPED"
    assert [c[1]["id_message_bob"] for c in refused.calls] == [first.message_id]  # jamais le 2e sans le 1er


@pytest.mark.asyncio
async def test_menu_buttons_do_what_the_written_commands_do(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    db_session.add(Product(tenant_id=tenant.id, sku="E", name="Écharpe soie", price=1, currency="XOF", stock_quantity=0, active=True))
    await db_session.commit()
    await _order(db_session, tenant)
    pairs = [("bob:menu:a-confirmer", "ventes à confirmer"), ("bob:menu:stock", "stock"), ("bob:menu:ajouter", "ajouter"),
             ("bob:menu:qui-attend", "qui attend"), ("bob:menu:vendu", "vendu"), ("bob:menu:ventes", "mes ventes")]
    for button, word in pairs:
        await _say(client, db_session, headers, _msg(kind="bouton", button=button))
        await _say(client, db_session, headers, _msg(word))
    replies = [(r.payload["texte"], r.payload.get("boutons")) for r in await _replies(db_session, tenant)]
    assert len(replies) == 12
    for i in range(0, 12, 2):
        assert replies[i] == replies[i + 1], pairs[i // 2]
    assert ma.MENU_1[0] not in [t for t, _ in replies]


@pytest.mark.asyncio
async def test_an_unknown_menu_button_brings_the_menu_back(client, db_session, assistant_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    await _say(client, db_session, headers, _msg(kind="bouton", button="bob:menu:inconnu"))
    assert [r.payload["texte"] for r in await _replies(db_session, tenant)] == [ma.MENU_1[0], ma.MENU_2[0]]
    assert (await db_session.execute(select(AssistantOutbox).where(AssistantOutbox.status == "WAITING"))).first() is None
