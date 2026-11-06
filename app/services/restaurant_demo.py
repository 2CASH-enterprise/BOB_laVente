"""
Lot 109 — démo instantanée d'un restaurant : une carte d'exemple prête (le prospect n'a aucun fichier à préparer).

Plats d'Abidjan avec leur fiche (composition, accompagnements au choix, piment, allergènes), cuisine ouverte de 11 h à
23 h, à emporter, livraison par commune et sur place, réservation des tables midi et soir. Le prospect essaie tout de
suite : commander, réserver, demander les allergènes ou les horaires.
"""
from decimal import ROUND_HALF_UP, Decimal

# (rubrique, nom, prix en FCFA, fiche)
MENU = [
    ("GRILLADE", "Poulet braisé", 4500, {"composition": "Poulet mariné aux épices, oignons, tomates", "sides": "attiéké, alloco ou riz",
                                          "spicy": "Un peu pimenté", "allergens": "Aucun des 14", "prep_time": "25 min"}),
    ("GRILLADE", "Poisson braisé", 6000, {"composition": "Carpe entière braisée, sauce tomate oignon", "sides": "attiéké, alloco ou riz",
                                           "spicy": "Pimenté", "allergens": "Poissons", "prep_time": "30 min"}),
    ("GRILLADE", "Brochettes de bœuf", 3000, {"composition": "Quatre brochettes, oignons, piment à part", "spicy": "Un peu pimenté",
                                               "allergens": "Aucun des 14"}),
    ("PLAT", "Kedjenou de poulet", 5000, {"composition": "Poulet mijoté à l'étouffée, légumes", "sides": "attiéké ou riz",
                                           "spicy": "Pimenté", "allergens": "Céleri"}),
    ("PLAT", "Sauce graine et riz", 4000, {"composition": "Sauce graine de palme, viande de bœuf, poisson fumé", "spicy": "Pimenté",
                                            "allergens": "Poissons"}),
    ("PLAT", "Garba", 1500, {"composition": "Attiéké, thon frit, oignons, tomates, piment", "spicy": "Très pimenté",
                              "allergens": "Poissons"}),
    ("ENTREE", "Salade avocat crevettes", 3500, {"composition": "Avocat, crevettes, tomates, vinaigrette", "allergens": "Crustacés, Moutarde"}),
    ("DESSERT", "Dêguê", 1000, {"composition": "Mil, lait caillé sucré", "allergens": "Lait", "vegetarian": "Oui"}),
    ("BOISSON", "Bissap", 500, {"volume": "50 cl", "allergens": "Aucun des 14"}),
    ("BOISSON", "Gnamakoudji", 500, {"volume": "50 cl", "allergens": "Aucun des 14"}),
]
KITCHEN_HOURS = {str(day): [["11:00", "23:00"]] for day in range(7)}
SERVICES = {str(day): [["12:00", "15:00"], ["19:00", "23:00"]] for day in range(7)}  # midi et soir
COVERS = 40
ABIDJAN_AREAS = [("Cocody", 1000), ("Plateau", 1000), ("Marcory", 1500), ("Yopougon", 1500)]
OTHER_AREAS = [("Centre-ville", 1000), ("Quartiers nord", 1500), ("Quartiers sud", 1500)]
FCFA = {"XOF", "XAF"}
EURO_RATE = Decimal("655.957")


def local_price(fcfa: int, currency: str) -> Decimal:
    """Prix de la carte dans la monnaie de la démo (FCFA tels quels ; sinon converti et arrondi à 0,50)."""
    if (currency or "XOF").upper() in FCFA:
        return Decimal(fcfa)
    value = Decimal(fcfa) / EURO_RATE
    return (value * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2 or Decimal("0.5")


async def seed(db, tenant) -> int:
    """Carte, livraison, horaires et réservation d'un restaurant de démo (jamais deux fois)."""
    from sqlalchemy import func, select

    from app.models.appointment_settings import TenantAppointmentSettings
    from app.models.product import Product

    if (await db.execute(select(func.count(Product.id)).where(Product.tenant_id == tenant.id))).scalar_one():
        return 0
    currency = (tenant.currency or "XOF").upper()
    for i, (code, name, price, sheet) in enumerate(MENU, start=1):
        db.add(Product(tenant_id=tenant.id, sku=f"DEMO-{i:02d}", name=name, price=local_price(price, currency),
                       currency=currency, stock_quantity=1, bob_category=code, sheet=dict(sheet), active=True))
    areas = ABIDJAN_AREAS if (tenant.country or "").upper() == "CI" else OTHER_AREAS
    tenant.delivery_areas = [{"name": n, "fee": float(local_price(fee, currency))} for n, fee in areas]
    tenant.delivery_enabled, tenant.pickup_enabled, tenant.dine_in_enabled = True, True, True
    tenant.kitchen_hours = KITCHEN_HOURS
    db.add(TenantAppointmentSettings(tenant_id=tenant.id, online_booking=True, opening_hours=SERVICES, slot_minutes=30,
                                     capacity=COVERS))
    await db.flush()
    return len(MENU)
