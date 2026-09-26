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


# --- Lot 18 : système visuel, mode sombre -------------------------------------------------

def _block(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n  \}", HTML, re.S)
    assert match, selector
    return match.group(1)


def test_dark_theme_redefines_every_color_token():
    light = set(re.findall(r"(--[a-z0-9-]+):", _block(":root")))
    dark = set(re.findall(r"(--[a-z0-9-]+):", _block('html[data-theme="dark"]')))
    layout_only = {"--radius", "--radius-sm", "--topbar-h", "--safe-top", "--safe-bottom"}
    assert light - layout_only - dark == set()


def test_theme_is_applied_before_first_paint_and_survives_blocked_storage():
    head = HTML.split("</head>")[0]
    assert 'document.documentElement.setAttribute("data-theme", theme)' in head
    assert 'try { theme = localStorage.getItem("bob_theme"); } catch (e) {}' in head
    assert "prefers-color-scheme: dark" in head


def test_theme_switch_in_sidebar():
    sidebar = re.search(r'<aside class="sidebar".*?</aside>', HTML, re.S).group(0)
    assert "setTheme('light')" in sidebar and "setTheme('dark')" in sidebar
    assert 'try { localStorage.setItem("bob_theme", theme); } catch (e) {}' in HTML


def test_menu_uses_line_icons_not_emojis():
    nav = re.search(r"<nav>(.*?)</nav>", HTML, re.S).group(1)
    assert nav.count("<svg") == nav.count("data-tab=")
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", nav)


def test_logo_on_sidebar_and_login_and_favicon():
    assert HTML.count('class="brand-mark"') >= 6  # barre latérale + 5 écrans de connexion
    assert 'rel="icon" type="image/svg+xml"' in HTML


def test_no_hardcoded_colors_in_templates():
    """Toute couleur d'interface passe par les variables du thème (sinon illisible en mode sombre).
    Seule exception : l'image QR à partager, dessinée sur un canvas."""
    script = HTML.split("<script>", 2)[-1]
    offenders = [
        line.strip() for line in script.splitlines()
        if re.search(r"#[0-9A-Fa-f]{3,6}\b", line) and "ctx." not in line and "addColorStop" not in line
    ]
    assert offenders == []


def test_status_badges_show_french_labels():
    assert "STATUS_LABELS[c.status] || c.status" in HTML and "STATUS_LABELS[o.status] || o.status" in HTML
    assert 'WAITING_HUMAN: "Attend un humain"' in HTML


# --- Lot 19 : accueil -------------------------------------------------------------------

def test_home_is_first_and_existing_cards_stay_below():
    overview = _section("overview")
    order = [overview.index(x) for x in ('id="home-root"', "Détails", 'id="analytics-grid"', 'id="sales-card"',
                                         'id="signals-card"', 'id="strategies-card"')]
    assert order == sorted(order)
    assert "loadHome();" in re.search(r"async function loadOverview\(\) \{(.*?)\n\}", HTML, re.S).group(1)


def test_home_escapes_everything_that_comes_from_customers():
    render = re.search(r"function renderHome\(h\) \{(.*?)\n\}", HTML, re.S).group(1)
    for field in ("t.customer", "t.detail", "t.reason", "a.title", "a.detail", "h.first_name", "item.conversation_id"):
        assert f"${{esc({field}" in render or f"esc({field}" in HTML, field
    assert "${t.customer}" not in render and "${t.detail}" not in render and "${a.detail}" not in render


def test_home_actions_reuse_existing_navigation():
    assert "jumpToCustomerConversation('${esc(item.conversation_id)}')" in HTML
    assert "if (item.kind === \"ORDER\") return `showTab('orders')`" in HTML
