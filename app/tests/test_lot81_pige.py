"""
Lot 81 — assistant de pige (décisions du 17/10) : Indépendant 30 annonces par mois, Starter 50, Pro sans plafond avec
la lecture par IA et le rapprochement avec les acheteurs ; prix de comparaison DVF ; Bob ne contacte jamais le vendeur ;
« agences s'abstenir » et oppositions respectées ; données effacées après 6 mois.
"""
import gzip
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.customer import Customer
from app.models.pige import DvfDepartment, DvfSale, PigeListing
from app.models.prospect_profile import ProspectProfile
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import dvf, pige

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)

AD_SALE = """Appartement T3 lumineux avec balcon
Vends T3 de 68 m² au 3e étage avec ascenseur, quartier Monplaisir.
Prix : 245 000 €. DPE : D. Charges 120 €/mois.
69008 Lyon
Tél 06 12 34 56 78"""
AD_RENT = """Studio meublé à louer
Loyer 620 € / mois charges comprises, 24 m2, Villeurbanne (69100). Contact : 07.11.22.33.44"""
AD_HOUSE = """Maison 5 pièces avec jardin de 600 m²
Maison de 120 m² sur terrain de 600 m2 à Bron 69500. 435 000 euros, prix au m² 3 625 €/m². DPE classe énergie F"""
AD_NO_AGENCY = AD_SALE.replace("Tél", "Agences s'abstenir. Tél")


async def _agency(db, plan=TenantPlan.STARTER, business_type="REAL_ESTATE"):
    email = f"p{uuid.uuid4().hex[:8]}@l81.fr"
    tenant = Tenant(name="Agence du Parc", country="FR", currency="EUR", email=email, plan=plan,
                    business_type=business_type, business_type_chosen_at=NOW)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="Julie Martin", role=Role.OWNER))
    await db.commit()
    return tenant


async def _headers(client, tenant):
    token = (await client.post("/api/v1/auth/login", data={"username": tenant.email, "password": "x"})).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Lecture de l'annonce ----------------------------------------------------------------------------------

def test_rules_read_a_sale_ad():
    out = pige.extract(AD_SALE, "https://www.leboncoin.fr/ad/ventes_immobilieres/123")
    assert out == {"site": "Leboncoin", "title": "Appartement T3 lumineux avec balcon", "transaction": "VENTE",
                   "price": Decimal("245000.0"), "surface": Decimal("68"), "rooms": 3, "dpe": "D", "postal_code": "69008",
                   "city": "Lyon", "owner_phone": "+33612345678", "no_agency": False, "property_type": "APPARTEMENT"}


def test_rules_read_a_rental_and_a_house():
    rent = pige.extract(AD_RENT, "https://www.pap.fr/annonces/x")
    assert (rent["site"], rent["transaction"], rent["price"], rent["surface"], rent["rooms"]) == \
        ("PAP", "LOCATION", Decimal("620.0"), Decimal("24"), 1)
    assert (rent["city"], rent["postal_code"], rent["owner_phone"]) == ("Villeurbanne", "69100", "+33711223344")
    house = pige.extract(AD_HOUSE)
    assert house["surface"] == Decimal("120")  # le terrain n'est pas la surface habitable
    assert house["price"] == Decimal("435000.0")  # « 3 625 €/m² » n'est pas un prix
    assert (house["city"], house["postal_code"], house["dpe"], house["property_type"]) == ("Bron", "69500", "F", "MAISON")
    assert house["site"] is None and house["owner_phone"] is None
    # Seul un prix au m² est écrit : ce n'est pas le prix du bien.
    assert pige.extract("Appartement à vendre, 68 m², prix au m² 4 500 €/m², Lyon")["price"] is None


@pytest.mark.parametrize("text", ["Agences s'abstenir", "Pas d'agence svp", "Non aux agences", "agence non merci",
                                  "Vente sans intermédiaire"])
def test_refusal_of_agencies_is_seen(text):
    assert pige.extract(f"Maison à vendre 300 000 €. {text}.")["no_agency"] is True
    assert pige.extract("Maison à vendre 300 000 €, de particulier à particulier.")["no_agency"] is False


def test_values_are_checked_and_personal_data_is_masked_for_the_ai():
    assert pige.clean_fields({"transaction": "LOUER", "property_type": "CHATEAU", "surface": "abc", "price": "10",
                              "rooms": "trois", "dpe": "h", "postal_code": "6900"}) == {}
    assert pige.clean_fields({"owner_phone": "06 12 34 56 78", "dpe": "c", "rooms": "4", "price": "1 150"}) == \
        {"owner_phone": "+33612345678", "dpe": "C", "rooms": 4, "price": Decimal("1150")}
    masked = pige.mask_personal("Appelez le 06 12 34 56 78 ou jean.dupont@mail.fr")
    assert "06 12" not in masked and "jean" not in masked and "[téléphone]" in masked and "[email]" in masked


# --- Ventes DVF --------------------------------------------------------------------------------------------

DVF_HEADER = "id_mutation,date_mutation,nature_mutation,valeur_fonciere,code_postal,code_commune,nom_commune,type_local,surface_reelle_bati,nombre_pieces_principales\n"


def _dvf_rows(n=8, start=0):
    rows = []
    for i in range(start, start + n):
        rows.append(f"2025-{i},2025-0{(i % 9) + 1}-15,Vente,{200000 + i * 10000},69008,69388,Lyon 8e Arrondissement,Appartement,{60 + i},3")
    rows.append("2025-X,2025-03-01,Vente,500000,69008,69388,Lyon 8e Arrondissement,Appartement,70,3")  # 2 logements :
    rows.append("2025-X,2025-03-01,Vente,500000,69008,69388,Lyon 8e Arrondissement,Appartement,65,3")  # écartée
    rows.append("2025-D,2025-03-01,Vente,20000,69008,69388,Lyon 8e Arrondissement,Dépendance,,")      # sans logement
    rows.append("2025-E,2025-03-01,Echange,300000,69008,69388,Lyon 8e Arrondissement,Appartement,70,3")  # pas une vente
    rows.append("2025-M,2025-04-01,Vente,250000,69008,69388,Lyon 8e Arrondissement,Maison,100,4")
    rows.append("2025-M,2025-04-01,Vente,250000,69008,69388,Lyon 8e Arrondissement,Dépendance,,")      # maison + garage : gardée
    return (DVF_HEADER + "\n".join(rows) + "\n").encode()


def test_dvf_keeps_simple_sales_of_one_home():
    sales = dvf.parse(gzip.compress(_dvf_rows(3)), "69")
    assert [(s["property_type"], s["surface"], int(s["price"])) for s in sales] == [
        ("APPARTEMENT", 60, 200000), ("APPARTEMENT", 61, 210000), ("APPARTEMENT", 62, 220000), ("MAISON", 100, 250000)]
    assert sales[0]["sold_on"] == date(2025, 1, 15) and sales[0]["postal_code"] == "69008"


@pytest.mark.parametrize("postal, department", [("69003", "69"), ("01000", "01"), ("20000", "2A"), ("20200", "2B"),
                                                 ("97400", "974"), ("6900", None), (None, None)])
def test_department_of_a_postal_code(postal, department):
    assert dvf.department_of(postal) == department


@pytest.mark.asyncio
async def test_dvf_loading_and_comparison(db_session):
    asked = []

    async def fetch(url):
        asked.append(url)
        return gzip.compress(_dvf_rows(8)) if "/2025/" in url else None  # les autres années : pas (encore) publiées

    assert await dvf.compare(db_session, "APPARTEMENT", "69008", 68, 245000) == {
        "status": "PENDING", "message": "Ventes du département 69 en cours de chargement (sous 24 h)."}
    assert await dvf.load_department(db_session, "69", NOW, fetch) == 9
    assert asked[0] == "https://files.data.gouv.fr/geo-dvf/latest/csv/2026/departements/69.csv.gz" and len(asked) == 4
    row = await db_session.get(DvfDepartment, "69")
    assert row.sales == 9 and row.years == "2025" and row.error is None
    result = await dvf.compare(db_session, "APPARTEMENT", "69008", 68, 245000)
    assert result["status"] == "OK" and result["count"] == 8 and result["scope"] == "même type, surface proche"
    assert result["listing_m2"] == 3603 and result["median_m2"] == 3700 and result["delta_pct"] == -3
    assert result["period"] == "ventes de 01/2025 à 08/2025" and "DVF" in result["note"]
    assert (await dvf.compare(db_session, "MAISON", "69008", 100, 300000))["status"] == "TOO_FEW"
    wide = await dvf.compare(db_session, "APPARTEMENT", "69008", 200, 600000)  # aucune surface proche : tout le code postal
    assert wide["scope"] == "même type" and wide["count"] == 8
    assert await dvf.compare(db_session, "TERRAIN", "69008", 100, 300000) is None
    # Rechargement : les anciennes ventes du département sont remplacées, jamais doublées.
    await dvf.load_department(db_session, "69", NOW, fetch)
    assert len((await db_session.execute(select(DvfSale))).scalars().all()) == 9
    assert not await dvf.needs_loading(db_session, "69", NOW + timedelta(days=100))
    assert await dvf.needs_loading(db_session, "69", NOW + timedelta(days=200))

    async def broken(url):
        raise ConnectionError("réseau")

    assert await dvf.load_department(db_session, "69", NOW, broken) == 0
    assert len((await db_session.execute(select(DvfSale))).scalars().all()) == 9  # les ventes restent
    assert (await db_session.get(DvfDepartment, "69")).error == "Chargement impossible : ConnectionError"


# --- Score et argumentaire ---------------------------------------------------------------------------------

def _listing(**kw):
    defaults = {"title": "T3 lumineux", "transaction": "VENTE", "owner_phone": "+33612345678", "no_agency": False,
                "online_days": None, "price_dropped": False, "dpe": None, "status": "A_APPELER", "site": "Leboncoin",
                "city": "Lyon", "analysis": None}
    return PigeListing(**{**defaults, **kw})


@pytest.mark.parametrize("kw, dvf_delta, buyers, priority, points", [
    ({}, None, [], "BASSE", 20),
    ({"online_days": 75, "price_dropped": True}, None, [], "HAUTE", 60),
    ({"online_days": 40}, 12, [], "MOYENNE", 55),
    ({"dpe": "G"}, None, ["Claire"], "MOYENNE", 55),
    ({"online_days": 90}, 15, ["Claire", "Bruno"], "HAUTE", 90),
    ({"owner_phone": None}, None, [], "BASSE", 0),
    ({"no_agency": True, "online_days": 90}, 15, ["Claire"], "NE_PAS_APPELER", 0),
])
def test_priority_rules(kw, dvf_delta, buyers, priority, points):
    comparison = {"status": "OK", "delta_pct": dvf_delta} if dvf_delta is not None else None
    assert pige.score(_listing(**kw), comparison, buyers, False)[:2] == (priority, points)
    assert pige.score(_listing(), None, [], True)[0] == "NE_PAS_APPELER"


def test_call_script_is_legal_and_filled():
    tenant = Tenant(name="Agence du Parc")
    listing = _listing(online_days=45, dpe="F", analysis={"priority": "HAUTE", "buyers": ["Claire", "Bruno"], "dvf": {
        "status": "OK", "median_m2": 3365, "postal_code": "69008", "period": "ventes de 01/2025 à 09/2025"}})
    out = pige.script(tenant, listing, "Julie")
    assert out["before"] == ["Appel manuel seulement : pas de SMS, d'email ni de WhatsApp sans l'accord de la personne.",
                             "Vérifiez que ce numéro n'est pas inscrit sur Bloctel avant d'appeler."]
    intro = out["steps"][0]["text"]
    assert intro.startswith("Bonjour, Julie, de Agence du Parc. Je vous appelle au sujet de votre annonce sur Leboncoin")
    assert "J'ai trouvé votre numéro sur cette annonce" in intro and "ne pas être recontacté" in intro
    hook = out["steps"][1]["text"]
    assert "2 acheteurs qui cherchent un bien comme le vôtre à Lyon" in hook and "3 365 € le m²" in hook
    assert "45 jours" in hook and "DPE F" in hook
    blocked = pige.script(tenant, _listing(analysis={"priority": "NE_PAS_APPELER", "reasons": ["l'annonce refuse les agences : à respecter"]}))
    assert blocked["steps"] == [] and blocked["blocked"].startswith("l'annonce refuse les agences")


# --- Acheteurs (Pro) ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buyers_match_only_for_pro(db_session):
    stamp = datetime.now(timezone.utc).isoformat()
    found = {}
    for plan in (TenantPlan.STARTER, TenantPlan.PRO):
        tenant = await _agency(db_session, plan)
        for name, project in (("Claire", {"zone": "Lyon", "budget": "250 000 €"}), ("Bruno", {"zone": "Villeurbanne"}),
                              ("Chloé", {"zone": "Lyon", "budget": "200 000 €"})):
            customer = Customer(tenant_id=tenant.id, whatsapp_number=f"336{uuid.uuid4().int % 10**8:08d}", first_name=name)
            db_session.add(customer)
            await db_session.flush()
            db_session.add(ProspectProfile(tenant_id=tenant.id, customer_id=customer.id,
                                           real_estate={"projects": {"ACHAT": {**project, "updated_at": stamp}}}))
        await db_session.commit()
        listing = PigeListing(tenant_id=tenant.id, created_by="x", **pige.clean_fields(pige.extract(AD_SALE)))
        found[plan] = await pige.matching_buyers(db_session, tenant, listing)
    assert found[TenantPlan.STARTER] == [] and found[TenantPlan.PRO] == ["Claire"]


# --- API -------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_flow_for_a_starter_agency(client, db_session):
    tenant = await _agency(db_session)
    headers = await _headers(client, tenant)
    settings = (await client.get("/api/v1/pige/settings", headers=headers)).json()
    assert settings["quota"] == 50 and settings["used"] == 0 and settings["pro"] is False and settings["ai"] is False
    created = await client.post("/api/v1/pige", headers=headers, json={
        "text": AD_SALE, "url": "https://www.leboncoin.fr/ad/1", "online_days": 70, "price_dropped": True})
    assert created.status_code == 201
    body = created.json()
    assert body["extraction"] == "RULES" and body["price"] == 245000 and body["owner_phone"] == "+33612345678"
    assert body["analysis"]["priority"] == "HAUTE" and body["analysis"]["priority_label"] == "Haute"
    assert body["analysis"]["dvf"]["status"] == "PENDING" and body["analysis"]["buyers"] == []
    again = await client.post("/api/v1/pige", headers=headers, json={"text": AD_SALE})
    assert again.status_code == 409 and again.json()["detail"] == "Cette annonce est déjà dans votre pige."
    short = await client.post("/api/v1/pige", headers=headers, json={"text": "T3 Lyon"})
    assert short.status_code == 422
    script = (await client.get(f"/api/v1/pige/{body['id']}/script", headers=headers)).json()
    assert script["steps"][0]["text"].startswith("Bonjour, Julie, de Agence du Parc.")
    callback = (NOW + timedelta(days=2)).isoformat()
    updated = (await client.put(f"/api/v1/pige/{body['id']}", headers=headers, json={
        "status": "A_RAPPELER", "next_call_at": callback, "notes": "Rappeler après 18 h", "rooms": 4, "dpe": "z"})).json()
    assert updated["status_label"] == "À rappeler" and updated["rooms"] == 4 and updated["dpe"] is None
    assert updated["notes"] == "Rappeler après 18 h" and updated["next_call_at"].startswith("2026-10-22")
    listing = await db_session.get(PigeListing, uuid.UUID(body["id"]))
    phone_hash = listing.phone_hash

    opposed = (await client.put(f"/api/v1/pige/{body['id']}", headers=headers, json={"status": "OPPOSITION"})).json()
    assert opposed["owner_phone"] is None and opposed["text"] is None and opposed["source_url"] is None
    assert opposed["notes"] is None and opposed["analysis"]["priority"] == "NE_PAS_APPELER"
    await db_session.refresh(listing)
    assert listing.phone_hash == phone_hash  # gardée pour ne jamais le rappeler
    refused = await client.post("/api/v1/pige", headers=headers, json={"text": AD_SALE.replace("Monplaisir", "Bachut")})
    assert refused.status_code == 409 and "ne plus être contacté" in refused.json()["detail"]
    assert (await client.delete(f"/api/v1/pige/{body['id']}", headers=headers)).status_code == 409
    listed = (await client.get("/api/v1/pige", headers=headers)).json()
    assert [item["status"] for item in listed] == ["OPPOSITION"]
    other = await _agency(db_session)
    assert (await client.get(f"/api/v1/pige/{body['id']}/script", headers=await _headers(client, other))).status_code == 404


@pytest.mark.asyncio
async def test_monthly_quota_and_plans(client, db_session):
    tenant = await _agency(db_session, TenantPlan.INDEPENDANT)
    for i in range(30):
        db_session.add(PigeListing(tenant_id=tenant.id, created_by="x", title=f"A{i}", created_at=datetime.now(timezone.utc)))
    db_session.add(PigeListing(tenant_id=tenant.id, created_by="x", title="mois dernier",
                               created_at=datetime.now(timezone.utc) - timedelta(days=40)))
    await db_session.commit()
    headers = await _headers(client, tenant)
    full = await client.post("/api/v1/pige", headers=headers, json={"text": AD_RENT})
    assert full.status_code == 403 and full.json()["detail"].startswith("Plafond du mois atteint (30 annonces)")
    free = await _agency(db_session, TenantPlan.FREE)
    free_headers = await _headers(client, free)
    assert (await client.get("/api/v1/pige/settings", headers=free_headers)).json() == {"available": False, "message": pige.NOT_AVAILABLE}
    assert (await client.post("/api/v1/pige", headers=free_headers, json={"text": AD_RENT})).status_code == 403
    store = await _agency(db_session, TenantPlan.PRO, business_type="ONLINE_STORE")
    assert (await client.get("/api/v1/pige/settings", headers=await _headers(client, store))).status_code == 403
    assert pige.monthly_quota(Tenant(plan=TenantPlan.STARTER)) == 50 and pige.monthly_quota(Tenant(plan=TenantPlan.PRO)) is None


@pytest.mark.asyncio
async def test_pro_uses_the_ai_without_the_phone_number(client, db_session, monkeypatch):
    seen = {}

    async def fake_ai(text):
        seen["text"] = pige.mask_personal(text)
        return ({"transaction": "VENTE", "property_type": "APPARTEMENT", "city": "Lyon 8e", "postal_code": "69008",
                 "surface": 68, "rooms": 3, "price": 245000, "dpe": "D", "no_agency": False,
                 "owner_phone": "+33799999999"}, {"prompt_tokens": 400, "completion_tokens": 80})

    monkeypatch.setattr(pige, "ai_extract", fake_ai)
    tenant = await _agency(db_session, TenantPlan.PRO)
    body = (await client.post("/api/v1/pige", headers=await _headers(client, tenant), json={"text": AD_SALE})).json()
    assert body["extraction"] == "AI" and body["city"] == "Lyon 8e"
    assert body["owner_phone"] == "+33612345678"  # le numéro vient de l'annonce, jamais de l'IA
    assert "06 12 34 56 78" not in seen["text"]
    from app.models.llm_usage import LlmUsage

    usage = (await db_session.execute(select(LlmUsage).where(LlmUsage.tenant_id == tenant.id))).scalar_one()
    assert usage.kind == "PIGE" and usage.prompt_tokens == 400 and usage.conversation_id is None


@pytest.mark.asyncio
async def test_no_ai_without_configuration(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "mistral_api_key", "")
    assert await pige.ai_extract(AD_SALE) == (None, None)


# --- Effacement et tâches ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_purge_after_six_months(db_session):
    tenant = await _agency(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=200)
    db_session.add_all([
        PigeListing(tenant_id=tenant.id, created_by="x", title="vieux", last_activity_at=old),
        PigeListing(tenant_id=tenant.id, created_by="x", title="mandat", status="MANDAT", last_activity_at=old),
        PigeListing(tenant_id=tenant.id, created_by="x", title="opposition", status="OPPOSITION", last_activity_at=old),
        PigeListing(tenant_id=tenant.id, created_by="x", title="récent"),
    ])
    await db_session.commit()
    assert await pige.purge(db_session) == 1
    titles = sorted((await db_session.execute(select(PigeListing.title))).scalars().all())
    assert titles == ["mandat", "opposition", "récent"]


@pytest.mark.asyncio
async def test_worker_loads_only_the_useful_departments(db_session):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.workers import pige as worker

    tenant = await _agency(db_session)
    db_session.add_all([PigeListing(tenant_id=tenant.id, created_by="x", transaction="VENTE", postal_code="69008"),
                        PigeListing(tenant_id=tenant.id, created_by="x", transaction="LOCATION", postal_code="75011")])
    await db_session.commit()
    factory = async_sessionmaker(bind=db_session.bind, expire_on_commit=False, class_=AsyncSession)

    async def fetch(url):
        return gzip.compress(_dvf_rows(6)) if "/2025/" in url else None

    report = await worker.refresh_dvf(factory, NOW, fetch)
    assert report == {"loaded": ["69"], "failed": 0}
    assert (await worker.refresh_dvf(factory, NOW, fetch))["loaded"] == []  # déjà chargé


def test_dashboard_worker_and_schedule():
    from app.services.monitoring import TASK_LABELS
    from app.workers.celery_app import TASK_MODULES, celery_app

    assert "app.workers.pige" in TASK_MODULES
    schedule = celery_app.conf.beat_schedule
    assert schedule["pige-dvf"]["task"] == "app.workers.pige.refresh_dvf_task"
    assert schedule["pige-purge"]["task"] == "app.workers.pige.purge_listings_task"
    assert "app.workers.pige.refresh_dvf_task" in TASK_LABELS and "app.workers.pige.purge_listings_task" in TASK_LABELS
    for marker in ('id="nav-pige" class="estate-only"', 'id="tab-pige"', "loadPige()", "Vérifiez la liste Bloctel",
                   '"/api/v1/pige"', "togglePigeScript(", "Ne plus contacter"):
        assert marker in HTML, marker
