"""
Lot 91 — mêmes mots dans le mode simple et le tableau de bord complet (validé le 27/10).

Payée → Vendu / Vente confirmée ; Encaissé → Ventes confirmées ; En attente de paiement → Vente à confirmer ;
Attend un humain → Vous attend ; Rendre à l'IA → Bob continue ; Annuler la commande → Annuler la vente.
Seuls les mots vus par le commerçant changent : statuts internes, routes et messages au client restent les mêmes.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
OLD = ["Payée", "payée", "Encaissé", "encaissé", "En attente de paiement", "Attend un humain", "Rendre à l'IA",
       "Annuler la commande", "Paiement reçu", "non payée"]


def _visible_text(source: str) -> str:
    """Le code sans ses commentaires (les commentaires gardent l'historique des lots)."""
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in source.splitlines())


def test_the_dashboard_no_longer_uses_the_old_words():
    text = _visible_text(HTML)
    for word in OLD:
        assert word not in text, word


def test_both_dashboards_share_the_same_words():
    for word in ('PENDING: "Vente à confirmer", PAID: "Vendu"', 'WAITING_HUMAN: "Vous attend"', '>Bob continue</button>',
                 '"Ventes confirmées"', '"Ventes à confirmer"', 'confirmLabel: "Vendu", cancelLabel: "Retour"',
                 'confirmLabel: "Oui, annuler la vente", cancelLabel: "Retour"'):
        assert word in HTML, word
    # mode simple et commandes : la même question avant de confirmer
    assert HTML.count('title: "Confirmer la vente ?"') == 2 and HTML.count("Annuler cette vente ?") == 2


def test_merchant_emails_and_reports_use_the_new_words():
    handoff = (ROOT / "services" / "handoff_service.py").read_text(encoding="utf-8")
    report = (ROOT / "services" / "monthly_report.py").read_text(encoding="utf-8")
    home = (ROOT / "services" / "home_service.py").read_text(encoding="utf-8")
    assert "« Rendre à l'IA »" not in handoff and handoff.count("« Bob continue »") == 2
    assert "« Paiement reçu »" not in report and "cliquez « Vendu »" in report
    assert '"Ventes confirmées"' in report and '"Encaissé"' not in report
    assert '"Paiement reçu"' not in home and '"Vente confirmée"' in home


def test_customer_messages_keep_talking_about_payment():
    # Le client paie : ses messages gardent « paiement » (récapitulatif, relances).
    emails = (ROOT / "services" / "order_emails.py").read_text(encoding="utf-8")
    reminders = (ROOT / "services" / "order_reminders.py").read_text(encoding="utf-8")
    assert "'en attente de paiement'" in emails
    assert "toujours en attente de paiement" in reminders
