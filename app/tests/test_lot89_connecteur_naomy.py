"""
Lot 89 — connecteur Naomy (contrat « Passerelle Bob ↔ Naomy » 1.2, docs/naomy/bob-naomy-openapi-v1.2.yaml) :
appairage par code unique et secret de plateforme, API lue par Naomy (clé par boutique), liens suivis « NaomyIA »,
résultats agrégés, événements signés avec renvois, rappels par e-mail écrits par Bob, carte « Naomy » du tableau de bord.
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.contact_point import ContactPoint
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.naomy import NaomyConnection, NaomyEvent, NaomyPairingCode, NaomyReminder
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import naomy_bridge as nb

SECRET = "secret-de-plateforme-de-test-0123456789"
EVENTS_URL = "https://agenc-ai.com/naomy/api/bob/evenements"
ROOT = Path(__file__).resolve().parents[2]
DASH = (ROOT / "app" / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def naomy_env(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "naomy_platform_secret", SECRET)
    monkeypatch.setattr(s, "naomy_allowed_host", "agenc-ai.com")
    monkeypatch.setattr(s, "naomy_allowed_path", "/naomy/api/bob/")
    monkeypatch.setattr(s, "naomy_base_url", "https://agenc-ai.com/naomy")
    monkeypatch.setattr(s, "naomy_whatsapp_number", "+33 7 80 84 12 69")
    monkeypatch.setattr(s, "public_base_url", "https://agenc-ai.com/bob")
    return s


async def _shop(db, plan=TenantPlan.PRO, business_type="ONLINE_STORE", name="MS4 Africa", email=None):
    email = email or f"s{uuid.uuid4().hex[:8]}@x.ci"
    tenant = Tenant(name=name, country="CI", currency="XOF", email=email, plan=plan, business_type=business_type)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="A", role=Role.OWNER))
    await db.commit()
    return tenant, email


async def _login(client, email):
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _signed(body: dict, secret=SECRET, age=0):
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()) - age)
    return raw, {"Content-Type": "application/json", "X-Platform-Timestamp": stamp,
                 "X-Platform-Signature": nb.sign(secret, stamp, raw)}


async def _pair(client, db, tenant, number="+2250701020312", url=EVENTS_URL, account="nao_1"):
    code = nb.new_code()
    db.add(NaomyPairingCode(tenant_id=tenant.id, code_hash=nb.digest(code),
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)))
    await db.commit()
    raw, headers = _signed({"code": code, "compte_naomy": account, "numero_confirmation": number, "url_evenements": url})
    return code, await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)


async def _linked(client, db, **shop):
    tenant, email = await _shop(db, **shop)
    _, r = await _pair(client, db, tenant)
    assert r.status_code == 200, r.text
    return tenant, email, {"Authorization": f"Bearer {r.json()['cle_api']}"}, r.json()


def _product(tenant, sku="R1", name="Robe wax Awa", price=15000, stock=4, image="https://agenc-ai.com/bob/uploads/products/a.jpg", **kw):
    return Product(tenant_id=tenant.id, sku=sku, name=name, price=price, currency="XOF", stock_quantity=stock,
                   image_url=image, active=True, **kw)


# --- Contrat OpenAPI : chaque réponse de Bob est conforme au fichier donné à Naomy -----------------------------------

def _contract():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "docs" / "naomy" / "bob-naomy-openapi-v1.3.1.yaml").read_text(encoding="utf-8"))


def conforms(instance, schema_name: str):
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    registry = Registry().with_resource("bob://contrat", Resource.from_contents(_contract(), default_specification=DRAFT202012))
    validator = jsonschema.Draft202012Validator({"$ref": f"bob://contrat#/components/schemas/{schema_name}"}, registry=registry)
    errors = sorted(validator.iter_errors(instance), key=str)
    assert not errors, [e.message for e in errors[:3]]


def test_contract_file_is_valid_openapi_and_lists_every_route():
    doc = _contract()
    assert doc["openapi"] == "3.1.0" and doc["info"]["version"] == "1.3.1"
    assert set(doc["paths"]) == {"/appairage", "/connexion", "/boutique", "/produits", "/produits/{produit_id}", "/stock",
                                 "/liens", "/liens/{code}", "/resultats", "/ventes", "/rappels",
                                 "/appairage/demande", "/assistant/messages"}  # lot 92
    events = set(doc["components"]["schemas"]["Evenement"]["properties"]["type"]["enum"])
    assert events == nb.EVENT_TYPES
    from app.api.naomy.routes import router

    served = {r.path.removeprefix("/api/v1/naomy") for r in router.routes if r.path.startswith("/api/v1/naomy/")}
    assert served == set(doc["paths"])


# --- Appairage -------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_pairing_rules(client, db_session, naomy_env, monkeypatch):
    free, free_email = await _shop(db_session, plan=TenantPlan.FREE)
    r = await client.post("/api/v1/naomy-connection/pairing", headers=await _login(client, free_email))
    assert r.status_code == 403 and r.json()["detail"] == "Naomy est incluse à partir de la formule Indépendant."
    view = (await client.get("/api/v1/naomy-connection", headers=await _login(client, free_email))).json()
    assert view["connected"] is False and "Indépendant" in view["refusal"]
    # Lot 92 : l'assistant seul reste possible en formule Gratuite (décision du 27/10).
    assert view["available"] is True and view["assistant_refusal"] is None

    shop, email = await _shop(db_session, plan=TenantPlan.INDEPENDANT)
    headers = await _login(client, email)
    url = (await client.post("/api/v1/naomy-connection/pairing", headers=headers)).json()["url"]
    assert url.startswith("https://agenc-ai.com/naomy/connexion/bob?code=") and len(url.rsplit("=", 1)[1]) == 32
    stored = (await db_session.execute(select(NaomyPairingCode))).scalar_one()
    assert stored.code_hash == nb.digest(url.rsplit("=", 1)[1])  # seulement l'empreinte en base

    monkeypatch.setattr(naomy_env, "naomy_platform_secret", "")
    r = await client.post("/api/v1/naomy-connection/pairing", headers=headers)
    assert r.status_code == 403 and "bientôt disponible" in r.json()["detail"]


@pytest.mark.asyncio
async def test_pairing_exchanges_the_code_once(client, db_session, naomy_env):
    tenant, _ = await _shop(db_session)
    code, r = await _pair(client, db_session, tenant)
    assert r.status_code == 200, r.text
    body = r.json()
    conforms(body, "ReponseAppairage")
    assert body["boutique_id"] == str(tenant.id) and body["cle_api"].startswith("bnk_") and body["secret_evenements"].startswith("bns_")
    connection = (await db_session.execute(select(NaomyConnection))).scalar_one()
    assert connection.api_key_hash == nb.digest(body["cle_api"]) and body["cle_api"] not in str(connection.__dict__)
    assert connection.events_secret == body["secret_evenements"] and connection.confirmation_number == "+2250701020312"
    opened = (await db_session.execute(select(NaomyEvent).where(NaomyEvent.type == "connexion.ouverte"))).scalar_one()
    assert opened.connection_id == connection.id

    raw, headers = _signed({"code": code, "compte_naomy": "nao_1", "numero_confirmation": "+2250701020312", "url_evenements": EVENTS_URL})
    again = await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)
    assert again.status_code == 409 and again.json() == {"erreur": "code_deja_utilise", "message": "Ce code d'appairage a déjà servi."}


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://agenc-ai.com/naomy/api/bob/evenements", "https://agenc-ai.com/bob/api/v1/x",
                                 "https://agenc-ai.com:8443/naomy/api/bob/e", "https://agenc-ai.com/naomy/api/bob/../../bob/x",
                                 "https://agenc-ai.com.evil.io/naomy/api/bob/e", "https://user:pw@agenc-ai.com/naomy/api/bob/e",
                                 "https://evil.io/naomy/api/bob/e"])
async def test_events_address_must_be_naomys(client, db_session, naomy_env, url):
    tenant, _ = await _shop(db_session)
    _, r = await _pair(client, db_session, tenant, url=url)
    assert r.status_code == 422 and r.json()["erreur"] == "url_evenements_refusee"
    assert (await db_session.execute(select(func.count(NaomyConnection.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_pairing_refusals(client, db_session, naomy_env, monkeypatch):
    tenant, _ = await _shop(db_session)
    code = nb.new_code()
    db_session.add(NaomyPairingCode(tenant_id=tenant.id, code_hash=nb.digest(code),
                                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await db_session.commit()
    payload = {"code": code, "compte_naomy": "n", "numero_confirmation": "+2250701020312", "url_evenements": EVENTS_URL}
    raw, headers = _signed(payload)
    assert (await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)).json()["erreur"] == "code_inconnu"
    raw, headers = _signed(payload, secret="un-autre-secret")
    assert (await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)).status_code == 401
    raw, headers = _signed(payload, age=400)
    assert (await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)).json()["erreur"] == "signature_invalide"
    raw, headers = _signed({**payload, "numero_confirmation": "0701020312"})
    assert (await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)).status_code == 400

    tenant.plan = TenantPlan.FREE  # repassée en gratuit entre la création du code et la confirmation
    await db_session.commit()
    _, r = await _pair(client, db_session, tenant)
    assert r.status_code == 403 and r.json()["erreur"] == "formule_insuffisante"

    monkeypatch.setattr(naomy_env, "naomy_platform_secret", "")
    raw, headers = _signed(payload)
    assert (await client.post("/api/v1/naomy/appairage", content=raw, headers=headers)).status_code == 503


@pytest.mark.asyncio
async def test_a_new_pairing_replaces_the_old_one(client, db_session, naomy_env):
    tenant, _, old_headers, _ = await _linked(client, db_session)
    _, r = await _pair(client, db_session, tenant, account="nao_2", number="+2250700000001")
    assert r.status_code == 200
    assert (await client.get("/api/v1/naomy/boutique", headers=old_headers)).status_code == 401
    closed = (await db_session.execute(select(NaomyEvent).where(NaomyEvent.type == "connexion.fermee"))).scalar_one()
    old = await db_session.get(NaomyConnection, closed.connection_id)
    assert old.naomy_account == "nao_1" and old.status == "CLOSED" and closed.data == {"par": "BOB"}


# --- API lue par Naomy -----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_errors_follow_the_contract(client, db_session, naomy_env, monkeypatch):
    r = await client.get("/api/v1/naomy/boutique")
    assert r.status_code == 401 and r.json() == {"erreur": "cle_revoquee", "message": "Clé inconnue ou connexion coupée."}
    conforms(r.json(), "Erreur")
    assert (await client.get("/api/v1/naomy/boutique", headers={"Authorization": "Bearer bnk_faux"})).status_code == 401
    _, _, headers, _ = await _linked(client, db_session)
    monkeypatch.setattr(nb, "RATE_LIMIT_PER_MINUTE", 2)
    codes = [(await client.get("/api/v1/naomy/boutique", headers=headers)).status_code for _ in range(3)]
    r = await client.get("/api/v1/naomy/boutique", headers=headers)
    assert codes == [200, 200, 429] and r.headers["retry-after"] == "60" and r.json()["erreur"] == "trop_de_requetes"


@pytest.mark.asyncio
async def test_shop_and_catalogue(client, db_session, naomy_env):
    tenant, _, headers, paired = await _linked(client, db_session)
    shop = (await client.get("/api/v1/naomy/boutique", headers=headers)).json()
    conforms(shop, "Boutique")
    assert shop == {**paired["boutique"]} and shop["secteur"] == "ONLINE_STORE" and shop["formule"] == "PRO"
    robe = _product(tenant, sheet={"sizes": "S à XL", "compare_at_price": "18000"}, bob_category="FASHION",
                    image_studio_url="https://agenc-ai.com/bob/uploads/products/a-studio.jpg", image_status="DONE")
    db_session.add_all([robe] + [_product(tenant, sku=f"P{i}", name=f"Pagne {i}") for i in range(3)]
                       + [_product(tenant, sku="OFF", name="Ancien", stock=0)])
    await db_session.flush()
    off = (await db_session.execute(select(Product).where(Product.sku == "OFF"))).scalar_one()
    off.active = False
    await db_session.commit()

    page = (await client.get("/api/v1/naomy/produits?limite=2", headers=headers)).json()
    assert len(page["elements"]) == 2 and page["curseur_suivant"]
    rest = (await client.get(f"/api/v1/naomy/produits?limite=2&curseur={page['curseur_suivant']}", headers=headers)).json()
    names = {p["nom"] for p in page["elements"] + rest["elements"]}
    assert names == {"Robe wax Awa", "Pagne 0", "Pagne 1", "Pagne 2"} and rest["curseur_suivant"] is None  # l'inactif n'y est pas
    for p in page["elements"] + rest["elements"]:
        conforms(p, "Produit")
    one = (await client.get(f"/api/v1/naomy/produits/{robe.id}", headers=headers)).json()
    assert one["prix"] == {"montant": "15000", "devise": "XOF"} and one["prix_barre"] == {"montant": "18000", "devise": "XOF"}
    assert one["fiche"] == {"sizes": "S à XL"} and one["photos"]["studio"].endswith("a-studio.jpg") and one["photos"]["portee"] is None
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    changed = (await client.get("/api/v1/naomy/produits", params={"modifies_depuis": since}, headers=headers)).json()
    assert {p["nom"]: p["actif"] for p in changed["elements"]}["Ancien"] is False  # désactivé : renvoyé pour être retiré

    other, _ = await _shop(db_session)
    foreign = _product(other, sku="X")
    db_session.add(foreign)
    await db_session.commit()
    assert (await client.get(f"/api/v1/naomy/produits/{foreign.id}", headers=headers)).status_code == 404
    stock = (await client.get(f"/api/v1/naomy/stock?ids={robe.id},{foreign.id}", headers=headers)).json()
    for item in stock["elements"]:
        conforms(item, "Stock")
    assert stock["elements"] == [{"produit_id": str(robe.id), "existe": True, "actif": True, "stock": 4,
                                  "prix": {"montant": "15000", "devise": "XOF"}},
                                 {"produit_id": str(foreign.id), "existe": False}]  # jamais le produit d'une autre boutique
    assert (await client.get("/api/v1/naomy/stock?ids=" + ",".join(str(uuid.uuid4()) for _ in range(51)), headers=headers)).status_code == 400


@pytest.mark.asyncio
async def test_a_broker_never_shows_a_price(client, db_session, naomy_env):
    tenant, _, headers, _ = await _linked(client, db_session, business_type="INSURANCE_BROKER")
    db_session.add(_product(tenant, sku="AUTO", name="Assurance auto", price=0, stock=0))
    await db_session.commit()
    item = (await client.get("/api/v1/naomy/produits", headers=headers)).json()["elements"][0]
    conforms(item, "Produit")
    assert item["prix"] is None and item["stock"] is None and item["prix_barre"] is None


# --- Liens suivis et résultats -----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_naomy_links_are_idempotent_and_kept_apart(client, db_session, naomy_env):
    tenant, email, headers, _ = await _linked(client, db_session)
    robe = _product(tenant)
    db_session.add(robe)
    await db_session.commit()
    body = {"nom": "Post robe Awa 25/10", "message": "Bonjour, je veux la robe Awa", "produit_id": str(robe.id),
            "reference_naomy": "post_8f2a"}
    first = await client.post("/api/v1/naomy/liens", json=body, headers=headers)
    second = await client.post("/api/v1/naomy/liens", json=body, headers=headers)
    assert (first.status_code, second.status_code) == (201, 200) and first.json() == second.json()
    link = first.json()
    conforms(link, "Lien")
    assert link["url"] == f"https://agenc-ai.com/bob/w/{link['code']}" and link["reference_naomy"] == "post_8f2a"
    assert (await client.post("/api/v1/naomy/liens", json={**body, "pirate": 1}, headers=headers)).status_code == 400
    assert (await client.post("/api/v1/naomy/liens", json={**body, "reference_naomy": "x", "produit_id": str(uuid.uuid4())},
                              headers=headers)).status_code == 404
    off = await client.patch(f"/api/v1/naomy/liens/{link['code']}", json={"actif": False}, headers=headers)
    assert off.json()["actif"] is False
    assert len((await client.get("/api/v1/naomy/liens", headers=headers)).json()["elements"]) == 1

    # Côté commerçant : le lien de Naomy n'est ni listé, ni modifiable, ni compté dans sa limite.
    merchant = await _login(client, email)
    assert (await client.get("/api/v1/contact-points", headers=merchant)).json() == []
    assert (await client.get("/api/v1/contact-points/limits", headers=merchant)).json()["used"] == 0
    cp = (await db_session.execute(select(ContactPoint))).scalar_one()
    assert (await client.delete(f"/api/v1/contact-points/{cp.id}", headers=merchant)).status_code == 404
    other_tenant, _, other_headers, _ = await _linked(client, db_session)
    assert (await client.patch(f"/api/v1/naomy/liens/{link['code']}", json={"actif": True}, headers=other_headers)).status_code == 404


@pytest.mark.asyncio
async def test_results_and_sales_are_aggregated(client, db_session, naomy_env):
    tenant, _, headers, _ = await _linked(client, db_session)
    robe = _product(tenant)
    db_session.add(robe)
    await db_session.commit()
    link = (await client.post("/api/v1/naomy/liens", headers=headers, json={"nom": "Post", "message": "Bonjour",
                                                                            "reference_naomy": "post_1"})).json()
    cp = (await db_session.execute(select(ContactPoint).where(ContactPoint.code == link["code"]))).scalar_one()
    cp.click_count = 54
    now = datetime.now(timezone.utc)
    customers = []
    for i, (source, ad) in enumerate([("NAOMY", None), ("NAOMY", None), ("AD_FACEBOOK", "1202")]):
        c = Customer(tenant_id=tenant.id, whatsapp_number=f"+22507000000{i}", first_name=f"Client {i}", acquisition_source=source,
                     acquisition_contact_point_id=cp.id if source == "NAOMY" else None, acquisition_ad_id=ad)
        db_session.add(c)
        customers.append(c)
    await db_session.flush()
    for c in customers:
        db_session.add(Conversation(tenant_id=tenant.id, customer_id=c.id))
    paid = Order(tenant_id=tenant.id, customer_id=customers[0].id, status=OrderStatus.PAID, total_amount=Decimal("30000"),
                 currency="XOF", paid_at=now)
    db_session.add_all([paid, Order(tenant_id=tenant.id, customer_id=customers[1].id, status=OrderStatus.PENDING,
                                    total_amount=Decimal("15000"), currency="XOF"),
                        Order(tenant_id=tenant.id, customer_id=customers[2].id, status=OrderStatus.PAID,
                              total_amount=Decimal("15000"), currency="XOF", paid_at=now)])
    await db_session.flush()
    db_session.add(OrderItem(order_id=paid.id, product_id=robe.id, quantity=2, unit_price=Decimal("15000"), subtotal=Decimal("30000")))
    await db_session.commit()

    today = datetime.now(timezone.utc).date()
    period = {"du": (today - timedelta(days=7)).isoformat(), "au": (today + timedelta(days=1)).isoformat()}
    res = (await client.get("/api/v1/naomy/resultats", params=period, headers=headers)).json()
    assert res["liens"] == [{"code": link["code"], "reference_naomy": "post_1", "clics": 54, "conversations": 2,
                             "nouveaux_clients": 2, "commandes": 2, "commandes_payees": 1,
                             "ventes_payees": {"montant": "30000", "devise": "XOF"}}]
    assert res["publicites"] == [{"id_publicite_meta": "1202", "conversations": 1, "nouveaux_clients": 1, "commandes": 1,
                                  "commandes_payees": 1, "ventes_payees": {"montant": "15000", "devise": "XOF"}}]
    for item in res["liens"]:
        conforms(item, "ResultatLien")
    for item in res["publicites"]:
        conforms(item, "ResultatPublicite")
    assert "Client" not in json.dumps(res) and "+2250700" not in json.dumps(res)  # jamais un client de Bob
    sales = (await client.get("/api/v1/naomy/ventes", params=period, headers=headers)).json()
    assert sales["elements"] == [{"produit_id": str(robe.id), "commandes_payees": 1, "quantite": 2,
                                  "ventes_payees": {"montant": "30000", "devise": "XOF"}}]
    conforms(sales["elements"][0], "VentesProduit")
    bad = await client.get("/api/v1/naomy/resultats", params={"du": "2026-01-01", "au": "2026-06-01"}, headers=headers)
    assert bad.status_code == 400 and bad.json()["erreur"] == "requete_invalide"


@pytest.mark.asyncio
async def test_a_customer_arriving_by_a_naomy_link_gets_the_naomy_source(client, db_session, naomy_env):
    from app.services.acquisition import channel_label, channel_of

    tenant, _, headers, _ = await _linked(client, db_session)
    link = (await client.post("/api/v1/naomy/liens", headers=headers, json={"nom": "Post", "message": "Bonjour",
                                                                            "reference_naomy": "post_2"})).json()
    source = Path(ROOT / "app" / "api" / "webhooks" / "whatsapp.py").read_text(encoding="utf-8")
    assert 'customer.acquisition_source = "NAOMY" if contact_point.created_by == "NAOMY" else "LINK"' in source
    assert channel_label(channel_of("NAOMY")) == "NaomyIA" and link["code"]


# --- Événements ------------------------------------------------------------------------------------------------------

async def _events(db, kind=None):
    stmt = select(NaomyEvent).order_by(NaomyEvent.created_at)
    if kind:
        stmt = stmt.where(NaomyEvent.type == kind)
    return (await db.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_product_changes_become_events_whatever_the_path(db_session):
    tenant, _ = await _shop(db_session)
    robe = _product(tenant)
    sans_photo = _product(tenant, sku="R2", name="Sac", image=None)
    db_session.add_all([robe, sans_photo])
    await db_session.commit()
    assert [e.data for e in await _events(db_session, "produit.cree")] == [{"produit_id": str(robe.id)}]

    robe.price = 12000
    robe.sheet = {"compare_at_price": "18000"}
    await db_session.commit()
    robe.price = 13000  # hausse : rien
    robe.stock_quantity = 0
    await db_session.commit()
    robe.stock_quantity = 3
    sans_photo.image_url = "https://agenc-ai.com/bob/uploads/products/b.jpg"
    await db_session.commit()
    robe.image_status = "DONE"
    robe.worn_status = "ACCEPTED"
    robe.active = False
    await db_session.commit()
    robe.sheet = {"compare_at_price": "17000"}  # prix barré déjà là, seulement modifié : pas un nouvel événement
    await db_session.commit()
    tenant.plan = TenantPlan.FREE
    await db_session.commit()

    kinds = [(e.type, e.data) for e in await _events(db_session)]
    assert [k[0] for k in kinds].count("produit.prix_barre_ajoute") == 1
    rid = str(robe.id)
    assert ("produit.prix_baisse", {"produit_id": rid, "ancien_prix": {"montant": "15000", "devise": "XOF"},
                                    "nouveau_prix": {"montant": "12000", "devise": "XOF"}}) in kinds
    assert ("produit.prix_barre_ajoute", {"produit_id": rid, "prix_barre": {"montant": "18000", "devise": "XOF"}}) in kinds
    assert [k for k in kinds if k[0] == "produit.prix_baisse"].__len__() == 1
    assert ("produit.rupture", {"produit_id": rid}) in kinds and ("produit.reapprovisionne", {"produit_id": rid, "stock": 3}) in kinds
    assert ("produit.cree", {"produit_id": str(sans_photo.id)}) in kinds  # première photo
    assert ("produit.photo_prete", {"produit_id": rid, "type": "studio"}) in kinds
    assert ("produit.photo_prete", {"produit_id": rid, "type": "portee"}) in kinds
    assert [k for k in kinds if k[0] == "produit.rupture"].__len__() == 2  # stock à 0, puis produit désactivé
    assert ("boutique.formule_changee", {"ancienne": "PRO", "nouvelle": "FREE"}) in kinds
    for _, data in kinds:
        conforms({"id": str(uuid.uuid4()), "type": "produit.cree", "boutique_id": str(tenant.id),
                  "date": "2026-10-26T10:00:00Z", "donnees": data}, "Evenement")


class Naomy:
    """Faux Naomy : vérifie la signature comme le contrat le demande et répond ce qu'on lui dit."""

    def __init__(self, secret, answers=None):
        self.secret, self.answers, self.received = secret, list(answers or []), []

    async def __call__(self, url, body, headers):
        expected = "sha256=" + hmac.new(self.secret.encode(), headers["X-Bob-Timestamp"].encode() + b"." + body,
                                        hashlib.sha256).hexdigest()
        assert hmac.compare_digest(expected, headers["X-Bob-Signature"]) and url == EVENTS_URL
        self.received.append(json.loads(body))
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.asyncio
async def test_dispatch_signs_skips_and_retries(client, db_session, naomy_env):
    before, _ = await _shop(db_session)
    db_session.add(_product(before, sku="OLD"))  # boutique reliée plus tard : cet événement ne part pas
    await db_session.commit()
    unlinked, _ = await _shop(db_session)
    db_session.add(_product(unlinked, sku="U"))
    await db_session.commit()
    _, r = await _pair(client, db_session, before)
    secret = r.json()["secret_evenements"]
    robe = _product(before, sku="NEW")
    db_session.add(robe)
    await db_session.commit()
    robe.image_url = None
    await db_session.commit()
    robe.image_url = "https://agenc-ai.com/bob/uploads/products/c.jpg"  # nouvelle « première photo » : doublon écarté
    await db_session.commit()

    naomy = Naomy(secret)
    report = await nb.dispatch(db_session, sender=naomy)
    assert [e["type"] for e in naomy.received] == ["connexion.ouverte", "produit.cree"]
    assert report["sent"] == 2 and report["skipped"] >= 3
    for e in naomy.received:
        conforms(e, "Evenement")
        assert e["boutique_id"] == str(before.id)
    statuses = {e.data.get("produit_id", e.type): e.status for e in await _events(db_session)}
    assert set(statuses.values()) <= {"SENT", "SKIPPED"}

    robe.price = 9000
    await db_session.commit()
    now = datetime.now(timezone.utc)
    failing = Naomy(secret, [500])
    assert (await nb.dispatch(db_session, now=now, sender=failing))["retry"] == 1
    evt = (await _events(db_session, "produit.prix_baisse"))[0]
    assert evt.attempts == 1 and evt.next_attempt_at.replace(tzinfo=timezone.utc) == now + timedelta(minutes=1)
    assert (await nb.dispatch(db_session, now=now, sender=failing))["retry"] == 0  # pas avant l'heure
    later = now
    for minutes in nb.RETRY_MINUTES:
        later = later + timedelta(minutes=minutes)
        await nb.dispatch(db_session, now=later, sender=Naomy(secret, [ConnectionError("down")]))
    await db_session.refresh(evt)
    assert evt.status == "FAILED" and evt.attempts == 6 and evt.last_error.startswith("Naomy injoignable")

    robe.stock_quantity = 0
    await db_session.commit()
    await nb.dispatch(db_session, sender=Naomy(secret, [401]))
    assert (await _events(db_session, "produit.rupture"))[0].status == "FAILED"  # 401 : pas de renvoi


@pytest.mark.asyncio
async def test_closing_sends_the_goodbye_with_the_old_secret(client, db_session, naomy_env):
    tenant, email, headers, paired = await _linked(client, db_session)
    await nb.dispatch(db_session, sender=Naomy(paired["secret_evenements"]))
    r = await client.delete("/api/v1/naomy/connexion", headers=headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/naomy/boutique", headers=headers)).status_code == 401
    naomy = Naomy(paired["secret_evenements"])
    await nb.dispatch(db_session, sender=naomy)
    assert [(e["type"], e["donnees"]) for e in naomy.received] == [("connexion.fermee", {"par": "NAOMY"})]
    db_session.add(_product(tenant, sku="AFTER"))
    await db_session.commit()
    assert (await nb.dispatch(db_session, sender=naomy))["sent"] == 0  # plus rien après la déconnexion


@pytest.mark.asyncio
async def test_purge(db_session):
    tenant, _ = await _shop(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    db_session.add_all([NaomyEvent(tenant_id=tenant.id, type="produit.cree", data={}, status="SENT", created_at=old),
                        NaomyEvent(tenant_id=tenant.id, type="produit.cree", data={}, status="PENDING", created_at=old),
                        NaomyEvent(tenant_id=tenant.id, type="produit.cree", data={}, status="SKIPPED",
                                   created_at=datetime.now(timezone.utc) - timedelta(days=3))])
    await db_session.commit()
    assert (await nb.purge(db_session))["events"] == 2
    assert [e.status for e in await _events(db_session)] == ["PENDING"]


# --- Rappels par e-mail ----------------------------------------------------------------------------------------------

class Mailer:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def __call__(self, **kw):
        self.sent.append(kw)
        return self.ok


@pytest.mark.asyncio
async def test_reminders_are_written_by_bob(client, db_session, naomy_env, monkeypatch):
    tenant, email, headers, _ = await _linked(client, db_session)
    robe, sac = _product(tenant), _product(tenant, sku="S", name="Sac cabas")
    db_session.add_all([robe, sac])
    await db_session.commit()
    mailer = Mailer()
    monkeypatch.setattr("app.services.email_service.send_email", mailer)
    body = {"reference_naomy": "rappel_1", "nombre_propositions": 3, "produit_ids": [str(robe.id), str(sac.id)]}
    r = await client.post("/api/v1/naomy/rappels", json=body, headers=headers)
    assert r.status_code == 202 and r.json() == {"reference_naomy": "rappel_1", "statut": "accepte"}
    mail = mailer.sent[0]
    assert mail["to"] == email and mail["subject"] == "Naomy a préparé 3 propositions pour vous"
    assert "pour vos produits « Robe wax Awa » et « Sac cabas »" in mail["body"]
    assert "https://wa.me/33780841269?text=posts" in mail["body"] and "15000" not in mail["body"]
    assert "/api/v1/naomy-rappels/desinscription?c=" in mail["body"]
    assert mail["extra_headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    again = await client.post("/api/v1/naomy/rappels", json=body, headers=headers)
    assert again.status_code == 202 and again.json()["statut"] == "deja_envoye" and len(mailer.sent) == 1
    second = await client.post("/api/v1/naomy/rappels", json={**body, "reference_naomy": "rappel_2"}, headers=headers)
    assert second.status_code == 429 and int(second.headers["retry-after"]) >= 60
    assert (await client.post("/api/v1/naomy/rappels", json={**body, "texte": "Cliquez ici"}, headers=headers)).status_code == 400

    # Lien de désinscription en un clic, puis 409.
    token = mail["body"].split("desinscription?")[1].split()[0]
    params = dict(p.split("=") for p in token.split("&"))
    assert (await client.get("/api/v1/naomy-rappels/desinscription", params={**params, "t": "faux"})).status_code == 404
    assert (await client.post("/api/v1/naomy-rappels/desinscription", params=params)).status_code == 200
    connection = (await db_session.execute(select(NaomyConnection))).scalar_one()
    await db_session.refresh(connection)
    assert connection.reminders_unsubscribed_at is not None
    db_session.add(NaomyReminder(tenant_id=tenant.id, reference="hier", sent_on=datetime.now(timezone.utc).date() - timedelta(days=1), sent=True))
    await db_session.execute(NaomyReminder.__table__.delete().where(NaomyReminder.reference == "rappel_1"))
    await db_session.commit()
    r = await client.post("/api/v1/naomy/rappels", json={**body, "reference_naomy": "rappel_3"}, headers=headers)
    assert r.status_code == 409 and r.json()["erreur"] == "desinscrit"

    # Le commerçant les rallume dans Bob, puis les décoche.
    merchant = await _login(client, email)
    view = (await client.put("/api/v1/naomy-connection/reminders", json={"enabled": True}, headers=merchant)).json()
    assert view["reminders_enabled"] is True
    await client.put("/api/v1/naomy-connection/reminders", json={"enabled": False}, headers=merchant)
    r = await client.post("/api/v1/naomy/rappels", json={**body, "reference_naomy": "rappel_4"}, headers=headers)
    assert r.json()["erreur"] == "rappels_desactives"


@pytest.mark.asyncio
async def test_reminder_needs_a_number_and_a_working_mailbox(client, db_session, naomy_env, monkeypatch):
    _, _, headers, _ = await _linked(client, db_session)
    monkeypatch.setattr("app.services.email_service.send_email", Mailer(ok=False))
    body = {"reference_naomy": "r1", "nombre_propositions": 1}
    r = await client.post("/api/v1/naomy/rappels", json=body, headers=headers)
    assert r.status_code == 503 and r.json()["erreur"] == "envoi_impossible"
    assert (await db_session.execute(select(func.count(NaomyReminder.id)))).scalar_one() == 0  # rien de décompté
    monkeypatch.setattr(naomy_env, "naomy_whatsapp_number", "")
    assert (await client.post("/api/v1/naomy/rappels", json=body, headers=headers)).json()["erreur"] == "rappels_indisponibles"


# --- Tableau de bord et monitoring -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_card_and_disconnect(client, db_session, naomy_env):
    tenant, email, headers, _ = await _linked(client, db_session)
    merchant = await _login(client, email)
    view = (await client.get("/api/v1/naomy-connection", headers=merchant)).json()
    assert view["connected"] is True and view["number"] == "+22507 •• •• 12" and view["suspended"] is False
    tenant.plan = TenantPlan.FREE
    await db_session.commit()
    assert (await client.get("/api/v1/naomy-connection", headers=merchant)).json()["suspended"] is True  # liaison gardée
    assert (await client.get("/api/v1/naomy/boutique", headers=headers)).json()["formule"] == "FREE"
    closed = (await client.delete("/api/v1/naomy-connection", headers=merchant)).json()
    assert closed["connected"] is False
    assert (await client.get("/api/v1/naomy/boutique", headers=headers)).status_code == 401
    assert (await client.delete("/api/v1/naomy-connection", headers=merchant)).status_code == 404


def test_dashboard_has_the_naomy_card():
    assert 'id="naomy-card"' in DASH and "Naomy, votre agent marketing" in DASH
    js = DASH[DASH.index("async function loadNaomyCard() {"):DASH.index("async function disconnectNaomy() {")]
    assert '"/api/v1/naomy-connection/pairing"' in js and 'rel="noopener noreferrer"' in js and "valable 15 minutes" in js
    assert "Me prévenir par e-mail des propositions de Naomy" in js and "jamais le nom ni le numéro de vos clients" in DASH
    assert "loadNaomyCard();  // lot 89" in DASH


@pytest.mark.asyncio
async def test_monitoring_light(db_session, naomy_env, monkeypatch):
    from app.services.monitoring_status import naomy_check

    now = datetime.now(timezone.utc)
    assert (await naomy_check(db_session, now, naomy_env))["detail"] == "Configuré · 0 boutique reliée"
    tenant, _ = await _shop(db_session)
    db_session.add(NaomyEvent(tenant_id=tenant.id, type="produit.cree", data={}, status="FAILED", created_at=now))
    await db_session.commit()
    light = await naomy_check(db_session, now, naomy_env)
    assert light["status"] == "ko" and light["detail"].startswith("1 événement abandonné en 24 h")
    monkeypatch.setattr(naomy_env, "naomy_platform_secret", "")
    assert (await naomy_check(db_session, now, naomy_env))["status"] == "idle"
    from app.services.monitoring import TASK_LABELS
    from app.workers.celery_app import celery_app

    tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
    assert {"app.workers.naomy.send_naomy_events_task", "app.workers.naomy.purge_naomy_task"} <= tasks & set(TASK_LABELS)
