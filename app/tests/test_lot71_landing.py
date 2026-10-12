"""Lot 71 — page de présentation de Bob au style de Naomy AI (polices, logo, secteurs, tarifs à jour)."""
import re
from pathlib import Path

LANDING = Path("app/static/landing/index.html").read_text(encoding="utf-8")


def test_same_fonts_colors_and_logo_as_naomy():
    assert "family=Sora:" in LANDING and "family=DM+Sans:" in LANDING and "family=Inter" not in LANDING
    for token in ("--bg: #0E0E0E", "--gold: #C9A84C", "--gold2: #D4A843", "--gold3: #E8C46A"):
        assert token in LANDING
    assert LANDING.count('<polyline points="7,31 24,39.5 41,31"') == 1  # logo à trois couches


def test_prices_and_caps_match_plan_quota():
    from app.services.plan_quota import DEFAULT_QUOTAS, PRICES

    for plan, (fcfa, eur) in ((p, v) for p, v in PRICES.items() if v):
        assert f"{fcfa:,}".replace(",", " ") + " <small>FCFA</small>" in LANDING, plan
        assert f"soit {eur} € HT / mois" in LANDING, plan
    for plan, cap in DEFAULT_QUOTAS.items():
        if cap:
            assert f"Jusqu'à {cap:,} conversations".replace(",", " ") in LANDING, plan
    assert "200€" not in LANDING and "700€" not in LANDING  # anciens tarifs retirés


def test_three_sector_scenarios_with_whatsapp_screens():
    for key in ("commerce", "auto", "assurance"):
        assert f'id="sector-{key}"' in LANDING and f'data-sector="{key}"' in LANDING
    assert LANDING.count('class="phone"') == 3
    assert "Bob ne donne jamais de prix d'assurance" in LANDING


def test_no_inline_handlers_and_buttons_keep_their_targets():
    assert not re.search(r"\son[a-z]+=", LANDING)  # lot 70 : rien dans les attributs on…
    assert '<a class="float" href="demo/"' in LANDING
    assert 'href="dashboard/"' in LANDING and 'href="dashboard/?preinscription=1"' in LANDING
    assert 'href="mailto:contact@agenc-ai.com"' in LANDING
    assert 'fetch("api/v1/preregistration/public")' in LANDING and "p.offer_open" in LANDING
    assert 'id="gift-title"' in LANDING and 'id="launch"' in LANDING


def test_footer_like_naomy_and_tutoiement():
    assert "Bob AI est une solution développée par <b>SAS 2 Cash Enterprise</b>." in LANDING
    assert "Exploitation : <b>Agen'C AI</b> — Abidjan, Côte d'Ivoire" in LANDING
    assert "Teste avec ton catalogue" in LANDING
