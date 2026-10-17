"""
Lot 78 C — « Impossible de charger l'accueil : Cannot read properties of undefined (reading 'value') » : après une mise
à jour, le navigateur gardait l'ancienne page, dont le code ne comprenait plus la réponse de l'accueil. Les pages sont
désormais toujours revalidées, et l'accueil choisit sa forme d'après les chiffres reçus.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path("app/static/dashboard/index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/dashboard/", "/dashboard/index.html", "/dashboard/sw.js",
                                  "/dashboard/manifest.webmanifest", "/superadmin/", "/legal/privacy.html", "/"])
async def test_pages_are_always_revalidated(client, path):
    response = await client.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    again = await client.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304  # rien n'a changé : rien n'est renvoyé


@pytest.mark.asyncio
async def test_images_keep_their_normal_cache(client):
    response = await client.get("/dashboard/icon-192.png")
    assert response.status_code == 200 and "cache-control" not in response.headers


def _render(home: dict) -> str:
    """Exécute renderHome dans Node avec un faux document ; renvoie le HTML produit ou l'erreur."""
    start = HTML.index("function frMoney(")
    code = HTML[start:HTML.index("async function loadPlanUsage() {")]
    script = """
const out = {}; const document = { getElementById: (id) => (out[id] = out[id] || { innerHTML: "", scrollIntoView() {} }) };
const esc = (s) => String(s ?? ""); const jsArg = (v) => JSON.stringify(v); const homeIcon = () => ""; const ACTIVITY_STYLE = {};
const loadOnboarding = () => {}; const loadPlanUsage = () => {}; const insuranceHomeRow = () => "";
""" + code + """
try { renderHome(%s); console.log(out["home-root"].innerHTML.includes("kpi-value") ? "OK" : "VIDE"); }
catch (e) { console.log("ERREUR " + e.message); }
""" % __import__("json").dumps(home)
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30).stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize("sector", ["ONLINE_STORE", "CAR_DEALERSHIP", "REAL_ESTATE", "INSURANCE_BROKER"])
def test_home_renders_whatever_the_shape_of_the_figures(sector):
    from app.services.preregistration import example_home

    for figures_of in ["ONLINE_STORE", "CAR_DEALERSHIP", "REAL_ESTATE", "INSURANCE_BROKER"]:
        home = {**example_home(figures_of, "Lina", "EUR"), "business_type": sector}
        assert _render(home) == "OK", (sector, figures_of)
