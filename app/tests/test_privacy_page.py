"""
Politique de confidentialité (lot 23a) : Meta l'examine avant d'accorder les accès avancés.
Elle doit couvrir les données reçues de Meta, le traitement par IA et la suppression des données,
en français et en anglais, sans promettre ce que le code ne fait pas.
"""
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

HTML = (Path(__file__).resolve().parents[1] / "static" / "legal" / "privacy.html").read_text(encoding="utf-8")


def test_both_languages_and_deletion_anchors():
    assert 'id="fr" lang="fr"' in HTML and 'id="en" lang="en"' in HTML
    assert 'id="supprimer"' in HTML and 'id="delete-data"' in HTML
    assert 'href="#delete-data"' in HTML and 'href="#supprimer"' in HTML


@pytest.mark.parametrize("permission", ["whatsapp_business_messaging", "whatsapp_business_management",
                                        "catalog_management", "public_profile"])
def test_every_requested_meta_permission_is_explained_in_both_languages(permission):
    assert HTML.count(f"<code>{permission}</code>") == 2


def test_processors_listed_in_both_languages():
    for name in ("Meta Platforms", "Mistral AI", "Hetzner", "Brevo", "Shopify"):
        assert HTML.count(name) >= 2, name


def test_no_promise_the_code_does_not_keep():
    """Les jetons ne sont pas chiffrés en base aujourd'hui : la page ne doit pas le prétendre."""
    lowered = HTML.lower()
    for claim in ("chiffré", "encrypted", "nom de profil", "profile name"):
        assert claim not in lowered, claim
    assert "STOP" in HTML  # mot-clé réellement reconnu par consent_service


@pytest.mark.asyncio
async def test_page_is_served():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/legal/privacy.html")
    assert response.status_code == 200
    assert "Delete your data" in response.text
