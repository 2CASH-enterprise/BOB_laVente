"""Lot 128 — documents légaux communs à AgenC'AI, à l'adresse neutre /legal/ (nginx), partie Naomy et conditions d'utilisation."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGAL = ROOT / "static" / "legal"
PRIVACY = (LEGAL / "privacy.html").read_text(encoding="utf-8")
CGU = (LEGAL / "cgu.html").read_text(encoding="utf-8")
MENTIONS = (LEGAL / "mentions-legales.html").read_text(encoding="utf-8")


def test_every_link_uses_the_neutral_address():
    for path in (ROOT / "static").rglob("*.html"):
        assert "/bob/legal/" not in path.read_text(encoding="utf-8"), path
    from app.services import prospect_mailer

    assert "https://agenc-ai.com/legal/mentions-legales.html" in prospect_mailer.LEGAL
    for page in (PRIVACY, CGU, MENTIONS):
        for href in ("/legal/mentions-legales.html", "/legal/privacy.html", "/legal/cgu.html"):
            assert f'href="{href}"' in page


def test_privacy_covers_naomy_in_both_languages():
    for perm in ("pages_show_list", "pages_manage_posts", "pages_read_engagement", "instagram_basic",
                 "instagram_content_publish"):
        assert PRIVACY.count(f"<code>{perm}</code>") == 2, perm
    assert "<code>business_management</code>" not in PRIVACY  # non confirmée dans la console Meta (22/11)
    for words in ("5. Naomy : données des commerçants et de leurs contenus", "5. Naomy: merchant data and content",
                  "12 mois après la publication, puis effacés", "12 months after publication, then erased",
                  "15 minutes au plus", "Naomy AI"):
        assert words in PRIVACY, words
    assert "audit" not in PRIVACY.lower()  # mini-audit pas encore en service
    assert PRIVACY.count('id="supprimer"') == 1 and PRIVACY.count('id="delete-data"') == 1


def test_terms_are_a_marked_draft_with_annexes():
    assert "Projet en cours de relecture juridique" in CGU
    for part in ("Annexe A — Bob", "Annexe B — Naomy", "Annexe C — Accord de sous-traitance (RGPD, article 28)"):
        assert part in CGU, part
    assert len(re.findall(r"<h3[^>]*>N\d+\. ", CGU)) == 12 and len(re.findall(r"<h3[^>]*>B\d\. ", CGU)) == 7
    assert "[À COMPLÉTER" in CGU  # pas de prix tant que le modèle économique n'est pas décidé
    assert "€" not in CGU and "FCFA" not in CGU
    assert "AgenC'AI n'encaisse aucun paiement" in CGU
