"""
Lot 87 — Bob retouche les photos (décisions de Thérèse du 20/10) : fond blanc studio pour tous les produits (Photoroom
dans le quota de la formule, notre serveur au-delà et en gratuit), photo portée sur mannequin pour les habits seulement
(FASHN, validée par le commerçant, quotas), montage « Photo réelle | Portée », signature « Édité par Bob AI ».
"""
import base64
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.image_job import ImageJob
from app.models.product import Product
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User
from app.services import photo_studio as ps

DASH = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


def _photo_bytes(color=(200, 30, 40), size=(400, 300), fmt="JPEG") -> bytes:
    image = Image.new("RGB", size, (120, 90, 60))
    image.paste(Image.new("RGB", (160, 160), color), (120, 70))
    out = io.BytesIO()
    image.save(out, fmt)
    return out.getvalue()


def _cutout_png(*_args) -> bytes:
    item = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    item.paste(Image.new("RGBA", (160, 160), (200, 30, 40, 255)), (120, 70))
    out = io.BytesIO()
    item.save(out, "PNG")
    return out.getvalue()


async def _cutter(data, model_path):
    return _cutout_png()


@pytest.fixture
def studio_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "UPLOADS", tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "photoroom_api_key", "")
    monkeypatch.setattr(settings, "fashn_api_key", "")
    monkeypatch.setattr(settings, "public_base_url", "https://agenc-ai.com/bob")
    return tmp_path


async def _shop(db, plan=TenantPlan.PRO, business_type="ONLINE_STORE", country="CI", is_demo=False):
    email = f"s{uuid.uuid4().hex[:8]}@x.ci"
    tenant = Tenant(name="MS4", country=country, currency="XOF", email=email, plan=plan, business_type=business_type,
                    is_demo=is_demo)
    db.add(tenant)
    await db.flush()
    db.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="A", role=Role.OWNER))
    await db.commit()
    return tenant, email


async def _product(db, tenant, folder, name="Robe wax", category="FASHION"):
    (folder / "orig.jpg").write_bytes(_photo_bytes())
    product = Product(tenant_id=tenant.id, sku=uuid.uuid4().hex[:6], name=name, price=1000, currency="XOF",
                      stock_quantity=3, bob_category=category,
                      image_url="https://agenc-ai.com/bob/uploads/products/orig.jpg")
    db.add(product)
    await db.commit()
    return product


# --- Règles ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, category, garment", [
    ("Robe wax", "FASHION", True), ("Costume 2 pièces", "FASHION", True), ("Chemise en lin", None, True),
    ("Escarpins vernis", "FASHION", False), ("Sac cabas cuir", "FASHION", False), ("Montre dorée", "FASHION", False),
    ("Casquette", "FASHION", False), ("Téléphone A56", "ELECTRONICS", False), ("Moto InfinX", None, False),
])
def test_only_garments_get_a_mannequin(name, category, garment):
    assert ps.is_garment(Product(name=name, bob_category=category)) is garment


def test_quotas_chosen_by_therese():
    assert ps.WHITE_QUOTA == {"FREE": 0, "INDEPENDANT": 50, "STARTER": 150, "PRO": 250, "ENTERPRISE": 500}
    assert ps.WORN_QUOTA == {"FREE": 0, "INDEPENDANT": 5, "STARTER": 20, "PRO": 100, "ENTERPRISE": 500}
    assert (ps.PHOTOROOM_COST, ps.FASHN_COST) == (0.02, 0.15)
    assert "Black African" in ps.worn_prompt("CI") and "Black African" not in ps.worn_prompt("FR")


def test_studio_finish_is_white_centered_and_signed():
    jpg = ps.studio(_cutout_png())
    image = Image.open(io.BytesIO(jpg)).convert("RGB")
    assert image.size == (1200, 1200)
    assert image.getpixel((5, 5)) == (255, 255, 255) and min(image.getpixel((600, 1190))) >= 240
    assert image.getpixel((600, 560))[0] > 150 and image.getpixel((600, 560))[1] < 80  # l'article, centré
    corner = image.crop((20, 1150, 200, 1185)).convert("L")
    assert min(corner.getdata()) < 200  # la signature « Édité par Bob AI »
    unsigned = Image.open(io.BytesIO(ps.studio(_cutout_png(), signature=False))).convert("L").crop((20, 1150, 200, 1185))
    assert min(unsigned.getdata()) > 220
    empty = io.BytesIO()
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(empty, "PNG")
    with pytest.raises(RuntimeError):
        ps.studio(empty.getvalue())  # rien de détouré : pas d'article


def test_montage_is_one_square_image_with_both_photos():
    image = Image.open(io.BytesIO(ps.montage(ps.studio(_cutout_png()), _photo_bytes((20, 60, 200), (300, 400))))).convert("RGB")
    assert image.size == (1200, 1200)
    left, right = image.getpixel((300, 560)), image.getpixel((900, 600))
    assert left[0] > 150 and right[2] > 150  # photo réelle à gauche, portée à droite


# --- Fond blanc studio ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_photoroom_within_quota_then_server_beyond(db_session, studio_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "photoroom_api_key", "cle-de-test")
    monkeypatch.setitem(ps.WHITE_QUOTA, "PRO", 1)
    tenant, _ = await _shop(db_session)
    first = await _product(db_session, tenant, studio_env)
    calls = []

    async def photoroom(data, key):
        calls.append(key)
        return _cutout_png()

    assert await ps.process_white(db_session, first, tenant, photoroom=photoroom, cutter=_cutter) == "DONE"
    assert calls == ["cle-de-test"]
    assert first.image_original_url == "https://agenc-ai.com/bob/uploads/products/orig.jpg"
    assert first.image_url == first.image_studio_url and first.image_url.endswith("-studio.jpg")
    assert (studio_env / first.image_url.rsplit("/", 1)[1]).is_file()
    second = await _product(db_session, tenant, studio_env, name="Robe bleue")
    assert await ps.process_white(db_session, second, tenant, photoroom=photoroom, cutter=_cutter) == "DONE"
    assert calls == ["cle-de-test"]  # quota du mois atteint : notre serveur
    jobs = (await db_session.execute(select(ImageJob).order_by(ImageJob.created_at))).scalars().all()
    assert [(j.engine, float(j.cost_usd), j.status) for j in jobs] == [("PHOTOROOM", 0.02, "DONE"), ("SERVER", 0.0, "DONE")]


@pytest.mark.asyncio
async def test_free_plan_and_missing_key_use_the_server_and_photoroom_failure_falls_back(db_session, studio_env, monkeypatch):
    free, _ = await _shop(db_session, plan=TenantPlan.FREE)
    product = await _product(db_session, free, studio_env)
    monkeypatch.setattr(get_settings(), "photoroom_api_key", "cle")

    async def boom(data, key):
        raise AssertionError("Photoroom ne doit pas être appelé en gratuit")

    assert await ps.process_white(db_session, product, free, photoroom=boom, cutter=_cutter) == "DONE"
    paid, _ = await _shop(db_session, plan=TenantPlan.STARTER)
    other = await _product(db_session, paid, studio_env)

    async def down(data, key):
        raise RuntimeError("503")

    assert await ps.process_white(db_session, other, paid, photoroom=down, cutter=_cutter) == "DONE"
    job = (await db_session.execute(select(ImageJob).where(ImageJob.product_id == other.id))).scalar_one()
    assert job.engine == "SERVER" and float(job.cost_usd) == 0


@pytest.mark.asyncio
async def test_a_failed_retouch_keeps_the_merchant_photo(db_session, studio_env):
    tenant, _ = await _shop(db_session)
    product = await _product(db_session, tenant, studio_env)

    async def broken(data, model):
        raise RuntimeError("modèle absent")

    assert await ps.process_white(db_session, product, tenant, cutter=broken) == "FAILED"
    assert product.image_url.endswith("/orig.jpg") and product.image_status == "FAILED"
    product.image_url = "http://non-securise/photo.jpg"
    product.image_original_url = None
    assert await ps.process_white(db_session, product, tenant, cutter=_cutter) == "FAILED"  # jamais en http
    with pytest.raises(ValueError, match="non sécurisée"):
        await ps.load_source("http://non-securise/photo.jpg")


@pytest.mark.asyncio
async def test_background_task_skips_demos_agencies_and_brokers(db_session, studio_env, monkeypatch):
    monkeypatch.setattr(ps, "server_cutout", _cutter)
    shop, _ = await _shop(db_session)
    agency, _ = await _shop(db_session, business_type="REAL_ESTATE")
    demo, _ = await _shop(db_session, is_demo=True)
    a = await _product(db_session, shop, studio_env)
    b = await _product(db_session, agency, studio_env, name="T3 lumineux", category="APPARTEMENT")
    c = await _product(db_session, demo, studio_env)
    report = await ps.run_pending(db_session, limit=3)
    for p in (a, b, c):
        await db_session.refresh(p)
    assert report["done"] == 1 and a.image_status == "DONE"
    assert b.image_status == "ORIGINAL" and b.image_url.endswith("/orig.jpg")
    assert c.image_status is None  # démo : jamais retouchée
    assert (await ps.run_pending(db_session))["done"] == 0  # rien à refaire


def test_new_source_resets_but_the_same_source_keeps_the_retouch():
    product = Product(name="x", image_url="https://b/studio.jpg", image_original_url="https://shop/a.jpg",
                      image_studio_url="https://b/studio.jpg", image_status="DONE", worn_status="ACCEPTED",
                      worn_image_url="https://b/w.jpg", card_style="WORN")
    ps.reset_for_new_source(product, "https://shop/a.jpg")  # synchro Shopify : même photo
    assert product.image_url == "https://b/studio.jpg" and product.image_status == "DONE"
    ps.reset_for_new_source(product, "https://shop/b.jpg")
    assert (product.image_url, product.image_original_url, product.image_studio_url, product.image_status) == (
        "https://shop/b.jpg", None, None, None)
    assert product.worn_image_url is None and product.card_style is None
    for path in ("app/services/shopify_sync.py", "app/services/meta_catalog_sync.py", "app/services/catalog_import.py",
                 "app/api/catalog/products.py"):
        source = (Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8")
        assert "reset_for_new_source(" in source, path


def test_original_can_always_come_back():
    product = Product(name="x", image_url="https://b/studio.jpg", image_original_url="https://b/orig.jpg",
                      image_status="DONE")
    ps.use_original(product)
    assert product.image_url == "https://b/orig.jpg" and product.image_status == "ORIGINAL"


# --- Photo portée ---------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worn_photo_rules(db_session, studio_env, monkeypatch):
    free, _ = await _shop(db_session, plan=TenantPlan.FREE)
    dress = await _product(db_session, free, studio_env)
    assert "Indépendant" in (await ps.request_worn(db_session, dress, free))["error"]
    shop, _ = await _shop(db_session, plan=TenantPlan.INDEPENDANT)
    shoes = await _product(db_session, shop, studio_env, name="Escarpins vernis")
    assert "habits" in (await ps.request_worn(db_session, shoes, shop))["error"]
    dress = await _product(db_session, shop, studio_env)
    assert "pas encore activée" in (await ps.request_worn(db_session, dress, shop))["error"]
    monkeypatch.setattr(get_settings(), "fashn_api_key", "cle-fashn")
    monkeypatch.setitem(ps.WORN_QUOTA, "INDEPENDANT", 1)
    sent = []

    async def runner(image, prompt, key):
        sent.append((len(image) > 0, prompt, key))
        return "pred-1"

    assert await ps.request_worn(db_session, dress, shop, runner=runner) == {"status": "PENDING"}
    assert sent == [(True, ps.worn_prompt("CI"), "cle-fashn")] and dress.worn_status == "PENDING"
    other = await _product(db_session, shop, studio_env, name="Chemise lin")
    assert "Plus de photo portée" in (await ps.request_worn(db_session, other, shop, runner=runner))["error"]


@pytest.mark.asyncio
async def test_worn_result_is_reviewed_then_shown_as_montage_or_alone(db_session, studio_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "fashn_api_key", "cle-fashn")
    shop, _ = await _shop(db_session)
    dress = await _product(db_session, shop, studio_env)
    await ps.process_white(db_session, dress, shop, cutter=_cutter)

    async def runner(image, prompt, key):
        return "pred-9"

    await ps.request_worn(db_session, dress, shop, runner=runner)
    worn = "data:image/jpeg;base64," + base64.b64encode(_photo_bytes((20, 60, 200), (300, 400))).decode()

    async def poller(prediction_id, key):
        assert prediction_id == "pred-9"
        return {"status": "completed", "output": [worn]}

    assert (await ps.collect_worn(db_session, poller=poller))["ready"] == 1
    await db_session.refresh(dress)
    assert dress.worn_status == "REVIEW" and dress.image_url == dress.image_studio_url  # rien n'est montré avant accord
    job = (await db_session.execute(select(ImageJob).where(ImageJob.kind == "WORN"))).scalar_one()
    assert float(job.cost_usd) == 0.15 and job.status == "DONE"

    await ps.decide_worn(db_session, dress, accept=True)
    assert dress.worn_status == "ACCEPTED" and dress.card_style == "MONTAGE" and dress.image_url.endswith("-montage.jpg")
    await ps.set_card_style(db_session, dress, "WORN")
    assert dress.image_url == dress.worn_image_url
    await ps.process_white(db_session, dress, shop, cutter=_cutter)  # nouvelle retouche : la carte reste la photo portée
    assert dress.image_url == dress.worn_image_url
    with pytest.raises(ValueError):
        await ps.set_card_style(db_session, dress, "AUTRE")
    with pytest.raises(ValueError):
        await ps.decide_worn(db_session, dress, accept=True)  # plus rien à valider


@pytest.mark.asyncio
async def test_failed_or_refused_worn_photo(db_session, studio_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "fashn_api_key", "cle")
    shop, _ = await _shop(db_session)
    dress = await _product(db_session, shop, studio_env)

    async def runner(image, prompt, key):
        return "p1"

    await ps.request_worn(db_session, dress, shop, runner=runner)

    async def failed(prediction_id, key):
        return {"status": "failed", "error": {"name": "ImageLoadError", "message": "x"}}

    assert (await ps.collect_worn(db_session, poller=failed))["failed"] == 1
    await db_session.refresh(dress)
    assert dress.worn_status == "FAILED" and dress.image_url.endswith("/orig.jpg")
    quota = await ps.quotas(db_session, shop)
    assert quota["worn_used"] == 0  # un échec ne compte pas


# --- API et tableau de bord -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api(client, db_session, studio_env):
    tenant, email = await _shop(db_session, plan=TenantPlan.FREE)
    dress = await _product(db_session, tenant, studio_env)
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    view = (await client.get(f"/api/v1/products/{dress.id}/photos", headers=headers)).json()
    assert view["retouched"] and view["garment"] and not view["worn_enabled"] and view["white_quota"] == 0
    worn = await client.post(f"/api/v1/products/{dress.id}/photos/worn", headers=headers)
    assert worn.status_code == 400 and "Indépendant" in worn.json()["detail"]
    original = (await client.post(f"/api/v1/products/{dress.id}/photos/original", headers=headers)).json()
    assert original["status"] == "ORIGINAL"
    retouch = (await client.post(f"/api/v1/products/{dress.id}/photos/retouch", headers=headers)).json()
    assert retouch["status"] == "PENDING"
    assert (await client.post(f"/api/v1/products/{dress.id}/photos/card-style", headers=headers,
                              json={"style": "WORN"})).status_code == 400
    agency, agency_email = await _shop(db_session, business_type="REAL_ESTATE")
    flat = await _product(db_session, agency, studio_env, name="T3", category="APPARTEMENT")
    token = (await client.post("/api/v1/auth/login", data={"username": agency_email, "password": "x"})).json()["access_token"]
    r = await client.post(f"/api/v1/products/{flat.id}/photos/retouch", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    other = await client.get(f"/api/v1/products/{flat.id}/photos", headers=headers)
    assert other.status_code == 404  # jamais le produit d'une autre boutique


@pytest.mark.asyncio
async def test_new_upload_goes_out_at_once_then_gets_retouched(client, db_session, studio_env, monkeypatch):
    tenant, email = await _shop(db_session)
    dress = await _product(db_session, tenant, studio_env)
    dress.image_studio_url, dress.image_status = "https://agenc-ai.com/bob/uploads/products/old-studio.jpg", "DONE"
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    r = await client.post(f"/api/v1/products/{dress.id}/image", headers={"Authorization": f"Bearer {token}"},
                          files={"file": ("photo.jpg", _photo_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    await db_session.refresh(dress)
    assert dress.image_status == "PENDING" and dress.image_studio_url is None and "/uploads/products/" in dress.image_url


def test_dashboard_photo_block_and_schedule():
    for text in ("function loadPhotoStudio(productId)", "Revenir à ma photo", "Créer la photo portée", "Accepter",
                 "Photo réelle et photo portée côte à côte", "Photo portée seule", "loadPhotoStudio(p.id);",
                 "disponible à partir de la formule Indépendant"):
        assert text in DASH, text
    from app.services.monitoring import TASK_LABELS
    from app.workers.celery_app import celery_app

    for key in ("photo-retouch", "photo-worn"):
        assert celery_app.conf.beat_schedule[key]["task"] in TASK_LABELS
    docker = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(encoding="utf-8")
    assert "fonts-dejavu-core" in docker and "isnet-general-use.onnx" in docker and "sha256sum -c" in docker
