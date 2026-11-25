"""Lot 132 — le visage de Bob remplace l'ancien logo (couches) dans ses tableaux de bord ; favicons de toutes ses pages."""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"


def test_bob_pages_show_his_face_and_have_a_favicon():
    for page in ("dashboard/index.html", "superadmin/index.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert 'polyline points="7,31' not in html, page  # plus d'ancien logo
        assert 'src="../brand/bob-128.png"' in html and "../brand/bob-favicon.ico" in html, page
    for page, prefix in (("instant-demo/index.html", "../"), ("landing/index.html", ""), ("m/index.html", "../")):
        assert f'href="{prefix}brand/bob-favicon.ico"' in (STATIC / page).read_text(encoding="utf-8"), page


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/brand/bob-favicon.ico", "/brand/bob-32.png", "/brand/bob-128.png",
                                  "/dashboard/icon-192.png", "/dashboard/icon-maskable-512.png", "/dashboard/apple-touch-icon.png"])
async def test_bob_icons_are_served(client, path):
    r = await client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/") and len(r.content) > 1000
