"""
Lot 21 — catalogue Facebook en un clic, synchronisation de nuit, garde-fou de devise.

Le jeton obtenu auprès de Meta ne doit JAMAIS revenir au navigateur ni apparaître dans un
message d'erreur ; seuls les catalogues que le commerçant a cochés peuvent être connectés.
"""
import asyncio

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.pending_store import InMemoryPendingStore, get_pending_store
from app.core.security import hash_password
from app.integrations.ecommerce.dependency import get_meta_catalog_client_factory
from app.integrations.ecommerce.meta_catalog_client import MetaCatalogOAuth, get_meta_catalog_oauth
from app.integrations.whatsapp.client import MetaGraphError, MetaOAuthClient
from app.main import app
from app.models.audit_log import AuditLog
from app.models.ecommerce_connection import EcommerceConnection, EcommercePlatform, SyncStatus
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services.meta_catalog_sync import sync_meta_catalog
from app.tests.fakes_meta_catalog import make_meta_product

TOKEN = "EAAB-SECRET-TOKEN-XYZ"
CATALOG_NAMES = {"111": "Vetements", "222": "Chaussures", "333": "Véhicules"}


class FakeOAuth:
    def __init__(self, granted=("111",), fail=False):
        self.granted = list(granted)
        self.fail = fail
        self.codes = []

    async def exchange_code(self, code):
        self.codes.append(code)
        if self.fail:
            raise MetaGraphError("Meta a répondu 400 (code 100 : Invalid verification code format)")
        return TOKEN

    async def granted_catalogs(self, token):
        assert token == TOKEN
        return [{"id": i, "name": None} for i in self.granted], {"autorisations": 0, "attribues": len(self.granted), "entreprise": 0}


class FakeCatalog:
    products = [make_meta_product("p1", "Chemise jaune", price="15 000 FCFA", retailer_id="CH-1")]

    def __init__(self, catalog_id, token):
        assert token == TOKEN
        self.catalog_id = catalog_id

    async def fetch_catalog_info(self):
        return {"name": CATALOG_NAMES[self.catalog_id]}

    async def fetch_products(self, limit=100, max_pages=50):
        return [dict(p, currency="XOF") for p in self.products]


@pytest.fixture
def meta(monkeypatch):
    state = {"oauth": FakeOAuth(), "pending": InMemoryPendingStore()}
    app.dependency_overrides[get_meta_catalog_oauth] = lambda: state["oauth"]
    app.dependency_overrides[get_meta_catalog_client_factory] = lambda: FakeCatalog
    app.dependency_overrides[get_pending_store] = lambda: state["pending"]
    yield state
    for dep in (get_meta_catalog_oauth, get_meta_catalog_client_factory, get_pending_store):
        app.dependency_overrides.pop(dep, None)


async def _shop(db_session, email, role=Role.OWNER, plan=TenantPlan.PRO, currency="XOF"):
    tenant = Tenant(name="Boutique", country="CI", currency=currency, email=email, plan=plan)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=role))
    await db_session.commit()
    return tenant


async def _headers(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _connection(db_session, tenant_id):
    return (await db_session.execute(select(EcommerceConnection).where(
        EcommerceConnection.tenant_id == tenant_id).execution_options(populate_existing=True))).scalar_one_or_none()


# --- Un seul catalogue partagé : connexion et synchronisation directes ----------------------

@pytest.mark.asyncio
async def test_single_shared_catalog_connects_and_syncs(client, db_session, unique_email, meta):
    tenant = await _shop(db_session, unique_email)

    r = await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c0de"},
                          headers=await _headers(client, unique_email))

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "connected" and body["catalog_id"] == "111"
    assert body["sync"]["imported"] == 1
    assert TOKEN not in r.text
    connection = await _connection(db_session, tenant.id)
    assert connection.shop_domain == "111" and connection.access_token == TOKEN
    assert connection.auto_sync_enabled is True and connection.last_sync_status == SyncStatus.SUCCESS
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "META_CATALOG_CONNECTED"))).scalar_one()
    assert audit.details == {"catalog_id": "111", "via": "oauth"}


# --- Plusieurs catalogues : choix, jeton gardé côté serveur, usage unique -------------------

@pytest.mark.asyncio
async def test_several_catalogs_ask_to_choose_then_connect(client, db_session, unique_email, meta):
    tenant = await _shop(db_session, unique_email)
    meta["oauth"].granted = ["111", "222"]
    headers = await _headers(client, unique_email)

    r = await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c0de"}, headers=headers)

    assert r.json() == {"status": "choose", "catalogs": [{"id": "111", "name": "Vetements"}, {"id": "222", "name": "Chaussures"}]}
    assert TOKEN not in r.text
    assert await _connection(db_session, tenant.id) is None  # rien n'est connecté avant le choix

    r = await client.post("/api/v1/integrations/meta-catalog/oauth/select", json={"catalog_id": "222"}, headers=headers)

    assert r.status_code == 200 and r.json()["catalog_id"] == "222" and TOKEN not in r.text
    assert (await _connection(db_session, tenant.id)).shop_domain == "222"
    again = await client.post("/api/v1/integrations/meta-catalog/oauth/select", json={"catalog_id": "111"}, headers=headers)
    assert again.status_code == 410  # le jeton en attente est à usage unique


@pytest.mark.asyncio
async def test_only_a_shared_catalog_can_be_selected(client, db_session, unique_email, meta):
    await _shop(db_session, unique_email)
    meta["oauth"].granted = ["111", "222"]
    headers = await _headers(client, unique_email)
    await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c"}, headers=headers)

    r = await client.post("/api/v1/integrations/meta-catalog/oauth/select", json={"catalog_id": "333"}, headers=headers)

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_expired_or_missing_choice(client, db_session, unique_email, meta, monkeypatch):
    await _shop(db_session, unique_email)
    headers = await _headers(client, unique_email)
    r = await client.post("/api/v1/integrations/meta-catalog/oauth/select", json={"catalog_id": "111"}, headers=headers)
    assert r.status_code == 410

    import app.api.integrations.meta_catalog as routes

    monkeypatch.setattr(routes, "PENDING_TTL_SECONDS", 0)
    meta["oauth"].granted = ["111", "222"]
    await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c"}, headers=headers)
    r = await client.post("/api/v1/integrations/meta-catalog/oauth/select", json={"catalog_id": "111"}, headers=headers)
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_another_shop_cannot_use_a_pending_choice(client, db_session, meta):
    await _shop(db_session, "a@cat.ci")
    other = await _shop(db_session, "b@cat.ci")
    meta["oauth"].granted = ["111", "222"]
    await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c"},
                      headers=await _headers(client, "a@cat.ci"))

    r = await client.post("/api/v1/integrations/meta-catalog/oauth/select", json={"catalog_id": "111"},
                          headers=await _headers(client, "b@cat.ci"))

    assert r.status_code == 410
    assert await _connection(db_session, other.id) is None


# --- Refus ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_catalog_shared(client, db_session, unique_email, meta):
    await _shop(db_session, unique_email)
    meta["oauth"].granted = []
    r = await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c"},
                          headers=await _headers(client, unique_email))
    assert r.status_code == 400 and "cochez le catalogue" in r.json()["detail"]
    assert "Diagnostic Meta : 0 dans les autorisations, 0 attribué(s), 0 dans l'entreprise." in r.json()["detail"]


@pytest.mark.asyncio
async def test_meta_refusal_is_explained_without_secrets(client, db_session, unique_email, meta):
    await _shop(db_session, unique_email)
    meta["oauth"].fail = True
    r = await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c"},
                          headers=await _headers(client, unique_email))
    assert r.status_code == 400
    assert r.json()["detail"] == "Connexion Meta impossible : Meta a répondu 400 (code 100 : Invalid verification code format)"


@pytest.mark.asyncio
@pytest.mark.parametrize("role, plan, expected", [(Role.AGENT, TenantPlan.PRO, 403), (Role.OWNER, TenantPlan.FREE, 403)])
async def test_admin_and_paid_plan_required(client, db_session, unique_email, meta, role, plan, expected):
    await _shop(db_session, unique_email, role=role, plan=plan)
    r = await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c"},
                          headers=await _headers(client, unique_email))
    assert r.status_code == expected
    assert meta["oauth"].codes == []  # le code n'est même pas échangé


@pytest.mark.asyncio
async def test_login_config_is_public_values_only(client, db_session, unique_email, monkeypatch):
    from app.core.config import get_settings

    await _shop(db_session, unique_email)
    headers = await _headers(client, unique_email)
    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_app_id", "1886381942345588")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "APP-SECRET")
    monkeypatch.setattr(settings, "meta_catalog_login_config_id", "")
    assert (await client.get("/api/v1/integrations/meta-catalog/login-config", headers=headers)).json()["available"] is False

    monkeypatch.setattr(settings, "meta_catalog_login_config_id", "987654")
    r = await client.get("/api/v1/integrations/meta-catalog/login-config", headers=headers)
    assert r.json()["available"] is True and r.json()["config_id"] == "987654"
    assert "APP-SECRET" not in r.text


# --- Lecture des catalogues cochés et erreurs Meta (vraies requêtes simulées) ----------------

def _mock(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def _graph(routes):
    """Répond selon le chemin ; toute route absente = refus de Meta."""
    seen = []

    def handler(request):
        seen.append(request)
        for path, body in routes.items():
            if request.url.path.endswith(path):
                return httpx.Response(200, json=body)
        return httpx.Response(400, json={"error": {"code": 100, "message": "Unsupported"}})

    return handler, seen


def test_catalogs_assigned_to_the_system_user_are_found(monkeypatch):
    """Le cas réel du 27/09 : aucune liste dans debug_token, les catalogues sont attribués au système."""
    handler, seen = _graph({
        "/debug_token": {"data": {"granular_scopes": [{"scope": "catalog_management"}]}},
        "/me/assigned_product_catalogs": {"data": [{"id": "1625074041890982", "name": "Vetements"}]},
    })
    _mock(monkeypatch, handler)

    catalogs, counts = asyncio.run(MetaCatalogOAuth("APPID", "APP-SECRET").granted_catalogs(TOKEN))

    assert catalogs == [{"id": "1625074041890982", "name": "Vetements"}]
    assert counts == {"autorisations": 0, "attribues": 1, "entreprise": 0}
    for request in seen:
        assert TOKEN not in str(request.url).replace("input_token=" + TOKEN, "") and "APP-SECRET" not in str(request.url)
    assert seen[0].headers["Authorization"] == "Bearer APPID|APP-SECRET"
    assert seen[1].headers["Authorization"] == f"Bearer {TOKEN}"


def test_token_permissions_list_is_used_and_merged(monkeypatch):
    handler, _ = _graph({
        "/debug_token": {"data": {"granular_scopes": [{"scope": "catalog_management", "target_ids": ["111", 222]}]}},
        "/me/assigned_product_catalogs": {"data": [{"id": "222", "name": "Chaussures"}]},
    })
    _mock(monkeypatch, handler)

    catalogs, counts = asyncio.run(MetaCatalogOAuth("A", "S").granted_catalogs(TOKEN))

    assert catalogs == [{"id": "111", "name": None}, {"id": "222", "name": "Chaussures"}]
    assert counts["autorisations"] == 2


def test_client_business_catalogs_as_last_resort(monkeypatch):
    handler, _ = _graph({
        "/me": {"client_business_id": "999"},
        "/999/owned_product_catalogs": {"data": [{"id": "111", "name": "Vetements"}]},
    })
    _mock(monkeypatch, handler)

    catalogs, counts = asyncio.run(MetaCatalogOAuth("A", "S").granted_catalogs(TOKEN))

    assert catalogs == [{"id": "111", "name": "Vetements"}] and counts["entreprise"] == 1


def test_nothing_found_anywhere(monkeypatch):
    handler, _ = _graph({})
    _mock(monkeypatch, handler)
    assert asyncio.run(MetaCatalogOAuth("A", "S").granted_catalogs(TOKEN)) == (
        [], {"autorisations": 0, "attribues": 0, "entreprise": 0})


@pytest.mark.asyncio
async def test_names_given_by_meta_are_used_without_extra_calls(client, db_session, unique_email, meta):
    class Named(FakeOAuth):
        async def granted_catalogs(self, token):
            return [{"id": "111", "name": "Vetements"}, {"id": "222", "name": "Chaussures"}], {}

    await _shop(db_session, unique_email)
    meta["oauth"] = Named()
    app.dependency_overrides[get_meta_catalog_oauth] = lambda: meta["oauth"]
    r = await client.post("/api/v1/integrations/meta-catalog/oauth/callback", json={"code": "c"},
                          headers=await _headers(client, unique_email))
    assert r.json()["catalogs"] == [{"id": "111", "name": "Vetements"}, {"id": "222", "name": "Chaussures"}]


def test_code_exchange_error_never_shows_the_app_secret(monkeypatch):
    """Meta exige le secret de l'app DANS l'adresse de l'échange : le message brut le contiendrait."""
    _mock(monkeypatch, lambda r: httpx.Response(400, json={"error": {"code": 100, "message": "Invalid code"}}))

    with pytest.raises(MetaGraphError) as caught:
        asyncio.run(MetaOAuthClient("APPID", "APP-SECRET").exchange_code_for_token("c"))

    assert "APP-SECRET" not in str(caught.value) and "graph.facebook.com" not in str(caught.value)
    assert str(caught.value) == "Meta a répondu 400 (code 100 : Invalid code)"


@pytest.mark.asyncio
async def test_whatsapp_signup_error_never_shows_the_app_secret(client, db_session, unique_email):
    from app.integrations.whatsapp.dependency import get_meta_oauth_client

    class Leaky:
        async def exchange_code_for_token(self, code):
            request = httpx.Request("GET", "https://graph.facebook.com/v25.0/oauth/access_token?client_secret=APP-SECRET")
            raise httpx.ConnectError("boom " + str(request.url), request=request)

    await _shop(db_session, unique_email)
    app.dependency_overrides[get_meta_oauth_client] = lambda: Leaky()
    try:
        r = await client.post("/api/v1/whatsapp/embedded-signup/callback", headers=await _headers(client, unique_email),
                              json={"code": "c", "waba_id": "1", "phone_number_id": "2"})
    finally:
        app.dependency_overrides.pop(get_meta_oauth_client, None)
    assert r.status_code == 400 and "APP-SECRET" not in r.text
    assert r.json()["detail"] == "Connexion WhatsApp impossible : Meta est injoignable pour le moment"


# --- Garde-fou de devise ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_products_in_another_currency_are_not_imported(db_session):
    tenant = await _shop(db_session, "devise@cat.ci", currency="XOF")
    connection = EcommerceConnection(tenant_id=tenant.id, platform=EcommercePlatform.META_CATALOG,
                                     shop_domain="111", access_token=TOKEN)
    db_session.add(connection)
    await db_session.commit()

    class Mixed(FakeCatalog):
        async def fetch_products(self, limit=100, max_pages=50):
            return [dict(make_meta_product("x", "Chemise jaune", price="15 000 FCFA"), currency="XAF"),
                    dict(make_meta_product("y", "Chemise rayée", price="25 000 FCFA"), currency="XOF")]

    result = await sync_meta_catalog(db_session, tenant.id, connection, client=Mixed("111", TOKEN))

    assert (result.imported, result.failed) == (1, 1)
    assert result.errors == ["« Chemise jaune » : devise XAF, votre boutique est en XOF. "
                             "Corrigez la devise dans le Gestionnaire de commerce puis resynchronisez."]
    names = (await db_session.execute(select(Product.name).where(Product.tenant_id == tenant.id))).scalars().all()
    assert names == ["Chemise rayée"]


# --- Synchronisation de nuit ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nightly_sync_keeps_going_when_one_shop_fails(monkeypatch):
    from app.workers import catalog_sync

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        ok = Tenant(name="OK", country="CI", currency="XOF", email="ok@n.ci", plan=TenantPlan.PRO)
        ko = Tenant(name="KO", country="CI", currency="XOF", email="ko@n.ci", plan=TenantPlan.PRO)
        shopify = Tenant(name="S", country="CI", currency="XOF", email="s@n.ci", plan=TenantPlan.PRO)
        db.add_all([ok, ko, shopify])
        await db.flush()
        db.add_all([
            EcommerceConnection(tenant_id=ok.id, platform=EcommercePlatform.META_CATALOG, shop_domain="111", access_token=TOKEN),
            EcommerceConnection(tenant_id=ko.id, platform=EcommercePlatform.META_CATALOG, shop_domain="222", access_token="REVOKED"),
            EcommerceConnection(tenant_id=shopify.id, platform=EcommercePlatform.SHOPIFY, shop_domain="x.myshopify.com", access_token="t"),
        ])
        await db.commit()
        ok_id, ko_id = ok.id, ko.id

    class NightClient:
        def __init__(self, catalog_id, token):
            self.token = token

        async def fetch_products(self, limit=100, max_pages=50):
            if self.token == "REVOKED":
                request = httpx.Request("GET", "https://graph.facebook.com/v25.0/222/products")
                raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(
                    401, request=request, json={"error": {"code": 190, "message": "Session expired"}}))
            return [dict(make_meta_product("n1", "Pagne", price="8 000 FCFA"), currency="XOF")]

    import app.integrations.ecommerce.meta_catalog_client as client_module

    monkeypatch.setattr(client_module, "MetaCatalogClient", NightClient)

    report = await catalog_sync.sync_all_meta_catalogs(session_factory=factory)

    assert report == {"synced": 1, "failed": 1}
    async with factory() as db:
        rows = {c.tenant_id: c for c in (await db.execute(select(EcommerceConnection))).scalars()}
        assert rows[ok_id].last_sync_status == SyncStatus.SUCCESS and rows[ok_id].last_synced_at is not None
        assert rows[ko_id].last_sync_status == SyncStatus.FAILED
        assert (await db.execute(select(Product.name).where(Product.tenant_id == ok_id))).scalars().all() == ["Pagne"]
    await engine.dispose()


def test_nightly_task_is_registered_and_scheduled():
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.catalog_sync" in TASK_MODULES
    entry = celery_app.conf.beat_schedule["sync-meta-catalogs-nightly"]
    assert entry["task"] == "app.workers.catalog_sync.sync_meta_catalogs_task"
