"""
Lot 56 — nouvelles tâches plus visibles : son, fenêtre et titre qui clignote dans le tableau de bord.
Seule une NOUVELLE tâche prévient (comme la pastille du lot 37b), jamais le nom d'un client ; un onglet en
arrière-plan ne « consomme » pas la notification du téléphone (seen=false).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.models.conversation import Conversation, ConversationStatus
from app.models.customer import Customer
from app.models.push_subscription import NotificationState
from app.services.notifications import KINDS, TITLES
from app.tests.test_lot53_courtier import _cabinet, _headers

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
SW = (ROOT / "static" / "dashboard" / "sw.js").read_text(encoding="utf-8")


def _function(name):
    body = HTML[HTML.index(f"function {name}("):]
    return body[:body.index("\n}\n") + 2]


# --- Serveur ------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_tab_does_not_swallow_the_phone_notification(client, db_session):
    tenant = await _cabinet(db_session)
    tenant_id = tenant.id
    headers = await _headers(client, tenant)
    customer = Customer(tenant_id=tenant.id, whatsapp_number="2250700000001")
    db_session.add(customer)
    await db_session.flush()
    db_session.add(Conversation(tenant_id=tenant.id, customer_id=customer.id, status=ConversationStatus.WAITING_HUMAN))
    await db_session.commit()

    hidden = await client.get("/api/v1/notifications/counts?seen=false", headers=headers)
    assert hidden.status_code == 200 and hidden.json()["conversations"] == 1
    assert await db_session.get(NotificationState, tenant.id) is None  # rien d'enregistré : le téléphone sera prévenu
    seen = await client.get("/api/v1/notifications/counts", headers=headers)
    assert seen.json()["conversations"] == 1
    db_session.expire_all()
    state = await db_session.get(NotificationState, tenant_id)
    assert state is not None and state.counts["conversations"] == 1


def test_service_worker_wakes_the_open_tabs():
    push = SW[SW.index('self.addEventListener("push"'):SW.index('self.addEventListener("notificationclick"')]
    assert 'postMessage({ type: "bob-refresh-counts" })' in push and "tellOpenTabs" in push
    waited = push[push.index("event.waitUntil(Promise.all(["):]
    assert waited.index("tellOpenTabs,") < waited.index("showNotification(")  # attendu : le navigateur ne coupe pas avant
    assert 'e.data.type === "bob-refresh-counts") refreshTaskCounts()' in HTML


# --- Tableau de bord ----------------------------------------------------------------------------------------

def test_every_task_kind_has_an_alert_and_a_page():
    labels = HTML[HTML.index("var TASK_ALERT_LABELS = {"):HTML.index("var TASK_ALERT_TAB")]
    tabs = HTML[HTML.index("var TASK_ALERT_TAB = {"):HTML.index("var SOUND_GAP_MS")]
    for kind in KINDS:
        assert f"  {kind}: [" in labels, kind
        assert f"{kind}: \"" in tabs, kind
        assert f'id="tab-{re.search(kind + r": \"(\w+)\"", tabs).group(1)}"' in HTML
    assert dict(re.findall(r'(\w+): "(\w+)"', tabs)) == {
        "conversations": "conversations", "appointments": "appointments", "outcomes": "appointments", "orders": "orders",
        "callbacks": "overview", "quotes": "quotes", "renewals": "contracts", "complaints": "complaints"}
    # Même ordre de priorité que les notifications du téléphone
    order = re.findall(r"^  (\w+): \[", labels, re.M)
    assert order == list(TITLES)
    for kind, title in TITLES.items():
        assert f'{kind}: ["{title}"' in labels, kind


def test_polling_rhythm_and_seen_flag():
    refresh = _function("refreshTaskCounts")
    assert 'document.visibilityState === "visible"' in refresh and '"?seen=false"' in refresh
    assert refresh.index("showTaskCounts(counts)") < refresh.index("noticeNewTasks(counts)")
    assert "var COUNTS_VISIBLE_MS = 20000;" in HTML and "var COUNTS_HIDDEN_MS = 60000;" in HTML
    start = _function("startTaskCounts")
    assert "lastTaskCounts = null;" in start and "setInterval(" in start and "refreshSoundCard" in start
    assert "closeTaskAlert();" in _function("stopTaskCounts")


def test_alert_window_is_general_and_escaped():
    show = _function("showTaskAlert")
    assert "esc(title)" in show and "esc(taskAlertLabel(k, pendingAlert[k]))" in show
    for forbidden in ("customer", "content", "message", "first_name"):
        assert forbidden not in show, forbidden
    assert 'role", "status"' in show and 'aria-live", "assertive"' in show
    assert "Voir" in show and "closeTaskAlert()" in show and "setSoundEnabled(!soundEnabled())" in show
    assert "setTimeout" not in show  # 4A : la fenêtre reste jusqu'à ce qu'on la ferme
    assert "Son bloqué par le navigateur" in show
    assert "showTab(tab)" in _function("openTaskAlert")


def test_sound_rules():
    assert 'localStorage.getItem("bob_task_sound") !== "off"' in _function("soundEnabled")  # 3A : activé par défaut
    play = _function("playTaskSound")
    assert "SOUND_GAP_MS" in play and "bob_task_sound_at" in play and "if (!force && !soundEnabled()) return false;" in play
    assert "var SOUND_GAP_MS = 10000;" in HTML
    assert "new Ctx()" in _function("unlockAudio") and '["pointerdown", "keydown", "touchstart"]' in HTML
    assert 'id="sound-toggle"' in HTML and "testTaskSound()" in HTML and 'id="sound-card"' in HTML
    assert "<audio" not in HTML and ".mp3" not in HTML  # aucun fichier à télécharger


def test_title_flash_only_in_background():
    notice = _function("noticeNewTasks")
    assert 'if (document.visibilityState !== "visible") startTitleFlash(counts.total);' in notice
    assert "if (previous === null) return;" in notice  # tâches déjà là à l'ouverture : aucune alerte
    assert 'document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") stopTitleFlash(); });' in HTML


def test_no_browser_popups_and_vouvoiement():
    block = HTML[HTML.index("// ---- Lot 56"):HTML.index("// -- Réglage dans Paramètres --")]
    assert "alert(" not in block.replace("TaskAlert(", "").replace("taskAlert", "").replace("Alert(", "")
    assert not re.search(r"\b(tu|ton|ta|tes)\b", re.sub(r"//[^\n]*", "", block))


# --- La logique, exécutée pour de vrai (Node) ---------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js absent")
def test_alert_logic_runs():
    code = "\n".join([
        HTML[HTML.index("var TASK_ALERT_LABELS = {"):HTML.index("var SOUND_GAP_MS")],
        _function("taskAlertLabel"), _function("newTaskKinds"),
        """
        const out = {
          first: newTaskKinds({}, {conversations: 0, quotes: 0}),
          one: newTaskKinds({conversations: 1, quotes: 2}, {conversations: 2, quotes: 2}),
          many: newTaskKinds({conversations: 1, quotes: 0, renewals: 3}, {conversations: 3, quotes: 1, renewals: 1, total: 5}),
          labels: [taskAlertLabel("quotes", 1), taskAlertLabel("quotes", 3), taskAlertLabel("conversations", 2),
                   taskAlertLabel("renewals", 2)],
        };
        console.log(JSON.stringify(out));
        """,
    ])
    result = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)
    assert result["first"] == []
    assert result["one"] == [["conversations", 1]]
    assert result["many"] == [["conversations", 2], ["quotes", 1]]  # une baisse (échéances traitées) ne prévient pas
    assert result["labels"] == ["Nouvelle demande de cotation", "3 nouvelles demandes de cotation",
                                "2 clients attendent votre réponse", "2 échéances de contrat à préparer"]


def test_nothing_used_at_startup_is_declared_too_late():
    """
    Incident du 09/10 : une page rechargée déjà connectée lance enterApp() pendant la lecture du script, AVANT
    les « const » écrits plus bas → « Cannot access … before initialization », plus aucune vérification des tâches.
    Rien de ce que le démarrage utilise tout de suite ne doit être un const / let déclaré après cet appel.
    """
    script = HTML[HTML.index("<script>"):]
    startup = script.index("} else if (token) {\n  enterApp();")
    late = {m.group(1) for m in re.finditer(r"^(?:const|let) (\w+)", script[startup:], re.M)}
    synchronous = "".join(_function(name) for name in ("enterApp", "startTaskCounts", "stopTaskCounts", "closeTaskAlert",
                                                       "refreshSoundCard", "soundEnabled"))
    used = {name for name in late if re.search(rf"\b{name}\b", synchronous)}
    assert used == set(), used
