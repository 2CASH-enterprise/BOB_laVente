"""
Lot 81 — ventes réelles DVF (Demandes de valeurs foncières, publiées par l'État ; licence ouverte), pour le prix de
comparaison de la pige.

- Chargées seulement pour les départements où les agences ont des annonces à vendre (un fichier par année : les
  3 dernières années pleines et l'année en cours si elle est publiée). Aucun coût : données publiques, gratuites.
- On ne garde que les ventes simples d'UN logement (appartement ou maison) : une vente de plusieurs logements, ou sans
  surface, fausserait le prix au m². Les ventes sont publiées avec plusieurs mois de retard : Bob le dit toujours.
- Le prix de comparaison est une MÉDIANE au m² de ventes comparables (même code postal, même type, surface de ±30 %) :
  une aide pour l'appel de l'agent, jamais une estimation donnée par Bob à un client.
"""
import csv
import gzip
import io
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import delete, select

from app.models.pige import DvfDepartment, DvfSale

logger = logging.getLogger(__name__)

TYPES = {"Appartement": "APPARTEMENT", "Maison": "MAISON"}
YEARS_BACK = 3
MIN_SALES = 5
SURFACE_MARGIN = 0.30
REFRESH_AFTER = timedelta(days=180)
SOURCE_NOTE = "Ventes réelles publiées par l'État (DVF), avec plusieurs mois de retard."


def department_of(postal_code: str | None) -> str | None:
    """« 69003 » → « 69 » ; Corse « 20000 » → « 2A », « 20200 » → « 2B » ; outre-mer « 97400 » → « 974 »."""
    code = (postal_code or "").strip()
    if len(code) != 5 or not code.isdigit():
        return None
    if code.startswith("97"):
        return code[:3]
    if code.startswith("20"):
        return "2A" if int(code) < 20200 else "2B"
    return code[:2]


def url_for(year: int, department: str) -> str:
    from app.core.config import get_settings

    return f"{get_settings().dvf_base_url.rstrip('/')}/{year}/departements/{department}.csv.gz"


def _number(value) -> float | None:
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse(raw: bytes, department: str) -> list[dict]:
    """Ventes simples d'un logement, d'un fichier geo-dvf (compressé ou non)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    mutations: dict[str, list[dict]] = {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))):
        if (row.get("nature_mutation") or "") != "Vente":
            continue
        mutations.setdefault(row.get("id_mutation") or "", []).append(row)
    sales = []
    for rows in mutations.values():
        homes = [r for r in rows if r.get("type_local") in TYPES]
        if len(homes) != 1:
            continue  # aucun logement, ou plusieurs : prix au m² impossible
        row = homes[0]
        price, surface = _number(row.get("valeur_fonciere")), _number(row.get("surface_reelle_bati"))
        try:
            sold_on = date.fromisoformat((row.get("date_mutation") or "")[:10])
        except ValueError:
            continue
        if price is None or surface is None or surface < 9:
            continue
        rooms = _number(row.get("nombre_pieces_principales"))
        sales.append({"department": department, "postal_code": (row.get("code_postal") or "")[:5] or None,
                      "commune": (row.get("nom_commune") or "")[:100] or None, "sold_on": sold_on,
                      "property_type": TYPES[row["type_local"]], "surface": int(surface),
                      "rooms": int(rooms) if rooms else None, "price": Decimal(str(round(price, 2)))})
    return sales


async def _download(url: str) -> bytes | None:
    import httpx

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.content


async def load_department(db, department: str, now: datetime | None = None, fetch=None) -> int:
    """Remplace les ventes du département par celles des fichiers publiés ; renvoie le nombre de ventes gardées."""
    now = now or datetime.now(timezone.utc)
    fetch = fetch or _download
    row = await db.get(DvfDepartment, department) or DvfDepartment(code=department)
    db.add(row)
    sales, years = [], []
    since = (now - timedelta(days=365 * YEARS_BACK)).date()
    try:
        for year in range(now.year, now.year - YEARS_BACK - 1, -1):
            raw = await fetch(url_for(year, department))
            if raw:
                years.append(str(year))
                sales += [s for s in parse(raw, department) if s["sold_on"] >= since]
    except Exception as exc:  # noqa: BLE001 — réseau, fichier illisible : on garde les anciennes ventes
        row.error = f"Chargement impossible : {type(exc).__name__}"[:300]
        await db.commit()
        logger.warning("Ventes DVF non chargées (département %s)", department)
        return 0
    await db.execute(delete(DvfSale).where(DvfSale.department == department))
    db.add_all(DvfSale(**s) for s in sales)
    row.loaded_at, row.sales, row.years, row.error = now, len(sales), ", ".join(sorted(years)) or None, None
    await db.commit()
    return len(sales)


async def needs_loading(db, department: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    row = await db.get(DvfDepartment, department)
    if row is None or row.loaded_at is None:
        return True
    loaded = row.loaded_at if row.loaded_at.tzinfo else row.loaded_at.replace(tzinfo=timezone.utc)
    return now - loaded > REFRESH_AFTER


async def compare(db, property_type: str | None, postal_code: str | None, surface, price) -> dict | None:
    """Prix de comparaison d'un bien à vendre ; None si la donnée manque, un message s'il y a trop peu de ventes."""
    department = department_of(postal_code)
    if property_type not in TYPES.values() or not department or not surface or not price:
        return None
    surface, price = float(surface), float(price)
    loaded = await db.get(DvfDepartment, department)
    if loaded is None or loaded.loaded_at is None:
        return {"status": "PENDING", "message": f"Ventes du département {department} en cours de chargement (sous 24 h)."}
    rows = (await db.execute(select(DvfSale.price, DvfSale.surface, DvfSale.sold_on).where(
        DvfSale.department == department, DvfSale.postal_code == postal_code,
        DvfSale.property_type == property_type))).all()
    close = [r for r in rows if abs(r.surface - surface) <= surface * SURFACE_MARGIN]
    pool, scope = (close, "même type, surface proche") if len(close) >= MIN_SALES else (rows, "même type")
    if len(pool) < MIN_SALES:
        return {"status": "TOO_FEW", "message": f"Pas assez de ventes comparables au code postal {postal_code}."}
    per_m2 = sorted(float(r.price) / r.surface for r in pool)
    quarter = max(1, len(per_m2) // 4)
    median_m2 = median(per_m2)
    listing_m2 = price / surface
    first, last = min(r.sold_on for r in pool), max(r.sold_on for r in pool)
    return {"status": "OK", "median_m2": round(median_m2), "low_m2": round(per_m2[quarter - 1]),
            "high_m2": round(per_m2[-quarter]), "count": len(pool), "scope": scope, "listing_m2": round(listing_m2),
            "delta_pct": round((listing_m2 - median_m2) / median_m2 * 100), "postal_code": postal_code,
            "period": f"ventes de {first.strftime('%m/%Y')} à {last.strftime('%m/%Y')}", "note": SOURCE_NOTE}
