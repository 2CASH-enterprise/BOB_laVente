from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import CurrentUser, get_current_user, require_role
from app.repositories.product_repository import ProductRepository
from app.schemas.catalog import ProductCreate, ProductResponse, ProductUpdate
from app.models.product import Product

router = APIRouter(prefix="/api/v1/products", tags=["products"])


def _checked_vehicle(raw: dict | None, tenant=None) -> dict | None:
    """Lot 26 — caractéristiques normalisées ; valeur douteuse refusée plutôt qu'enregistrée."""
    from app.services.business_type import NOT_AVAILABLE, is_dealership
    from app.services.vehicle import normalize_vehicle

    if raw and tenant is not None and not is_dealership(tenant):
        raise HTTPException(status_code=403, detail=NOT_AVAILABLE)  # lot 32 : fiche véhicule = concession

    vehicle, errors = normalize_vehicle(raw)
    if errors:
        raise HTTPException(status_code=422, detail=" ; ".join(errors))
    return vehicle


def _checked_sheet(tenant, code: str | None, raw: dict | None, *texts) -> dict:
    """Lot 78 — commerce : catégorie Bob valide (proposée par mots-clés si absente) et fiche nettoyée.
    Lot 79 — agence immobilière : la catégorie est le type de bien (proposé d'après le nom si absent)."""
    from app.services import product_sheets
    from app.services.business_type import is_real_estate, is_store

    if is_real_estate(tenant):
        if code and not product_sheets.is_property(code):
            raise HTTPException(status_code=422, detail="Type de bien inconnu")
        code = code or product_sheets.guess_property_type(*texts)
        return {"bob_category": code, "sheet": product_sheets.normalize_sheet(code, raw)}
    if not is_store(tenant):
        return {"bob_category": None, "sheet": None}
    if code and not product_sheets.is_category(code):
        raise HTTPException(status_code=422, detail="Catégorie inconnue")
    if not code:
        default = getattr(tenant, "default_bob_category", None)
        code = default if product_sheets.is_category(default) else product_sheets.guess_category(*texts)
    return {"bob_category": code, "sheet": product_sheets.normalize_sheet(code, raw)}


@router.post("", response_model=ProductResponse, status_code=201, dependencies=[Depends(require_role("MANAGER"))])
async def create_product(
    payload: ProductCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.tenant import Tenant
    from app.services.plan_limits import FREEMIUM_MAX_PRODUCTS, remaining_product_slots

    tenant = await db.get(Tenant, current_user.tenant_id)
    remaining = await remaining_product_slots(db, current_user.tenant_id, tenant.is_paid)
    if remaining is not None and remaining <= 0:
        raise HTTPException(
            status_code=403,
            detail=f"Plan freemium limité à {FREEMIUM_MAX_PRODUCTS} produits. Passez à un plan payant pour en ajouter davantage.",
        )

    repo = ProductRepository(db)
    existing = await repo.get_by_sku(current_user.tenant_id, payload.sku)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Un produit avec la référence « {payload.sku} » existe déjà")

    data = payload.model_dump()
    data["vehicle"] = _checked_vehicle(data.get("vehicle"), tenant)
    data.update(_checked_sheet(tenant, data.pop("bob_category", None), data.pop("sheet", None),
                               data.get("name"), data.get("description")))
    from app.services.business_type import is_insurance

    if is_insurance(tenant):
        # Lot 53 — un produit d'assurance n'a ni prix ni stock : le cabinet fait une proposition personnalisée.
        data.update(price=0, cost_price=None, stock_quantity=0)
    elif data.get("price") is None:
        raise HTTPException(status_code=422, detail="Le prix est obligatoire")
    product = Product(tenant_id=current_user.tenant_id, **data)
    await repo.add(product)
    await db.commit()
    await db.refresh(product)
    return product


@router.get("", response_model=list[ProductResponse])
async def search_products(
    query: str | None = None,
    category_id: UUID | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Section 14 — recherche produit. Cette route est la base directe du futur outil IA
    `search_products` : mêmes paramètres, même garantie d'isolation par tenant (section 30).
    """
    repo = ProductRepository(db)
    return await repo.search(
        tenant_id=current_user.tenant_id,
        query=query,
        category_id=category_id,
        min_price=float(min_price) if min_price is not None else None,
        max_price=float(max_price) if max_price is not None else None,
    )


@router.get("/sheets")
async def product_sheet_definitions(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lot 78 — catégories Bob et champs de leurs fiches (formulaire du tableau de bord). Avant /{product_id}."""
    from app.models.tenant import Tenant
    from app.services import product_sheets

    from app.services.business_type import is_real_estate

    tenant = await db.get(Tenant, current_user.tenant_id)
    estate = is_real_estate(tenant)  # lot 79 : types de bien et fiche du bien
    return {"categories": product_sheets.definitions(estate), "common_fields": product_sheets.common_definitions(estate),
            "default_category": None if estate else getattr(tenant, "default_bob_category", None),
            "real_estate": estate}


@router.post("/sheets/guess", dependencies=[Depends(require_role("MANAGER"))])
async def guess_missing_categories(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lot 78 — propose une catégorie Bob (mots-clés, sans IA) aux produits qui n'en ont pas encore."""
    from sqlalchemy import select

    from app.models.tenant import Tenant
    from app.services import product_sheets
    from app.services.business_type import is_store

    from app.services.business_type import is_real_estate

    tenant = await db.get(Tenant, current_user.tenant_id)
    if is_real_estate(tenant):  # lot 79 : type de bien proposé d'après le nom et la description
        products = (await db.execute(select(Product).where(Product.tenant_id == current_user.tenant_id,
                                                           Product.bob_category.is_(None)))).scalars().all()
        for product in products:
            product.bob_category = product_sheets.guess_property_type(product.name, product.description)
        await db.commit()
        return {"updated": len(products)}
    if not is_store(tenant):
        return {"updated": 0}
    products = (await db.execute(select(Product).where(Product.tenant_id == current_user.tenant_id,
                                                       Product.bob_category.is_(None)))).scalars().all()
    default = getattr(tenant, "default_bob_category", None)
    for product in products:
        guess = product_sheets.guess_category(product.name, product.description)
        product.bob_category = default if guess == product_sheets.GENERAL and product_sheets.is_category(default) else guess
    await db.commit()
    return {"updated": len(products)}


@router.get("/template/xlsx")
async def product_template_xlsx(
    category: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Lot 78 B — modèle Excel vide du secteur (commerce : colonnes de la catégorie choisie). DOIT rester déclaré
    avant /{product_id}.
    """
    from fastapi import Response

    from app.models.tenant import Tenant
    from app.services import product_sheets, spreadsheet
    from app.services.catalog_import import file_columns, template_help

    tenant = await db.get(Tenant, current_user.tenant_id)
    if category and not product_sheets.is_category(category):
        raise HTTPException(status_code=400, detail="Catégorie inconnue")
    codes = [category] if category else None
    header = [h for h, _, _ in file_columns(tenant, codes)]
    raw = spreadsheet.to_xlsx("Catalogue", header, [])
    raw = spreadsheet.add_help_sheet(raw, template_help(tenant))
    suffix = f"-{category.lower()}" if category else ""
    return Response(raw, media_type=spreadsheet.XLSX_MEDIA,
                    headers={"Content-Disposition": f"attachment; filename=modele-catalogue-bob{suffix}.xlsx"})


@router.get("/export/xlsx")
async def export_products_xlsx(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Export du catalogue en Excel — DOIT rester déclaré avant /{product_id} : sinon
    FastAPI tente de parser "export" comme un UUID et échoue avant même d'essayer cette route.
    Lot 78 B : mêmes colonnes que le modèle du secteur, le fichier se réimporte tel quel (corrections en masse).
    """
    from fastapi import Response

    from app.models.tenant import Tenant
    from app.services import spreadsheet
    from app.services.catalog_import import export_cell, file_columns

    tenant = await db.get(Tenant, current_user.tenant_id)
    repo = ProductRepository(db)
    products = await repo.search(tenant_id=current_user.tenant_id, query=None, active_only=False, limit=10000)
    codes = sorted({p.bob_category for p in products if p.bob_category}) or None
    columns = file_columns(tenant, codes)
    rows = [[export_cell(p, kind, key) for _, kind, key in columns] for p in products]
    raw = spreadsheet.to_xlsx("Catalogue", [h for h, _, _ in columns], rows)
    return Response(raw, media_type=spreadsheet.XLSX_MEDIA,
                    headers={"Content-Disposition": "attachment; filename=catalogue-bob.xlsx"})


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    return product


@router.put("/{product_id}", response_model=ProductResponse, dependencies=[Depends(require_role("MANAGER"))])
async def update_product(
    product_id: UUID,
    payload: ProductUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")

    changes = payload.model_dump(exclude_unset=True)
    from app.models.tenant import Tenant
    from app.services.business_type import is_insurance

    tenant = await db.get(Tenant, current_user.tenant_id)
    if "vehicle" in changes:
        changes["vehicle"] = _checked_vehicle(changes["vehicle"], tenant)
    if "bob_category" in changes or "sheet" in changes:
        # Lot 78 — changer de catégorie garde les champs communs et ceux que la nouvelle catégorie connaît.
        code = changes.get("bob_category") or product.bob_category
        changes.update(_checked_sheet(tenant, code, changes.get("sheet", product.sheet), product.name))
    if is_insurance(tenant):
        for key in ("price", "cost_price", "stock_quantity"):  # lot 53 : jamais de prix chez un courtier
            changes.pop(key, None)
    if "image_url" in changes:  # lot 87 : nouvelle photo → retouche refaite (même photo : retouche gardée)
        from app.services.photo_studio import reset_for_new_source

        reset_for_new_source(product, changes.pop("image_url"))
    for field, value in changes.items():
        setattr(product, field, value)

    await db.commit()
    await db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=204, dependencies=[Depends(require_role("MANAGER"))])
async def delete_product(
    product_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Suppression DOUCE (active=False), jamais une vraie suppression en base : un produit
    déjà vendu est référencé par des commandes existantes (contrainte de clé étrangère),
    et l'historique des ventes ne doit jamais pouvoir se casser.
    """
    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    product.active = False
    await db.commit()


@router.post("/{product_id}/image", response_model=ProductResponse, dependencies=[Depends(require_role("MANAGER"))])
async def upload_product_image(
    product_id: UUID,
    file: UploadFile,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stocke le fichier sur disque (monté en volume, persiste aux redéploiements) et met à jour image_url."""
    import uuid as uuid_module
    from pathlib import Path

    from app.core.config import get_settings

    repo = ProductRepository(db)
    product = await repo.get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")

    allowed_ext = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if ext not in allowed_ext:
        raise HTTPException(status_code=400, detail="Format d'image non supporté (jpg, png, webp uniquement)")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image trop volumineuse (max 5 Mo)")

    upload_dir = Path(__file__).resolve().parents[2] / "static" / "uploads" / "products"
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid_module.uuid4().hex}.{ext}"
    (upload_dir / filename).write_bytes(content)

    settings = get_settings()
    from app.services.photo_studio import reset_for_new_source

    # Lot 87 : la nouvelle photo part telle quelle tout de suite, puis passe au fond blanc studio (en arrière-plan).
    reset_for_new_source(product, f"{settings.public_base_url}/uploads/products/{filename}")
    product.image_status = "PENDING"

    await db.commit()
    await db.refresh(product)
    return product


# --- Lot 87 : Bob retouche les photos (fond blanc studio, photo portée des habits, montage) -------------------------

from pydantic import BaseModel  # noqa: E402


class WornDecision(BaseModel):
    accept: bool


class CardStyle(BaseModel):
    style: str


async def _photo_context(db, current_user, product_id):
    from app.models.tenant import Tenant

    product = await ProductRepository(db).get(tenant_id=current_user.tenant_id, record_id=product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    return product, await db.get(Tenant, current_user.tenant_id)


async def _photo_view(db, product, tenant) -> dict:
    from app.services import photo_studio

    return photo_studio.view(product, tenant, await photo_studio.quotas(db, tenant))


@router.get("/{product_id}/photos")
async def product_photos(product_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    product, tenant = await _photo_context(db, current_user, product_id)
    return await _photo_view(db, product, tenant)


@router.post("/{product_id}/photos/original", dependencies=[Depends(require_role("MANAGER"))])
async def product_photo_original(product_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import photo_studio

    product, tenant = await _photo_context(db, current_user, product_id)
    photo_studio.use_original(product)
    await db.commit()
    return await _photo_view(db, product, tenant)


@router.post("/{product_id}/photos/retouch", dependencies=[Depends(require_role("MANAGER"))])
async def product_photo_retouch(product_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import photo_studio

    product, tenant = await _photo_context(db, current_user, product_id)
    if not photo_studio.retouches(tenant):
        raise HTTPException(status_code=400, detail="Les photos de ce type de compte ne sont pas retouchées.")
    if not (product.image_original_url or product.image_url):
        raise HTTPException(status_code=400, detail="Ajoutez d'abord une photo du produit.")
    product.image_status = "PENDING"
    await db.commit()
    return await _photo_view(db, product, tenant)


@router.post("/{product_id}/photos/worn", dependencies=[Depends(require_role("MANAGER"))])
async def product_photo_worn(product_id: UUID, current_user: CurrentUser = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import photo_studio

    product, tenant = await _photo_context(db, current_user, product_id)
    result = await photo_studio.request_worn(db, product, tenant)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return await _photo_view(db, product, tenant)


@router.post("/{product_id}/photos/worn/decision", dependencies=[Depends(require_role("MANAGER"))])
async def product_photo_worn_decision(product_id: UUID, payload: WornDecision,
                                      current_user: CurrentUser = Depends(get_current_user),
                                      db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import photo_studio

    product, tenant = await _photo_context(db, current_user, product_id)
    try:
        await photo_studio.decide_worn(db, product, payload.accept)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return await _photo_view(db, product, tenant)


@router.post("/{product_id}/photos/card-style", dependencies=[Depends(require_role("MANAGER"))])
async def product_photo_card_style(product_id: UUID, payload: CardStyle,
                                   current_user: CurrentUser = Depends(get_current_user),
                                   db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import photo_studio

    product, tenant = await _photo_context(db, current_user, product_id)
    if not product.worn_image_url:
        raise HTTPException(status_code=400, detail="Acceptez d'abord une photo portée.")
    try:
        await photo_studio.set_card_style(db, product, payload.style)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return await _photo_view(db, product, tenant)
