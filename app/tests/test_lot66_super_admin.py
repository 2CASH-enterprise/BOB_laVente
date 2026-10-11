"""
Lot 66 — Super Admin redessiné : vue d'ensemble (chiffres, à faire, parcours, activité), boutiques en cartes et
fiche (réglages tracés dans l'historique), fenêtres de Bob à la place de celles du navigateur, mode sombre.
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.preregistration import PreregistrationFile
from app.models.prospect import Prospect, ProspectEvent
from app.models.tenant import Tenant, TenantPlan
from app.tests.test_lot61_prospection import _admin

HTML = Path("app/static/superadmin/index.html").read_text(encoding="utf-8")


async def _tenant(db, name, **fields):
    tenant = Tenant(name=name, country="CI", currency="XOF", email=f"{uuid.uuid4().hex[:6]}@l66.ci", plan=TenantPlan.PRO, **fields)
    db.add(tenant)
    await db.commit()
    return tenant


@pytest.mark.asyncio
async def test_overview_counts_todo_and_activity(client, db_session):
    headers = await _admin(client, db_session, name="Koffi Yao")
    now = datetime.now(timezone.utc)
    await _tenant(db_session, "Active")
    await _tenant(db_session, "Démo", is_demo=True)
    awa = await _tenant(db_session, "Boutique Awa", prereg_at=now - timedelta(hours=2))
    await _tenant(db_session, "Ancien pré-inscrit", prereg_at=now - timedelta(days=10))
    await _tenant(db_session, "Bêta", prereg_at=now - timedelta(days=5), access_opened_at=now)
    due = await _tenant(db_session, "Auto Prestige", paid_until=date.today() + timedelta(days=3))
    db_session.add(PreregistrationFile(tenant_id=awa.id, kind="PRODUCTS", filename="a.csv", content_type="text/csv", size=3, data=b"x;y"))
    replied = Prospect(company="Cabinet K", code="rep001", status="ACTIVE", sector="INSURANCE_BROKER",
                       replied_at=now - timedelta(hours=1), last_contact_at=now - timedelta(days=3), contacted_at=now - timedelta(days=3))
    late = Prospect(company="Auto Plus", code="fol001", status="ACTIVE", sector="CAR_DEALERSHIP",
                    next_action_on=date.today() - timedelta(days=1), contacted_at=now - timedelta(days=2))
    answered = Prospect(company="Déjà répondu", code="ans001", status="ACTIVE", sector="ONLINE_STORE",
                        replied_at=now - timedelta(days=2), last_contact_at=now - timedelta(days=1), contacted_at=now - timedelta(days=4),
                        next_action_on=date.today() + timedelta(days=5))
    old = Prospect(company="Ancien", code="old001", status="ACTIVE", sector="ONLINE_STORE", created_at=now - timedelta(days=60))
    await _tenant(db_session, "Payé longtemps", paid_until=date.today() + timedelta(days=200))
    db_session.add_all([replied, late, answered, old])
    await db_session.flush()
    db_session.add(ProspectEvent(prospect_id=late.id, kind="DEMO", actor="prospect", at=now - timedelta(minutes=5)))
    await db_session.commit()
    r = await client.put(f"/api/v1/superadmin/tenants/{due.id}/plan", headers=headers, json={"plan": "ENTERPRISE"})
    assert r.status_code == 200

    d = (await client.get("/api/v1/superadmin/overview", headers=headers)).json()
    assert d["first_name"] == "Koffi" and d["me_name"] == "Koffi Yao" and d["prereg_mode"] is False
    k = d["kpis"]
    assert k["active"] == 4 and k["prereg"] == 2 and k["prereg_with_files"] == 1  # Active, Bêta, Auto Prestige, Payé longtemps
    assert k["replies"] == 1 and k["follow_up"] == 1 and k["ai_cost"] == 0
    kinds = [t["kind"] for t in d["todo"]]
    assert kinds == ["REPLIES", "PREREG", "BILLING", "FOLLOW_UP"]  # l'ancien pré-inscrit (10 jours) n'est plus « nouveau »
    assert d["todo"][0]["view"] == "replied" and "Cabinet K" in d["todo"][0]["detail"]
    assert d["todo"][1]["title"] == "Nouvelle pré-inscription : Boutique Awa" and "fichiers" in d["todo"][1]["detail"]
    assert d["todo"][2]["tenant_id"] == str(due.id) and "Auto Prestige se termine le" in d["todo"][2]["title"]
    whats = [(a["who"], a["what"]) for a in d["activity"]]
    assert ("Auto Plus", "a essayé la démo") in whats and ("Auto Prestige", "Plan changé : Pro → Entreprise") in whats
    assert next(a for a in d["activity"] if a["who"] == "Auto Prestige")["by"] == "Koffi Yao"
    assert [f["key"] for f in d["funnel"]][0] == "ADDED" and d["funnel"][0]["count"] == 3  # pas l'ancien (60 jours)
    assert (await client.get("/api/v1/superadmin/overview")).status_code == 401


@pytest.mark.asyncio
async def test_every_setting_change_is_traced_in_the_history(client, db_session):
    headers = await _admin(client, db_session, name="Koffi Yao")
    t = await _tenant(db_session, "Auto Prestige")
    base = f"/api/v1/superadmin/tenants/{t.id}"
    await client.put(f"{base}/plan", headers=headers, json={"plan": "STARTER"})
    await client.put(f"{base}/paid-until", headers=headers, json={"paid_until": "2026-11-17"})
    await client.put(f"{base}/paid-until", headers=headers, json={"paid_until": "2026-11-17"})  # inchangé : rien de tracé
    await client.put(f"{base}/commission-rate", headers=headers, json={"commission_rate": 2.5})
    await client.put(f"{base}/active", headers=headers, json={"active": False})
    await client.put(f"{base}/active", headers=headers, json={"active": True})
    await client.put(f"{base}/active", headers=headers, json={"active": True})  # déjà active : rien de tracé
    await client.put(f"{base}/business-type", headers=headers, json={"business_type": "CAR_DEALERSHIP"})
    r = await client.get(f"{base}/history", headers=headers)
    items = [i["what"] for i in r.json()["items"]]
    assert sorted(items) == sorted(["Secteur changé : Commerce → Concession automobile", "Boutique réactivée", "Boutique suspendue",
                     "Commission changée : — → 2.5 %", "Échéance changée (payé jusqu'au) : — → 17/11/2026",
                     "Plan changé : Pro → Starter"])  # même seconde en test : l'ordre n'est pas garanti
    assert all(i["by"] == "Koffi Yao" for i in r.json()["items"])
    summary = (await client.get(base, headers=headers)).json()
    assert summary["country"] == "CI" and summary["prereg_locked"] is False and "currency" in summary
    assert (await client.get(f"/api/v1/superadmin/tenants/{uuid.uuid4()}/history", headers=headers)).status_code == 404
    locked = await _tenant(db_session, "Pré-inscrit", prereg_at=datetime.now(timezone.utc))
    assert (await client.get(f"/api/v1/superadmin/tenants/{locked.id}", headers=headers)).json()["prereg_locked"] is True


def test_no_browser_dialogs_left():
    script = HTML[HTML.index("<script>\nconst API_BASE"):]
    assert not re.search(r"(?<![\w.])(confirm|alert|prompt)\(", script)
    assert "function saConfirm(" in script and "function toast(" in script
    assert script.count("saConfirm(") >= 10 and 'aria-modal="true"' in script
    assert 'if (e.key === "Escape") close(false);' in script  # Échap annule


def test_shell_sections_theme_and_startup():
    for section in ("overview", "shops", "prospects", "prereg", "aicosts"):
        assert f'id="{section}-section"' in HTML and f'data-section="{section}"' in HTML
    assert ':root[data-theme="dark"]' in HTML and "@media (prefers-color-scheme: dark)" in HTML
    assert 'localStorage.setItem("bob_sa_theme", theme)' in HTML and "family=Manrope" in HTML
    assert 'class="topbar"' in HTML and "function openMenu()" in HTML and "@media (max-width: 900px)" in HTML
    assert HTML.rstrip().endswith("if (token) enterApp();\n</script>\n</body>\n</html>".strip())  # démarrage en dernier
    assert "function extendShop()" in HTML and "Prolonger d'un mois" in HTML and "/history`" in HTML
    assert "function bootstrapAccount()" in HTML and 'id="bootstrap-form"' in HTML
