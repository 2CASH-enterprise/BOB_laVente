"""
Lot 79 B — stratégies de réponse aux objections de l'agence immobilière. Chaque frein a ses stratégies, toujours
honnêtes (faits réels, jamais de prix d'estimation, de chiffre de crédit, de remise ni de promesse sur un dossier) ;
réglables et mesurées comme celles des autres secteurs ; les autres secteurs sont inchangés.
"""
import random
import re
import uuid
from datetime import datetime, timezone

import pytest

from app.agents.strategies import (
    REAL_ESTATE,
    REAL_ESTATE_GUARDRAILS,
    STRATEGIES,
    priority_for,
    select_strategy,
    strategies_for,
    strategy_instruction,
)
from app.agents.taxonomy import REAL_ESTATE_OBJECTIONS
from app.core.security import hash_password
from app.models.tenant import Tenant, TenantPlan
from app.models.user import Role, User

AGENCY = [s for s in STRATEGIES if s.activity == REAL_ESTATE]


def test_every_objection_of_the_agency_has_its_strategies():
    assert set(priority_for(REAL_ESTATE)) == set(REAL_ESTATE_OBJECTIONS)
    for objection in REAL_ESTATE_OBJECTIONS:
        items = strategies_for(objection, REAL_ESTATE)
        assert 2 <= len(items) <= 3, objection
        assert all(s.code.startswith("IMMO_") for s in items)
    assert len(AGENCY) == 22
    # Les autres secteurs ne voient jamais les stratégies de l'agence, et inversement.
    for sector in ("ONLINE_STORE", "CAR_DEALERSHIP", "INSURANCE_BROKER"):
        assert not any(s.code.startswith("IMMO_") for o in priority_for(sector) for s in strategies_for(o, sector))


def test_guardrails_are_specific_to_the_agency():
    for strategy in AGENCY:
        text = strategy_instruction(strategy)
        assert text.endswith(REAL_ESTATE_GUARDRAILS)
    for words in ("prix d'estimation", "chiffre de crédit", "aucune remise", "dossier sera accepté", "fausse concurrence",
                  "vie personnelle"):
        assert words in REAL_ESTATE_GUARDRAILS, words


def test_no_strategy_invents_a_figure_a_discount_or_a_promise():
    for strategy in AGENCY:
        text = strategy.instruction
        assert not re.search(r"\d+\s?(%|€|euros?)", text), strategy.code  # aucun chiffre écrit dans une consigne
        assert "accorde une remise" not in text.replace("N'accorde jamais de remise", "").replace("N'accorde JAMAIS de remise", "")
        assert "sera accepté" not in text.replace("Ne dis JAMAIS que son dossier sera accepté", "")
    by_code = {s.code: s.instruction for s in AGENCY}
    assert "N'accorde JAMAIS de remise" in by_code["IMMO_HONORAIRES_TRANSPARENCE"]
    assert "Ne donne AUCUN chiffre" in by_code["IMMO_FIN_POINT"] and "Ne donne AUCUN chiffre" in by_code["IMMO_FIN_PARALLELE"]
    assert "Ne dis JAMAIS que son dossier sera accepté ou refusé" in by_code["IMMO_DOSSIER_PIECES"]
    assert "consigne 9" in by_code["IMMO_DOSSIER_PIECES"]  # seulement les pièces autorisées
    assert "record_marketing_consent" in by_code["IMMO_INDISPO_ALERTE"]  # les alertes du lot 80 demandent l'accord
    assert "n'en cite aucun" in by_code["IMMO_FIN_CONSEILLER"]  # jamais un partenaire inventé


def test_selection_follows_the_agency_priority():
    rng = random.Random(1)
    chosen, _ = select_strategy(["HESITATION", "DOSSIER", "PRIX_TROP_ELEVE"], set(), rng, business_type=REAL_ESTATE)
    assert chosen.objection == "DOSSIER"  # le dossier passe avant le prix et l'hésitation
    disabled = {s.code for s in strategies_for("CONFIANCE", REAL_ESTATE)} - {"IMMO_CONFIANCE_VISITE"}
    chosen, _ = select_strategy(["CONFIANCE"], disabled, rng, business_type=REAL_ESTATE)
    assert chosen.code == "IMMO_CONFIANCE_VISITE"
    # Une objection du commerce n'existe pas pour l'agence : aucune stratégie.
    assert select_strategy(["FRAIS_LIVRAISON"], set(), rng, business_type=REAL_ESTATE) == (None, None)


@pytest.mark.asyncio
async def test_agency_settings_screen_lists_its_strategies(client, db_session):
    email = f"s{uuid.uuid4().hex[:8]}@l79b.fr"
    tenant = Tenant(name="Agence du Parc", country="FR", currency="EUR", email=email, plan=TenantPlan.PRO,
                    business_type=REAL_ESTATE, business_type_chosen_at=datetime.now(timezone.utc))
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(User(tenant_id=tenant.id, email=email, hashed_password=hash_password("x"), full_name="U", role=Role.OWNER))
    await db_session.commit()
    token = (await client.post("/api/v1/auth/login", data={"username": email, "password": "x"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    body = (await client.get("/api/v1/tenants/me/strategy-settings", headers=headers)).json()
    groups = body["objections"]
    assert [g["objection"] for g in groups] == priority_for(REAL_ESTATE)
    assert groups[1]["label"] == "Dossier de location"
    # Garder au moins une stratégie par frein.
    all_trust = [s["code"] for s in groups[0]["strategies"]]
    refused = await client.put("/api/v1/tenants/me/strategy-settings", headers=headers, json={"disabled": all_trust})
    assert refused.status_code in (400, 422) and "Confiance" in refused.json()["detail"]
