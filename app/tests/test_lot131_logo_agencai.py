"""Lot 131 — logo définitif d'AgenC'AI (25/11) : icône dans les en-têtes et pieds de page, favicons des pages AgenC'AI."""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"
PAGES = ["agencai/index.html", "legal/privacy.html", "legal/cgu.html", "legal/mentions-legales.html"]


def test_pages_use_the_logo_and_the_favicon():
    for page in PAGES:
        html = (STATIC / page).read_text(encoding="utf-8")
        assert '/bob/brand/icone-64.png' in html and 'rel="icon" href="/bob/brand/favicon.ico"' in html, page
        assert 'rel="apple-touch-icon" href="/bob/brand/apple-touch-icon.png"' in html, page
        assert '">A</span>' not in html and "<span>A</span>" not in html, page  # plus de « A » provisoire
    assert "/bob/brand/icone-64.png" in (STATIC / "landing" / "index.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kind", [("favicon.ico", "image/"), ("icone-32.png", "image/png"), ("icone-64.png", "image/png"),
                                       ("icone-192.png", "image/png"), ("apple-touch-icon.png", "image/png"),
                                       ("logo-512.png", "image/png")])
async def test_brand_files_are_served(client, name, kind):
    r = await client.get(f"/brand/{name}")
    assert r.status_code == 200 and r.headers["content-type"].startswith(kind) and len(r.content) > 1000
