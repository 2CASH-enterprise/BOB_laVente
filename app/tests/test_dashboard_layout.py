"""
Organisation du tableau de bord (lot 17) : chaque carte dans l'onglet où le commerçant la cherche,
et chargée quand cet onglet s'ouvre.
"""
import re
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")


def _section(tab: str) -> str:
    match = re.search(rf'<section id="tab-{tab}"[^>]*>(.*?)\n    </section>', HTML, re.S)
    assert match, f"onglet {tab} introuvable"
    return match.group(1)


def _show_tab_line(tab: str) -> str:
    body = re.search(r"function showTab\(name\) \{(.*?)\n\}", HTML, re.S).group(1)
    return next(line for line in body.splitlines() if f'name === "{tab}"' in line)


@pytest.mark.parametrize("tab, cards", [
    ("integrations", ["Compte WhatsApp Business", "Liens WhatsApp &amp; widgets", "Shopify", "Meta Commerce Catalog"]),
    ("bob", ["Négociation de prix", "Transmission à un humain", "Réponses aux objections", "Relances automatiques"]),
    ("settings", ["Sécurité du compte"]),
    ("products", ["Ajouter un produit", "Importer un catalogue CSV", "Codes QR produits"]),
])
def test_each_card_is_in_its_tab(tab, cards):
    section = _section(tab)
    for card in cards:
        assert card in section, f"« {card} » devrait être dans l'onglet {tab}"


def test_moved_cards_left_their_old_tab():
    assert "Compte WhatsApp Business" not in _section("settings")
    assert "Négociation de prix" not in _section("settings")
    assert "Liens WhatsApp" not in _section("products")


def test_integrations_start_with_whatsapp_then_links():
    section = _section("integrations")
    order = [section.index(c) for c in ("Compte WhatsApp Business", "Liens WhatsApp", "Shopify", "Meta Commerce Catalog")]
    assert order == sorted(order)


def test_each_element_id_exists_once():
    ids = re.findall(r'\sid="([^"$]+)"', HTML)
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates


def test_new_tab_in_menu_after_knowledge_base():
    nav = re.search(r"<nav>(.*?)</nav>", HTML, re.S).group(1)
    tabs = re.findall(r'data-tab="([a-z]+)"', nav)
    assert tabs.index("bob") == tabs.index("knowledge") + 1
    assert 'data-label="Réglages de Bob"' in nav
    assert '"bob", "settings"].forEach' in HTML  # l'onglet est bien masqué / affiché avec les autres


def test_each_tab_loads_what_it_shows():
    integrations = _show_tab_line("integrations")
    assert "loadWhatsAppStatus()" in integrations and "loadContactPoints()" in integrations
    bob = _show_tab_line("bob")
    for loader in ("loadNegotiationSettings()", "loadHandoffSettings()", "loadStrategySettings()", "loadFollowupSettings()"):
        assert loader in bob
    settings = _show_tab_line("settings")
    assert "loadMfaStatus()" in settings and "loadWhatsAppStatus" not in settings


def test_links_card_loads_products_when_opened_directly():
    """Le ciblage d'un produit (?p=SKU) a besoin de la liste des produits, même sans passer par Produits."""
    loader = re.search(r"async function loadContactPoints\(\) \{(.*?)\n\}", HTML, re.S).group(1)
    assert 'if (!productsLoaded) { allProducts = await api("/api/v1/products")' in loader


def test_no_text_points_to_the_old_location():
    assert "Paramètres → Réponses aux objections" not in HTML
    assert "Réglages de Bob → Réponses aux objections" in HTML
