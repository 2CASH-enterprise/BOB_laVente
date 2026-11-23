"""
Lot 129 b — liens « une seule page » envoyés par Bob sur le numéro AgenC'AI : usage unique, 15 minutes, session de
30 minutes limitée par le serveur aux routes de la page ; bouton « Ouvrir … » (contrat 1.7.1, champ `lien`) quand
l'interrupteur est allumé ; « Tableau de bord » = juste le lien, connexion habituelle (décision du 23/11).
"""
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.naomy import AssistantOutbox
from app.models.tenant import Tenant
from app.models.user import User
from app.services import magic_login, page_links
from app.tests.test_lot89_connecteur_naomy import naomy_env  # noqa: F401 — fixture
from app.tests.test_lot92_assistant import NOW, _msg, _replies, _say, assistant_env  # noqa: F401
from app.tests.test_lot129_menu_restaurant import _last, _resto, cards  # noqa: F401

PAGE = (Path(__file__).resolve().parents[1] / "static" / "m" / "index.html").read_text(encoding="utf-8")
ID = "0b1e7f2a-1111-4222-8333-944455556666"


def test_each_page_opens_only_its_own_routes():
    assert page_links.scope_of("PAGE_CARTE") == "page:carte" and page_links.scope_of("WHATSAPP") is None
    assert page_links.allowed("page:carte", "GET", "/api/v1/products")
    assert page_links.allowed("page:carte", "PUT", f"/api/v1/products/{ID}")
    assert page_links.allowed("page:carte", "POST", f"/api/v1/products/{ID}/image")
    assert page_links.allowed("page:horaires", "PUT", "/api/v1/tenants/me/kitchen-closed")
    assert page_links.allowed("page:affiches", "GET", f"/api/v1/qr-codes/{ID}/poster")
    for scope, method, path in [("page:carte", "GET", "/api/v1/customers"), ("page:carte", "GET", "/api/v1/conversations"),
                                ("page:carte", "DELETE", f"/api/v1/products/{ID}"), ("page:carte", "GET", "/api/v1/products/export/xlsx"),
                                ("page:carte", "PUT", "/api/v1/tenants/me/fulfillment"), ("page:horaires", "GET", "/api/v1/products"),
                                ("page:affiches", "PUT", "/api/v1/tenants/me/profile"), ("page:inconnue", "GET", "/api/v1/tenants/me"),
                                ("page:carte", "GET", "/api/v1/tenants/me/../../customers")]:
        assert not page_links.allowed(scope, method, path), (scope, method, path)


def _token(text):
    return re.search(r"connexion=([A-Za-z0-9_\-]+)", text).group(1)


@pytest.mark.asyncio
async def test_the_link_opens_the_page_once_and_nothing_else(client, db_session, assistant_env, cards, monkeypatch):
    from app.services import merchant_assistant as ma
    from app.tests.test_lot92_assistant import Sent

    tenant, headers, _ = await _resto(client, db_session)
    monkeypatch.setattr(ma, "http_post", Sent(503))  # rien ne part : on lit le message préparé
    await _say(client, db_session, headers, _msg("carte"))
    reply = await _last(db_session, tenant)
    assert "/m/?page=carte&connexion=" in reply["texte"] and "15 minutes" in reply["texte"] and "lien" not in reply
    token = _token(reply["texte"])
    from datetime import datetime, timezone

    from app.models.magic_login import MagicLogin

    for row in (await db_session.execute(select(MagicLogin))).scalars():  # les messages sont datés NOW, la route vit au présent
        row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await db_session.commit()
    r = await client.post("/api/v1/auth/magic", json={"token": token})
    assert r.status_code == 200, r.text
    page = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get("/api/v1/products", headers=page)).status_code == 200
    assert (await client.get("/api/v1/tenants/me", headers=page)).status_code == 200
    for path in ("/api/v1/customers", "/api/v1/conversations", "/api/v1/tenants/me/fulfillment", "/api/v1/orders"):
        r = await client.get(path, headers=page)
        assert r.status_code == 403 and "qu'à elle-même" in r.json()["detail"], path
    assert (await client.post("/api/v1/auth/magic", json={"token": token})).status_code == 400  # usage unique


@pytest.mark.asyncio
async def test_a_link_expires_after_15_minutes_and_does_not_eat_naomys_quota(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    url = await page_links.create(db_session, tenant, "horaires", NOW)
    await db_session.commit()
    assert await magic_login.issued_today(db_session, tenant.id, NOW) == 0  # les liens de Naomy (lot 118) restent 5 / jour
    assert await magic_login.consume_with_purpose(db_session, _token(url), NOW + timedelta(minutes=16)) is None
    user, purpose = await magic_login.consume_with_purpose(db_session, _token(url), NOW + timedelta(minutes=14))
    assert purpose == "PAGE_HORAIRES" and user.tenant_id == tenant.id


@pytest.mark.asyncio
async def test_hours_page_closes_the_kitchen(client, db_session, assistant_env, cards):
    tenant, headers, _ = await _resto(client, db_session)
    url = await page_links.create(db_session, tenant, "horaires")
    await db_session.commit()
    access = (await client.post("/api/v1/auth/magic", json={"token": _token(url)})).json()["access_token"]
    page = {"Authorization": f"Bearer {access}"}
    assert (await client.get("/api/v1/tenants/me/kitchen-closed", headers=page)).json()["closed_today"] is False
    r = await client.put("/api/v1/tenants/me/kitchen-closed", json={"closed": True}, headers=page)
    assert r.status_code == 200 and r.json()["closed_today"] is True
    tenant = (await db_session.execute(select(Tenant).where(Tenant.id == tenant.id).execution_options(populate_existing=True))).scalar_one()
    assert tenant.kitchen_closed_on is not None
    assert (await client.get("/api/v1/products", headers=page)).status_code == 403  # pas la carte depuis les horaires


@pytest.mark.asyncio
async def test_no_link_without_password_for_a_two_step_account(client, db_session, assistant_env, cards, monkeypatch):
    from app.services import merchant_assistant as ma
    from app.tests.test_lot92_assistant import Sent

    tenant, headers, _ = await _resto(client, db_session)
    for user in (await db_session.execute(select(User).where(User.tenant_id == tenant.id))).scalars():
        user.mfa_enabled = True
    await db_session.commit()
    monkeypatch.setattr(ma, "http_post", Sent(503))
    await _say(client, db_session, headers, _msg("carte"))
    text = (await _last(db_session, tenant))["texte"]
    assert "connexion=" not in text and "/dashboard/?tab=products" in text and "mot de passe" in text


@pytest.mark.asyncio
async def test_link_buttons_follow_contract_1_7_1_and_tokens_are_not_kept(client, db_session, assistant_env, cards, monkeypatch):
    tenant, headers, _ = await _resto(client, db_session)
    monkeypatch.setattr(get_settings(), "assistant_link_buttons", f"autre-boutique, {tenant.id}")  # essai sur une boutique
    await _say(client, db_session, headers, _msg("affiches"))
    await _say(client, db_session, headers, _msg("tableau de bord"))
    posters, board = (await _replies(db_session, tenant))[-2:]  # _replies vérifie le schéma EnvoiAssistant 1.7.1
    assert posters.payload["lien"]["titre"] == "Ouvrir mes affiches" and "boutons" not in posters.payload
    assert board.payload["lien"] == {"titre": "Tableau de bord", "url": f"{get_settings().public_base_url.rstrip('/')}/dashboard/"}
    assert "connexion=" not in str(board.payload)  # tableau de bord : connexion habituelle
    assert posters.status == "SENT" and "connexion=…" in posters.payload["lien"]["url"]  # jeton effacé après l'envoi
    rows = (await db_session.execute(select(AssistantOutbox).where(AssistantOutbox.tenant_id == tenant.id))).scalars().all()
    assert not any(re.search(r"connexion=[A-Za-z0-9_\-]{20,}", str(r.payload)) for r in rows)


def test_the_page_itself():
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in PAGE and "Plus+Jakarta+Sans" in PAGE
    assert not re.search(r"\son[a-z]+=", PAGE)  # lot 70 : aucun attribut on…
    assert 'fetch(API + "/auth/magic"' in PAGE and "history.replaceState" in PAGE and "sessionStorage" in PAGE
    assert "localStorage" not in PAGE
    for words in ("Ma carte", "Mes horaires", "Affiches et QR", "Disponible aujourd'hui", "Fermer la cuisine ce soir",
                  "Imprimer les chevalets", "Ajouter un plat"):
        assert words in PAGE, words


@pytest.mark.asyncio
async def test_the_page_is_served(client):
    assert (await client.get("/m/?page=carte")).status_code == 200


@pytest.mark.asyncio
async def test_link_buttons_stay_off_for_other_shops(client, db_session, assistant_env, cards, monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_link_buttons", "0b1e7f2a-0000-4000-8000-000000000000")
    tenant, headers, _ = await _resto(client, db_session)
    await _say(client, db_session, headers, _msg("tableau de bord"))
    reply = (await _replies(db_session, tenant))[-1].payload
    assert "lien" not in reply and "/dashboard/" in reply["texte"]
