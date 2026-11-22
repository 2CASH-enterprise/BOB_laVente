"""Lot 127 — page d'accueil d'AgenC'AI, page de Bob « restaurant », mentions légales et confidentialité AgenC'AI.

Remplace la page de présentation du lot 71 (multi-secteurs, style Naomy) : Bob n'est présenté qu'aux restaurants.
"""
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

STATIC = Path(__file__).resolve().parents[1] / "static"
BOB = (STATIC / "landing" / "index.html").read_text(encoding="utf-8")
HOME = (STATIC / "agencai" / "index.html").read_text(encoding="utf-8")
MENTIONS = (STATIC / "legal" / "mentions-legales.html").read_text(encoding="utf-8")
PRIVACY = (STATIC / "legal" / "privacy.html").read_text(encoding="utf-8")
CGU = (STATIC / "legal" / "cgu.html").read_text(encoding="utf-8")
PAGES = {"bob": BOB, "home": HOME, "mentions": MENTIONS, "privacy": PRIVACY, "cgu": CGU}


@pytest.mark.parametrize("name", list(PAGES))
def test_charte_agencai_mobile_first_and_no_inline_handlers(name):
    page = PAGES[name]
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert "family=Plus+Jakarta+Sans" in page
    assert not re.search(r"\son[a-z]+=", page)  # lot 70 : rien dans les attributs on…
    assert "{{" not in page and "sc-if" not in page and ".dc.html" not in page and "/_blob/" not in page


def test_bob_is_presented_for_restaurants_only_with_whatsapp_screens():
    assert "Le serveur IA des restaurants" in BOB
    for words in ("Commander", "Réserver une table", "Voir la carte", "Attiéké", "Envoyer ma position",
                  "Commande confirmée", "QR code sur chaque table", "Vous gardez la main"):
        assert words in BOB, words
    assert BOB.count('role="img" aria-label="Écran') == 5  # accueil + 4 écrans de commande
    for other in ("concession", "assurance", "immobili"):
        assert other not in BOB.lower(), other


def test_bob_buttons_keep_their_targets_and_preregistration():
    assert 'href="demo/?secteur=restaurant"' in BOB and 'href="dashboard/"' in BOB
    assert 'href="dashboard/?preinscription=1"' in BOB
    assert 'fetch("api/v1/preregistration/public")' in BOB and "p.offer_open" in BOB and "2 premiers mois" in BOB
    assert 'id="header-cta"' in BOB and 'id="hero-cta"' in BOB and 'id="launch"' in BOB
    assert 'href="/legal/privacy.html"' in BOB and 'href="/legal/mentions-legales.html"' in BOB and 'href="/legal/cgu.html"' in BOB
    assert 'href="/bob/agencai/"' in BOB


def test_no_subscription_price_and_no_whatsapp_logo():
    """Aucun prix d'abonnement affiché tant que rien n'est définitif ; les portraits sont recadrés sans le logo."""
    for page in (BOB, HOME):
        assert "€ HT" not in page and "/ mois" not in page
        assert "WhatsApp est une marque de Meta Platforms, Inc." in page
    for name in ("bob", "naomy", "fely"):
        assert (STATIC / "landing" / "img" / f"{name}.jpg").stat().st_size > 20_000
        assert f"/bob/img/{name}.jpg" in HOME


def test_home_presents_the_three_agents_and_links_to_bob():
    for agent, role in (("Naomy", "Community manager"), ("Bob", "Vendeur"), ("Fely", "Croissance")):
        assert agent in HOME and role in HOME
    assert '<a class="card" href="/bob/"' in HOME  # Naomy et Fely : pages faites par leurs sessions, rattachées plus tard
    assert 'href="/legal/mentions-legales.html"' in HOME and 'href="/legal/privacy.html"' in HOME and 'href="/legal/cgu.html"' in HOME
    assert 'id="menu-btn"' in HOME and 'id="menu"' in HOME


def test_legal_notice_names_the_publisher_only_there():
    assert "Noé BAKOUBA KALJOP, entrepreneur individuel (EI)" in MENTIONS and "132 945 858" in MENTIONS
    assert "+33 7 44 17 74 30" in MENTIONS and "Hetzner Online GmbH" in MENTIONS
    assert "TVA" not in MENTIONS  # statut à confirmer avec l'expert-comptable
    for page in (BOB, HOME, PRIVACY, CGU):
        assert "BAKOUBA" not in page
    for page in PAGES.values():
        assert "2 Cash" not in page and "2CASH" not in page and "bakouban2002" not in page


def test_privacy_names_the_ai_provider_actually_used():
    from app.core.config import get_settings

    assert get_settings().llm_provider in ("mistral", "anthropic")
    assert PRIVACY.count("Mistral AI (France)") >= 4  # serveur : LLM_PROVIDER=mistral (relevé le 22/11)


def test_prospecting_emails_no_longer_mention_2_cash():
    from app.services import prospect_mailer

    assert "2 Cash" not in prospect_mailer.LEGAL and "AgenC'AI" in prospect_mailer.LEGAL


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["/", "/agencai/", "/legal/mentions-legales.html", "/legal/privacy.html", "/legal/cgu.html",
                                 "/img/bob.jpg"])
async def test_pages_are_served(url):
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(url)
    assert response.status_code == 200, url
