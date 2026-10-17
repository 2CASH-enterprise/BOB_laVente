"""
Lot 81 — charger tout de suite les ventes DVF d'un ou plusieurs départements (sur le serveur) :
    docker compose exec api python -m app.scripts.dvf_load 69 75
Sans cette commande, la tâche automatique les charge dans l'heure qui suit la première annonce du département.
"""
import asyncio
import sys


async def main(codes: list[str]) -> None:
    from app.core.database import AsyncSessionLocal
    from app.services import dvf

    for code in codes:
        async with AsyncSessionLocal() as db:
            count = await dvf.load_department(db, code.upper())
            row = await db.get(dvf.DvfDepartment, code.upper())
            print(f"Département {code} : {count} ventes gardées" + (f" ({row.error})" if row and row.error else
                                                                    f" (années {row.years})" if row else ""))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Indiquez au moins un département, par exemple : python -m app.scripts.dvf_load 69")
        sys.exit(1)
    asyncio.run(main(sys.argv[1:]))
