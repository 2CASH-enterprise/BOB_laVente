"""
Lot 90 — mode simple du tableau de bord : quatre grands boutons pour le commerçant.

Décisions du 27/10 : mode simple par défaut pour un commerce (toutes formules) ; agences, concessions et courtiers
restent sur le tableau de bord complet mais peuvent changer ; les comptes déjà créés gardent le tableau complet (avec
un bandeau pour essayer) ; les nouveaux commerçants commencent en mode simple.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.conversation import ConversationStatus, MessageSender
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User
from app.models.whatsapp_account import WhatsAppAccount
from app.services import simple_mode
from app.tests.test_sales_opportunities import _headers, _shop

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
MIGRATION = (Path(__file__).resolve().parents[2] / "alembic" / "versions" / "d90a3c7e5b12_lot_90_mode_simple.py").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)  # midi : « aujourd'hui » ne dépend pas de l'heure du test


class _T:
    def __init__(self, business_type):
        self.business_type = business_type


class _U:
    def __init__(self, ui_mode):
        self.ui_mode = ui_mode


# --- Mode par défaut et choix de l'utilisateur ---------------------------------------------------------------------

def test_a_shop_starts_in_simple_mode_and_other_sectors_in_full_mode():
    assert simple_mode.default_mode(_T("ONLINE_STORE")) == "SIMPLE"
    assert simple_mode.default_mode(_T(None)) == "SIMPLE"  # pas encore choisi : commerce par défaut
    for sector in ("REAL_ESTATE", "AUTO_DEALER", "INSURANCE"):
        assert simple_mode.default_mode(_T(sector)) == "FULL"


def test_the_users_choice_wins_over_the_default():
    assert simple_mode.effective_mode(_U(None), _T("ONLINE_STORE")) == "SIMPLE"
    assert simple_mode.effective_mode(_U("FULL"), _T("ONLINE_STORE")) == "FULL"
    assert simple_mode.effective_mode(_U("SIMPLE"), _T("REAL_ESTATE")) == "SIMPLE"
    assert simple_mode.effective_mode(_U("BIZARRE"), _T("REAL_ESTATE")) == "FULL"


def test_existing_accounts_keep_the_full_dashboard_after_the_migration():
    assert "add_column" in MIGRATION and "ui_mode" in MIGRATION
    assert re.search(r"UPDATE users SET ui_mode\s*=\s*'FULL'", MIGRATION)
    assert "drop_column" in MIGRATION


@pytest.mark.asyncio
async def test_api_mode_get_and_change(client, db_session, unique_email):
    shop = await _shop(db_session, unique_email)
    headers = await _headers(client, unique_email)

    r = await client.get("/api/v1/simple/mode", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"mode": "SIMPLE", "default": "SIMPLE", "suggested": True}

    r = await client.put("/api/v1/simple/mode", json={"mode": "FULL"}, headers=headers)
    assert r.json()["mode"] == "FULL"
    assert (await client.get("/api/v1/simple/mode", headers=headers)).json()["mode"] == "FULL"
    user = (await db_session.execute(select(User).where(User.email == unique_email)
                                     .execution_options(populate_existing=True))).scalar_one()
    assert user.ui_mode == "FULL"

    assert (await client.put("/api/v1/simple/mode", json={"mode": "AUTRE"}, headers=headers)).status_code == 400
    assert shop.tenant.id


@pytest.mark.asyncio
async def test_an_agency_is_not_pushed_to_simple_mode_but_can_choose_it(client, db_session, unique_email):
    shop = await _shop(db_session, unique_email)
    shop.tenant.business_type = "REAL_ESTATE"
    await db_session.commit()
    headers = await _headers(client, unique_email)

    assert (await client.get("/api/v1/simple/mode", headers=headers)).json() == {"mode": "FULL", "default": "FULL", "suggested": False}
    assert (await client.put("/api/v1/simple/mode", json={"mode": "SIMPLE"}, headers=headers)).json()["mode"] == "SIMPLE"


@pytest.mark.asyncio
async def test_simple_routes_need_a_login(client):
    for path in ("/api/v1/simple/mode", "/api/v1/simple/home", "/api/v1/simple/waiting",
                 "/api/v1/simple/to-confirm", "/api/v1/simple/sales"):
        assert (await client.get(path)).status_code == 401, path


# --- Accueil : les chiffres des quatre boutons ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_counts(db_session):
    shop = await _shop(db_session)
    user = (await db_session.execute(select(User).where(User.tenant_id == shop.tenant.id))).scalar_one()
    user.full_name = "Aïcha Koné"
    a, conv_a = await shop.customer("221700000001", first_name="Mariam")
    b, conv_b = await shop.customer("221700000002")
    _, conv_c = await shop.customer("221700000003")
    shop.msg(conv_c, NOW - timedelta(minutes=1), sender=MessageSender.AI)  # Bob seul a écrit : pas un client servi
    conv_b.status = ConversationStatus.WAITING_HUMAN
    shop.msg(conv_a, NOW - timedelta(minutes=5))
    shop.msg(conv_b, NOW - timedelta(minutes=3))
    shop.msg(conv_b, NOW - timedelta(minutes=2), sender=MessageSender.AI)
    db_session.add(Order(tenant_id=shop.tenant.id, customer_id=a.id, conversation_id=conv_a.id, status=OrderStatus.PAID,
                         total_amount=15000, currency="XOF", created_at=NOW - timedelta(minutes=30), paid_at=NOW - timedelta(minutes=10)))
    db_session.add(Order(tenant_id=shop.tenant.id, customer_id=a.id, status=OrderStatus.PAID, total_amount=99000,
                         currency="XOF", created_at=NOW - timedelta(days=3), paid_at=NOW - timedelta(days=3)))  # pas aujourd'hui
    shop.order(b, conv_b, NOW - timedelta(minutes=20), status=OrderStatus.PENDING, total=30000)
    db_session.add(Product(tenant_id=shop.tenant.id, sku="R1", name="Robe wax", price=15000, currency="XOF", stock_quantity=0))
    db_session.add(Product(tenant_id=shop.tenant.id, sku="S1", name="Sac", price=12000, currency="XOF", stock_quantity=4))
    await db_session.commit()

    h = await simple_mode.home(db_session, shop.tenant, user, now=NOW)

    assert h["first_name"] == "Aïcha"
    assert h["shop"] == "Boutique" and h["currency"] == "XOF"
    assert h["today"] == {"sales": 1, "revenue": "15 000", "customers": 2}
    assert h["waiting"] == 1
    assert h["to_confirm"] == 1
    assert h["out_of_stock"] == ["Robe wax"]
    assert h["setup"] == {"whatsapp": False, "products": 2, "ready": False}


@pytest.mark.asyncio
async def test_setup_is_ready_once_whatsapp_and_products_are_there(db_session):
    shop = await _shop(db_session)
    db_session.add(WhatsAppAccount(tenant_id=shop.tenant.id, waba_id="w", phone_number_id="pn-lot90-ready",
                                   system_user_token="t"))
    await db_session.commit()
    h = await simple_mode.home(db_session, shop.tenant, None, now=NOW)
    assert h["setup"] == {"whatsapp": True, "products": 0, "ready": False}
    assert h["first_name"] is None

    db_session.add(Product(tenant_id=shop.tenant.id, sku="A", name="A", price=1, currency="XOF", stock_quantity=1))
    await db_session.commit()
    assert (await simple_mode.home(db_session, shop.tenant, None, now=NOW))["setup"]["ready"] is True


@pytest.mark.asyncio
async def test_support_number_is_the_agencai_number_in_digits(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "naomy_whatsapp_number", "+33 7 00 00 00 00")
    shop = await _shop(db_session)
    assert (await simple_mode.home(db_session, shop.tenant, None, now=NOW))["support_whatsapp"] == "33700000000"
    monkeypatch.setattr(get_settings(), "naomy_whatsapp_number", "")
    assert (await simple_mode.home(db_session, shop.tenant, None, now=NOW))["support_whatsapp"] is None


# --- Clients qui vous attendent --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_waiting_list_shows_the_last_customer_message(db_session):
    shop = await _shop(db_session)
    _, conv = await shop.customer("221700001234", first_name="Mariam")
    _, other = await shop.customer("221700005678")
    conv.status = ConversationStatus.WAITING_HUMAN
    conv.last_message_at = NOW
    shop.msg(conv, NOW - timedelta(minutes=4), content="Vous l'avez en taille 46 ?")
    shop.msg(conv, NOW - timedelta(minutes=3), sender=MessageSender.AI, content="Je demande au vendeur.")
    shop.msg(other, NOW, content="Bonjour")  # Bob s'en occupe : pas dans la liste
    await db_session.commit()

    [item] = await simple_mode.waiting_list(db_session, shop.tenant)

    assert item["conversation_id"] == str(conv.id)
    assert item["name"] == "Mariam"
    assert item["last_message"] == "Vous l'avez en taille 46 ?"
    assert item["whatsapp_link"] is None  # pas de coexistence : on répond dans Bob


@pytest.mark.asyncio
async def test_with_coexistence_the_merchant_answers_from_his_phone(db_session):
    shop = await _shop(db_session)
    db_session.add(WhatsAppAccount(tenant_id=shop.tenant.id, waba_id="w", phone_number_id="pn-lot90-coex",
                                   system_user_token="t", coexistence_mode=True))
    _, conv = await shop.customer("221700001234")
    conv.status = ConversationStatus.WAITING_HUMAN
    await db_session.commit()

    [item] = await simple_mode.waiting_list(db_session, shop.tenant)

    assert item["whatsapp_link"] == "https://wa.me/221700001234"
    assert item["name"] == "Client •• 1234"


# --- Ventes à confirmer et mes ventes --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sales_to_confirm_list_the_pending_orders_with_their_items(db_session):
    shop = await _shop(db_session)
    customer, conv = await shop.customer("221700000001", first_name="Aïcha")
    robe = Product(tenant_id=shop.tenant.id, sku="R", name="Robe longue", price=15000, currency="XOF", stock_quantity=3)
    db_session.add(robe)
    await db_session.flush()
    order = Order(tenant_id=shop.tenant.id, customer_id=customer.id, conversation_id=conv.id, status=OrderStatus.PENDING,
                  total_amount=30000, currency="XOF", created_at=NOW, reminders_sent=1)
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItem(order_id=order.id, product_id=robe.id, quantity=2, unit_price=15000, subtotal=30000))
    shop.order(customer, conv, NOW, status=OrderStatus.PAID, total=5000)  # déjà vendue : pas dans la liste
    await db_session.commit()

    [item] = await simple_mode.to_confirm_list(db_session, shop.tenant)

    assert item["order_id"] == str(order.id)
    assert item["customer"] == "Aïcha"
    assert item["items"] == "2 × Robe longue"
    assert item["total"] == "30 000" and item["currency"] == "XOF"
    assert item["conversation_id"] == str(conv.id)
    assert item["reminded"] is True


@pytest.mark.asyncio
async def test_sales_summary_by_period_and_best_sellers(db_session):
    shop = await _shop(db_session)
    customer, _ = await shop.customer()
    robe = Product(tenant_id=shop.tenant.id, sku="R", name="Robe", price=10000, currency="XOF", stock_quantity=3)
    sac = Product(tenant_id=shop.tenant.id, sku="S", name="Sac", price=5000, currency="XOF", stock_quantity=3)
    db_session.add_all([robe, sac])
    await db_session.flush()

    async def sold(when, product, qty, status=OrderStatus.PAID):
        order = Order(tenant_id=shop.tenant.id, customer_id=customer.id, status=status, total_amount=product.price * qty,
                      currency="XOF", created_at=when, paid_at=when if status == OrderStatus.PAID else None)
        db_session.add(order)
        await db_session.flush()
        db_session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=qty, unit_price=product.price,
                                 subtotal=product.price * qty))

    await sold(NOW - timedelta(minutes=5), robe, 1)
    await sold(NOW - timedelta(days=3), sac, 2)
    await sold(NOW - timedelta(days=7), sac, 1)                        # il y a 7 jours : hors de la semaine
    await sold(NOW - timedelta(days=20), robe, 2)
    await sold(NOW - timedelta(days=60), sac, 9)                       # trop ancienne
    await sold(NOW - timedelta(days=1), sac, 5, OrderStatus.PENDING)   # pas confirmée
    await db_session.commit()

    s = await simple_mode.sales_summary(db_session, shop.tenant, now=NOW)

    assert s["today"] == {"sales": 1, "revenue": "10 000"}
    assert s["week"] == {"sales": 2, "revenue": "20 000"}
    assert s["month"] == {"sales": 4, "revenue": "45 000"}
    assert [(t["name"], t["quantity"]) for t in s["top"]] == [("Robe", 3), ("Sac", 3)]


@pytest.mark.asyncio
async def test_lists_only_show_this_shop(client, db_session, unique_email):
    shop = await _shop(db_session, unique_email)
    other = await _shop(db_session, "autre-lot90@shop.sn")
    _, conv = await other.customer("221700009999")
    conv.status = ConversationStatus.WAITING_HUMAN
    c2, _ = await other.customer("221700008888")
    other.order(c2, None, NOW, status=OrderStatus.PENDING, total=1000)
    await db_session.commit()
    headers = await _headers(client, unique_email)

    assert (await client.get("/api/v1/simple/waiting", headers=headers)).json() == {"items": []}
    assert (await client.get("/api/v1/simple/to-confirm", headers=headers)).json() == {"items": []}
    home = (await client.get("/api/v1/simple/home", headers=headers)).json()
    assert home["waiting"] == 0 and home["to_confirm"] == 0
    sales = (await client.get("/api/v1/simple/sales", headers=headers)).json()
    assert sales["month"] == {"sales": 0, "revenue": "0"} and sales["top"] == []
    assert shop.tenant.id != other.tenant.id


# --- Tableau de bord -------------------------------------------------------------------------------------------------

def test_dashboard_has_the_simple_screen_and_the_way_back():
    assert '<div id="simple-app" class="hidden"' in HTML
    assert HTML.index('id="simple-app"') < HTML.index('<div id="app" class="hidden">')
    for label in ("Ajouter un produit", "Clients qui vous attendent", "Ventes à confirmer", "Mes ventes",
                  "Plus de réglages", "Revenir au mode simple", "Essayer", "Passer au mode simple",
                  "Où sont vos produits aujourd'hui ?", "Dans ma boutique Shopify", "Aidez-moi", "Vendu", "Annuler", "Bob continue"):
        assert label in HTML, label


def test_enter_app_opens_the_chosen_mode():
    start = HTML.index("async function enterApp()")
    body = HTML[start:HTML.index("\n}\n", start)]
    assert 'loadUiMode()' in body
    assert body.index('ui.mode === "SIMPLE"') < body.index("loadOverview();")
    assert "showSimpleBanner()" in body


def test_simple_actions_reuse_the_existing_routes():
    for route in ("/api/v1/orders/${id}/mark-paid", "/api/v1/orders/${id}/cancel", "/api/v1/conversations/${id}/release",
                  '"/api/v1/products"', "/api/v1/products/${created.id}/image", "/api/v1/simple/mode"):
        assert route in HTML, route
    # « Vendu » et « Annuler » demandent toujours une confirmation (bouton de sortie « Retour »).
    start = HTML.index("async function simpleConfirmSale")
    block = HTML[start:HTML.index("if (!ok) return;", start)]
    assert block.count("await bobDialog(") == 2 and "const ok = sold\n" in block


def test_banner_dismissal_survives_a_blocked_storage():
    start = HTML.index("function showSimpleBanner()")
    block = HTML[start:HTML.index("async function trySimpleMode()")]
    assert block.count("try {") == 2 and "localStorage" in block


def test_simple_screen_css_uses_theme_tokens_only():
    css = HTML[HTML.index("/* Lot 90"):HTML.index(".simple-bar .action")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css.replace("#simple-app", "").replace("#sm-photo-box", ""))
    script = HTML[HTML.index("// --- Lot 90 : mode simple"):HTML.index("async function loadOverview()")]
    assert not re.search(r"#[0-9a-fA-F]{6}\b", script)
