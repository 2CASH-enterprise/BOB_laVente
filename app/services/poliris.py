"""
Lot 105 a — les annonces du logiciel de l'agence, au format des portails (validé le 03/11).

Les logiciels des agences françaises (La Boîte Immo, Hektor, Apimo, AC3, Netty…) diffusent leurs annonces vers les
portails dans le format SeLoger « Poliris » (spécification v4.09 du 31/05/2021) : un ZIP qui contient Annonces.csv (une
annonce par ligne, champs entre guillemets séparés par « !# », numérotés), Config.txt, Photos.cfg et, selon le mode,
les photos. L'agence dépose ce ZIP dans Bob (carte « Votre logiciel immobilier », à la place de Shopify) : Bob le lit,
montre le tableau à vérifier (même écran que l'import Excel), puis crée ou met à jour les biens. Mode « annule et
remplace » de la spécification : un bien déjà reçu par cette voie et absent du nouveau fichier passe en Vendu ou Loué
(jamais supprimé). Un bien n'est montré avec un prix qu'avec ses honoraires (loi ALUR) ; les photos deviennent des
JPEG (WhatsApp). Aucune IA, aucun coût.
"""
import io
import re
import uuid
import zipfile

SEPARATOR = "!#"
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_LISTINGS = 2000
MAX_PHOTO_BYTES = 8 * 1024 * 1024

# Numéros des champs (spécification v4.09), comptés à partir de 1.
F = {"agency": 1, "ref": 2, "kind": 3, "type": 4, "cp": 5, "city": 6, "district": 9, "price": 11, "rent_cc": 13,
     "fees": 15, "surface": 16, "land": 17, "rooms": 18, "bedrooms": 19, "title": 20, "text": 21, "available": 22,
     "charges": 23, "floor": 24, "furnished": 26, "photo1": 85, "mandate": 112, "deposit": 161, "tech_id": 175,
     "dpe": 177, "ges": 179, "real_cp": 108, "real_city": 109, "fees_payer": 302, "price_without_fees": 303,
     "charges_mode": 304, "inventory_fees": 306, "energy_min": 326, "energy_max": 327}
PHOTO_FIELDS = [85, 86, 87, 88, 89, 90, 91, 92, 93, *range(164, 175), *range(264, 274)]

SALE_KINDS = ("vente", "vente de prestige", "viager", "produit d'investissement", "produit d investissement")
RENT_KINDS = ("location",)
TYPES = (  # type de bien de la spécification → type de bien de Bob
    ("appartement", "Appartement"), ("maison", "Maison"), ("villa", "Maison"), ("chateau", "Maison"),
    ("hotel particulier", "Maison"), ("terrain", "Terrain"), ("parking", "Parking"), ("box", "Parking"),
    ("boutique", "Local"), ("bureau", "Local"), ("local", "Local"), ("loft", "Local"), ("atelier", "Local"),
    ("batiment", "Local"), ("immeuble", "Local"), ("entreprise", "Local"))
FEES_PAYER = {"1": "à la charge de l'acquéreur", "2": "à la charge du vendeur", "3": "à la charge de l'acquéreur et du vendeur"}
CHARGES_MODE = {"1": "forfaitaires", "2": "provisions avec régularisation annuelle", "3": "remboursement annuel par le locataire"}
DPE_CLASSES = set("ABCDEFG")

HEADER = ["SKU", "NAME", "DESCRIPTION", "PRICE", "CURRENCY", "STOCK", "IMAGE_URL", "ACTIVE", "TYPE", "TRANSACTION",
          "VILLE", "QUARTIER", "SURFACE", "TERRAIN", "PIECES", "CHAMBRES", "DPE", "GES", "HONORAIRES", "CHARGES",
          "DEPOT DE GARANTIE", "ETAGE", "MEUBLE", "REFERENCE MANDAT", "DISPONIBILITE"]


class PolirisError(ValueError):
    """Fichier refusé : message pour l'agence."""


def _plain(text) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(text.lower().replace("’", "'").split())


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_line(line: str) -> list[str]:
    """« "a"!#"b"!#"" » → ["a", "b", ""] (guillemets retirés, <BR> → retour à la ligne)."""
    out = []
    for cell in line.split(SEPARATOR):
        cell = cell.strip()
        if len(cell) >= 2 and cell[0] == '"' and cell[-1] == '"':
            cell = cell[1:-1]
        elif cell in ('"', '""'):
            cell = ""
        out.append(re.sub(r"<br\s*/?>", "\n", cell, flags=re.IGNORECASE).strip())
    return out


def parse_annonces(text: str) -> list[list[str]]:
    lines = [line for line in re.split(r"\r\n|\n|\r", text) if line.strip()]
    return [parse_line(line) for line in lines]


def _get(cells: list[str], key) -> str:
    number = F[key] if isinstance(key, str) else key
    return cells[number - 1].strip() if number - 1 < len(cells) else ""


def _number(text: str) -> float | None:
    text = (text or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value


def _figure(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _euros(value: float | None) -> str:
    if value is None:
        return ""
    whole = f"{value:,.0f}".replace(",", " ") if float(value).is_integer() else f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{whole} €"


def _type_of(text: str) -> str:
    plain = _plain(text)
    return next((label for word, label in TYPES if word in plain), "Autre")


def _transaction_of(text: str) -> str | None:
    plain = _plain(text)
    if plain in SALE_KINDS or plain.startswith("vente") and "fonds" not in plain:
        return "Vente"
    if plain in RENT_KINDS:
        return "Location"
    return None  # location vacances, cession de bail, fonds de commerce, produit catalogue : pas pour Bob


def fees_text(cells: list[str], sale: bool) -> str:
    """Honoraires lisibles, tels que la loi ALUR les demande (vente : %, qui paie ; location : montant, état des lieux)."""
    fees = _number(_get(cells, "fees"))
    if sale:
        payer = FEES_PAYER.get(_get(cells, "fees_payer"), "")
        if payer == FEES_PAYER["2"]:
            return "à la charge du vendeur"
        if fees is None:
            return ""
        text = f"{_figure(fees)} % TTC {payer or FEES_PAYER['1']}"
        without = _number(_get(cells, "price_without_fees"))
        return text + (f" (prix hors honoraires : {_euros(without)})" if without else "")
    if fees is None:
        return ""
    text = f"{_euros(fees)} TTC à la charge du locataire"
    inventory = _number(_get(cells, "inventory_fees"))
    return text + (f", dont {_euros(inventory)} d'état des lieux" if inventory else "")


def _charges_text(cells: list[str], sale: bool) -> str:
    amount = _number(_get(cells, "charges"))
    if amount is None or amount <= 0:
        return ""
    if sale:
        return _euros(amount)
    mode = CHARGES_MODE.get(_get(cells, "charges_mode"))
    return f"{_euros(amount)} par mois" + (f" ({mode})" if mode else "")


def _class(value: str) -> str:
    value = (value or "").strip().upper()
    return value if value in DPE_CLASSES else ""


def _description(cells: list[str]) -> str:
    text = _get(cells, "text")
    low, high = _number(_get(cells, "energy_min")), _number(_get(cells, "energy_max"))
    if low and high:
        text += f"\nDépenses d'énergie estimées : entre {_euros(low)} et {_euros(high)} par an."
    return text.strip()


def photo_names(cells: list[str]) -> list[str]:
    return [_get(cells, n) for n in PHOTO_FIELDS if _get(cells, n)]


def to_rows(listings: list[list[str]], photo_url_of) -> tuple[list[list[str]], list[str]]:
    """Annonces → lignes du tableau de Bob (HEADER) ; et les annonces écartées, avec la raison."""
    rows, skipped, seen = [], [], set()
    for cells in listings[:MAX_LISTINGS]:
        title = _get(cells, "title") or _get(cells, "ref")
        transaction = _transaction_of(_get(cells, "kind"))
        if transaction is None:
            skipped.append(f"{title or 'Annonce'} : « {_get(cells, 'kind') or 'type inconnu'} » n'est pas une vente ni une location")
            continue
        sku = (_get(cells, "ref") or _get(cells, "tech_id"))[:64]
        if not sku or sku in seen:
            skipped.append(f"{title or 'Annonce'} : référence {'absente' if not sku else 'en double'}")
            continue
        seen.add(sku)
        sale = transaction == "Vente"
        price = _number(_get(cells, "price"))
        image = next((url for url in (photo_url_of(name, sku) for name in photo_names(cells)) if url), "")
        floor = _get(cells, "floor")
        rows.append([
            sku, title[:255], _description(cells), _figure(price), "EUR", "1", image, "oui",
            _type_of(_get(cells, "type")), transaction,
            _get(cells, "real_city") or _get(cells, "city"), _get(cells, "district"),
            _figure(_number(_get(cells, "surface"))), _figure(_number(_get(cells, "land"))),
            _figure(_number(_get(cells, "rooms"))), _figure(_number(_get(cells, "bedrooms"))),
            _class(_get(cells, "dpe")), _class(_get(cells, "ges")), fees_text(cells, sale), _charges_text(cells, sale),
            "" if sale else _euros(_number(_get(cells, "deposit"))),
            ("RDC" if floor == "0" else floor), "Meublé" if _plain(_get(cells, "furnished")) == "oui" else "",
            _get(cells, "mandate"), _get(cells, "available"),
        ])
    return rows, skipped


def read_zip(raw: bytes, save_photo, existing_photo=None) -> dict:
    """Le ZIP de la passerelle → {header, rows, skipped, mode, count}.
    save_photo(nom, contenu) enregistre une photo jointe et renvoie son adresse ; existing_photo(sku) renvoie la photo
    déjà connue d'un bien (mode « DIFF » : seules les photos nouvelles sont jointes)."""
    if len(raw) > MAX_ZIP_BYTES:
        raise PolirisError("Fichier trop volumineux (50 Mo au plus) : choisissez l'envoi des photos par adresse web.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise PolirisError("Ce fichier n'est pas un ZIP. Déposez l'export « format SeLoger (Poliris) » de votre logiciel.") from None
    names = {info.filename.rsplit("/", 1)[-1].lower(): info for info in archive.infolist() if not info.is_dir()}
    if "annonces.csv" not in names:
        raise PolirisError("Le ZIP ne contient pas « Annonces.csv » : ce n'est pas un export au format SeLoger (Poliris).")
    listings = parse_annonces(_decode(archive.read(names["annonces.csv"])))
    if not listings:
        raise PolirisError("« Annonces.csv » est vide.")
    if max(len(cells) for cells in listings) < 21:
        raise PolirisError("« Annonces.csv » n'a pas les colonnes du format SeLoger (séparateur « !# »).")
    mode = "URL"
    if "photos.cfg" in names:
        match = re.search(r"mode\s*=\s*(\w+)", _decode(archive.read(names["photos.cfg"])), re.IGNORECASE)
        mode = (match.group(1).upper() if match else "URL")
    saved: dict[str, str] = {}

    def photo_url_of(name: str, sku: str) -> str:
        if re.match(r"^https?://", name, re.IGNORECASE):
            return name if name.lower().startswith("https://") else ""
        key = name.rsplit("/", 1)[-1].lower()
        if key in saved:
            return saved[key]
        info = names.get(key)
        if info is not None and 0 < info.file_size <= MAX_PHOTO_BYTES:
            try:
                saved[key] = save_photo(key, archive.read(info)) or ""
            except Exception:  # noqa: BLE001 — photo illisible : le bien arrive sans elle
                saved[key] = ""
            return saved[key]
        return (existing_photo(sku) if existing_photo else None) or ""  # mode DIFF : photo déjà reçue

    rows, skipped = to_rows(listings, photo_url_of)
    if len(listings) > MAX_LISTINGS:
        skipped.append(f"{len(listings) - MAX_LISTINGS} annonce(s) au-delà de {MAX_LISTINGS} : non lues")
    return {"header": list(HEADER), "rows": rows, "skipped": skipped, "mode": mode, "count": len(listings)}


def photo_saver(uploads, base_url: str):
    """Enregistre une photo jointe au ZIP en JPEG (ou PNG) dans les photos des produits ; renvoie son adresse."""
    from app.services.photo_formats import sendable

    def save(name: str, content: bytes) -> str:
        ext = name.rsplit(".", 1)[-1] if "." in name else "jpg"
        content, ext = sendable(content, ext)
        uploads.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.{ext}"
        (uploads / filename).write_bytes(content)
        return f"{base_url.rstrip('/')}/uploads/products/{filename}"

    return save


async def retire_missing(db, tenant_id, skus: set[str]) -> int:
    """Annule et remplace : les biens reçus par la passerelle sont marqués ; ceux qui n'y sont plus passent en Vendu ou
    Loué (jamais supprimés). Renvoie le nombre de biens retirés."""
    from sqlalchemy import select

    from app.models.product import Product
    from app.services.product_sheets import _plain as plain

    products = (await db.execute(select(Product).where(Product.tenant_id == tenant_id))).scalars().all()
    retired = 0
    for product in products:
        if product.sku in skus:
            if product.external_source in (None, "POLIRIS"):
                product.external_source, product.external_id = "POLIRIS", product.sku
            continue
        if product.external_source != "POLIRIS" or (product.stock_quantity or 0) <= 0:
            continue
        sheet = dict(product.sheet or {})
        sheet["status"] = "Loué" if plain(sheet.get("transaction")) == plain("Location") else "Vendu"
        product.sheet, product.stock_quantity = sheet, 0
        retired += 1
    await db.commit()
    return retired
