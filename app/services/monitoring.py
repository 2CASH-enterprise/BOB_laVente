"""
Lot 67 — monitoring technique (intégré, sans service extérieur).

1. Capture : erreurs de l'API (500), tâches automatiques en échec, envois refusés (email, WhatsApp avec le code
   Meta), erreurs de l'IA et du webhook, erreurs JavaScript des pages (tableau de bord, démo, Super Admin).
   Tout passe par capture(), qui MASQUE d'abord les secrets (jetons, mots de passe, numéros, adresses) puis
   pose l'événement dans une file (Redis ; mémoire en test ou si Redis est injoignable). capture() ne lève
   jamais d'exception : surveiller ne doit jamais casser ce qui est surveillé.
2. Regroupement : ingest() vide la file chaque minute (tâche « monitoring-drain ») et range chaque événement
   dans une « erreur » (MonitorIssue) selon sa signature : même source, même endroit du code, même message
   une fois les nombres, identifiants et adresses retirés. Statuts : Nouvelle, En cours, Corrigée, et
   Revenue (réouverte toute seule si elle se reproduit après avoir été marquée corrigée).
3. Alertes : send_alerts() envoie aux Super Admins actifs UN email pour les nouvelles erreurs (et celles qui
   reviennent), au plus toutes les 10 minutes, et UN email par pic (plus de N fois en une heure, au plus
   toutes les 6 heures pour la même erreur). Jamais un email par occurrence.
4. Conservation : 30 jours (purge()).
"""
import contextvars
import hashlib
import json
import logging
import re
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

SOURCES = {
    "API": "API", "TASK": "Tâche automatique", "SEND_WHATSAPP": "Envoi WhatsApp", "SEND_EMAIL": "Email",
    "AI": "IA (Mistral)", "WEBHOOK": "Webhook WhatsApp", "BROWSER": "Navigateur", "APP": "Application",
}
STATUSES = {"NEW": "Nouvelle", "IN_PROGRESS": "En cours", "RESOLVED": "Corrigée", "REOPENED": "Revenue"}
TODO_STATUSES = ("NEW", "REOPENED", "IN_PROGRESS")
UNREAD_STATUSES = ("NEW", "REOPENED")
RETENTION_DAYS = 30
NEW_ALERT_COOLDOWN = timedelta(minutes=10)
SPIKE_REALERT = timedelta(hours=6)
MAX_TENANTS = 20
MAX_BROWSERS = 5
MAX_OCCURRENCES_PER_DRAIN = 200
DRAIN_LIMIT = 2000
QUEUE_KEY = "bob:monitor:queue"
QUEUE_MAX = 10000
TASKS_KEY = "bob:monitor:tasks"
MARKS_KEY = "bob:monitor:marks"

# Tâches automatiques : libellés lisibles (le test vérifie que chaque tâche programmée en a un).
TASK_LABELS = {
    "app.workers.opportunities.recompute_opportunities_task": "Calcul des opportunités (nuit)",
    "app.workers.catalog_sync.sync_meta_catalogs_task": "Synchronisation des catalogues Meta (nuit)",
    "app.workers.appointment_reminders.send_appointment_reminders_task": "Rappels de rendez-vous",
    "app.workers.notifications.check_all_task": "Notifications et pastille",
    "app.workers.followups.check_followups_task": "Relances par email",
    "app.workers.llm_budget.check_budgets_task": "Alertes de coût IA",
    "app.workers.billing.check_billing_task": "Échéances d'abonnement",
    "app.workers.renewals.check_renewals_task": "Échéances des contrats (courtier)",
    "app.workers.reports.check_monthly_reports_task": "Rapports mensuels",
    "app.workers.prospects.purge_prospects_task": "Effacement des anciens prospects",
    "app.workers.prospect_emails.send_prospect_email_task": "Emails de prospection",
    "app.workers.prospect_emails.read_prospect_inbox_task": "Lecture de la boîte de prospection",
    "app.workers.order_reminders.check_order_reminders_task": "Relances de commandes",
    "app.workers.monitoring.drain_task": "Monitoring : collecte des erreurs",
    "app.workers.monitoring.purge_task": "Monitoring : effacement après 30 jours",
    "app.workers.wa_templates.refresh_templates_task": "Modèles WhatsApp : statut chez Meta",
    "app.workers.plan_quota.check_quotas_task": "Plafonds des formules : alertes",
    "app.workers.coexistence_sync.retry_syncs_task": "Numéro partagé : contacts et historique du téléphone",
    "app.workers.catalog_sync.sync_shopify_task": "Catalogues Shopify (prix, stock, variantes)",
    "app.workers.property_alerts.run_alerts_task": "Alertes « nouveau bien » (agences immobilières)",
}

# « Où » lisible, d'après le module d'où part l'opération (le plus extérieur dans la pile).
WHERE_LABELS = {
    "app/api/webhooks/whatsapp.py": "Webhook WhatsApp → réponse de Bob",
    "app/api/messages/routes.py": "Réponse d'un vendeur depuis le tableau de bord",
    "app/api/campaigns/routes.py": "Campagnes de la boutique",
    "app/api/orders/routes.py": "Commandes (tableau de bord)",
    "app/api/auth/routes.py": "Connexion et inscription",
    "app/api/demo/routes.py": "Démo",
    "app/api/preregistration/routes.py": "Pré-inscription",
    "app/api/prospects/campaigns.py": "Emails de prospection (Super Admin)",
    "app/services/email_service.py": "Envoi d'email",
    "app/services/prospect_mailer.py": "Emails de prospection (Gmail)",
    "app/agents/orchestrator.py": "Réponse de Bob (IA)",
    "app/agents/classifier.py": "Lecture de l'intention du client (IA)",
    "app/services/signal_service.py": "Lecture de l'intention du client (IA)",
    "app/integrations/whatsapp/client.py": "Envoi WhatsApp",
    "app/core/crypto.py": "Lecture des jetons enregistrés",
}

# Codes d'erreur Meta (envois WhatsApp) : titre, conséquence, piste.
META_CODES = {
    "190": ("jeton d'accès expiré ou invalide", "Les clients de cette boutique ne reçoivent plus les réponses de Bob.",
            "Reconnecter WhatsApp dans Intégrations (tableau de bord de la boutique)."),
    "102": ("session Meta expirée", "Les clients de cette boutique ne reçoivent plus les réponses de Bob.",
            "Reconnecter WhatsApp dans Intégrations (tableau de bord de la boutique)."),
    "463": ("jeton d'accès expiré", "Les clients de cette boutique ne reçoivent plus les réponses de Bob.",
            "Reconnecter WhatsApp dans Intégrations (tableau de bord de la boutique)."),
    "131047": ("fenêtre de 24 h fermée", "Le message n'est pas parti : le client n'a pas écrit depuis plus de 24 h.",
               "Normal si c'est rare. Sinon, vérifier que ce message passe bien par email après 20 h (règle des 20 h)."),
    "131026": ("message impossible à délivrer", "Ce client ne reçoit pas le message (numéro sans WhatsApp ou application trop ancienne).",
               "Vérifier le numéro du client."),
    "131056": ("trop d'envois au même client", "Des messages sont retardés ou perdus.", "Espacer les envois à ce client."),
    "130429": ("trop d'envois (limite Meta)", "Des messages sont retardés ou perdus.", "Réduire les envois groupés ; ils repartiront plus tard."),
    "80007": ("trop d'envois (limite Meta)", "Des messages sont retardés ou perdus.", "Réduire les envois groupés ; ils repartiront plus tard."),
    "4": ("trop d'appels à Meta", "Des messages sont retardés ou perdus.", "Attendre quelques minutes ; vérifier qu'aucune boucle n'envoie en masse."),
    "131031": ("compte WhatsApp bloqué par Meta", "Bob ne peut plus envoyer pour cette boutique.",
               "Vérifier le compte dans le Business Manager de Meta (qualité, restrictions)."),
    "368": ("compte temporairement bloqué par Meta", "Bob ne peut plus envoyer pour cette boutique.",
            "Vérifier le compte dans le Business Manager de Meta (qualité, restrictions)."),
    "100": ("paramètre refusé par Meta", "Ce type de message ne part pas.", "Vérifier le contenu envoyé (lien d'image, texte, modèle)."),
    "131009": ("paramètre refusé par Meta", "Ce type de message ne part pas.", "Vérifier le contenu envoyé (lien d'image, texte, modèle)."),
    "131053": ("image ou fichier refusé", "La photo du produit n'est pas partie.", "Vérifier l'image (format JPG ou PNG, moins de 5 Mo, lien public)."),
    "132001": ("modèle de message introuvable", "Le message à partir d'un modèle ne part pas.", "Vérifier le nom et la langue du modèle dans Meta."),
}
TOKEN_CODES = {"190", "102", "463"}

current_tenant: contextvars.ContextVar = contextvars.ContextVar("bob_monitor_tenant", default=None)

_guard = threading.local()
_local_queue: deque = deque(maxlen=DRAIN_LIMIT)
_local_tasks: dict = {}
_local_marks: dict = {}
_redis = None
_redis_down_until = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- masquage des secrets

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_KV = re.compile(
    r"(?i)([\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|signature|code_verifier)[\w-]*)"
    r"(\s*[=:]\s*|\"\s*:\s*\"|'\s*:\s*')((?:Bearer\s+)?[^\s,;&\"'}]+)")
# Mot de passe : la valeur va jusqu'au séparateur (les mots de passe d'application Gmail contiennent des espaces).
_PWD = re.compile(r"(?i)([\w-]*(?:password|passwd|pwd)[\w-]*)(\s*[=:]\s*|\"\s*:\s*\"|'\s*:\s*')([^\n,;&\"'}•]+)")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_LONG = re.compile(r"(?<![\w-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{24,}(?![\w-])")
_EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
_PHONE = re.compile(r"(?<![\w•])\+?\d[\d  .-]{6,}\d(?![\w])")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _mask_phone(m: re.Match) -> str:
    text = m.group(0)
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8 or _DATE.match(text):
        return text
    return ("+" if text.startswith("+") else "") + "•" * (len(digits) - 2) + digits[-2:]


def mask(text) -> str:
    """Masque jetons, mots de passe, numéros de téléphone et adresses email. Les identifiants (UUID) restent lisibles."""
    if text is None:
        return ""
    text = str(text)
    kept: list[str] = []

    def keep(m: re.Match) -> str:
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    text = _UUID.sub(keep, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} ••••••••", text)
    text = _PWD.sub(lambda m: f"{m.group(1)}{m.group(2)}••••••••", text)
    text = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}••••••••", text)
    text = _LONG.sub(lambda m: m.group(0)[:4] + "••••••••", text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)}•••@{m.group(2)}", text)
    text = _PHONE.sub(_mask_phone, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


def normalize(text: str) -> str:
    """Texte de regroupement : sans identifiants, adresses ni nombres (ils changent d'une occurrence à l'autre)."""
    text = _UUID.sub("‹id›", text or "")
    text = re.sub(r"\S+@\S+", "‹email›", text)
    text = re.sub(r"[+•]*\d[\d•]*", "‹n›", text)
    text = re.sub(r"[A-Za-z0-9]{0,4}••••••••", "‹secret›", text)
    return re.sub(r"\s+", " ", text).strip().lower()[:240]


# ---------------------------------------------------------------- emplacement dans le code

def _rel(path: str) -> str | None:
    path = (path or "").replace("\\", "/")
    index = path.rfind("/app/")
    if index >= 0:
        return path[index + 1:]
    return path if path.startswith("app/") else None


def _app_frames(frames) -> list:
    out = []
    for fr in frames:
        rel = _rel(fr.filename)
        if rel and not rel.startswith("app/services/monitoring") and rel != "app/main.py":
            out.append((rel, fr.lineno, fr.name))
    return out


def _fmt(frame) -> str:
    return f"{frame[0]}:{frame[1]} ({frame[2]})"


def location_key(location: str | None) -> str:
    return re.sub(r":\d+", "", location or "")


def where_label(origin: str | None, source: str, context: str | None = None) -> str:
    if source == "API" and context:
        return f"API · {context}"
    if source == "TASK" and context:
        return f"Tâche automatique · {TASK_LABELS.get(context, context)}"
    if source == "BROWSER" and context:
        return f"Page {context}"
    if origin:
        path = origin.split(":")[0]
        if path in WHERE_LABELS:
            return WHERE_LABELS[path]
        module = path[:-3].replace("/", ".") if path.endswith(".py") else path
        for task, label in TASK_LABELS.items():
            if task.startswith(module + "."):
                return f"Tâche automatique · {label}"
        return path
    return SOURCES.get(source, source)


# ---------------------------------------------------------------- file d'attente

def _queue_mode() -> str:
    from app.core.config import get_settings

    return get_settings().monitoring_queue


def _client():
    global _redis
    if _redis is None:
        import redis  # import différé (client synchrone : utilisable depuis le journal, les tâches et l'API)

        from app.core.config import get_settings

        _redis = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=0.3, socket_timeout=0.5)
    return _redis


def _redis_ok() -> bool:
    return _queue_mode() == "redis" and time.monotonic() >= _redis_down_until


def _redis_failed() -> None:
    global _redis_down_until
    _redis_down_until = time.monotonic() + 30


def _push(event: dict) -> None:
    if _redis_ok():
        try:
            pipe = _client().pipeline()
            pipe.rpush(QUEUE_KEY, json.dumps(event, default=str))
            pipe.ltrim(QUEUE_KEY, -QUEUE_MAX, -1)
            pipe.execute()
            return
        except Exception:  # noqa: BLE001 — Redis injoignable : file en mémoire de ce processus
            _redis_failed()
    _local_queue.append(event)


def pop_events(limit: int = DRAIN_LIMIT) -> list[dict]:
    events = []
    while _local_queue and len(events) < limit:
        events.append(_local_queue.popleft())
    if _redis_ok() and len(events) < limit:
        try:
            raw = _client().lpop(QUEUE_KEY, limit - len(events)) or []
            events.extend(json.loads(item) for item in raw)
        except Exception:  # noqa: BLE001
            _redis_failed()
    return events


def mark(name: str, at: datetime | None = None) -> None:
    """Dernier succès ou échec d'un canal (smtp_ok, smtp_fail, gmail_ok, gmail_fail, inbox_ok, whatsapp_ok)."""
    value = (at or _now()).isoformat()
    try:
        if _redis_ok():
            try:
                _client().hset(MARKS_KEY, name, value)
                return
            except Exception:  # noqa: BLE001
                _redis_failed()
        _local_marks[name] = value
    except Exception:  # noqa: BLE001
        pass


def read_marks() -> dict:
    marks = dict(_local_marks)
    if _redis_ok():
        try:
            marks.update({k.decode(): v.decode() for k, v in (_client().hgetall(MARKS_KEY) or {}).items()})
        except Exception:  # noqa: BLE001
            _redis_failed()
    return marks


def record_task_run(task: str, state: str, started: datetime | None, ended: datetime | None = None) -> None:
    value = {"state": state, "started": started.isoformat() if started else None,
             "ended": (ended or _now()).isoformat()}
    try:
        if _redis_ok():
            try:
                _client().hset(TASKS_KEY, task, json.dumps(value))
                return
            except Exception:  # noqa: BLE001
                _redis_failed()
        _local_tasks[task] = value
    except Exception:  # noqa: BLE001
        pass


def read_task_runs() -> dict:
    runs = dict(_local_tasks)
    if _redis_ok():
        try:
            runs.update({k.decode(): json.loads(v) for k, v in (_client().hgetall(TASKS_KEY) or {}).items()})
        except Exception:  # noqa: BLE001
            _redis_failed()
    return runs


def redis_ping() -> float | None:
    """Temps de réponse de Redis en ms (None si injoignable). Ignore la pause de 30 s : c'est un vrai test."""
    start = time.monotonic()
    try:
        _client().ping()
    except Exception:  # noqa: BLE001
        return None
    return round((time.monotonic() - start) * 1000, 1)


# ---------------------------------------------------------------- capture

def signature(source: str, exc_type: str | None, location: str | None, context: str | None, text: str,
              meta_code: str | None = None, origin: str | None = None) -> str:
    parts = [source, exc_type or "", location_key(location), location_key(origin), context or "", meta_code or "", normalize(text)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def capture(source: str, *, title: str, message: str = "", exc_type: str | None = None, location: str | None = None,
            origin: str | None = None, context: str | None = None, trace: str | None = None, meta_code: str | None = None,
            tenant_id=None, refs=(), browser: str | None = None, sig_text: str | None = None,
            at: datetime | None = None) -> dict | None:
    """Masque puis met l'événement en file. Ne lève jamais d'exception."""
    try:
        title_m = mask(title)[:300]
        message_m = mask(message)[:1000]
        event = {
            "source": source if source in SOURCES else "APP",
            "title": title_m, "message": message_m, "exc_type": (exc_type or None) and str(exc_type)[:120],
            "location": mask(location)[:300] if location else None, "origin": origin[:300] if origin else None,
            "context": mask(context)[:300] if context else None,
            "trace": mask(trace)[-6000:] if trace else None, "meta_code": meta_code,
            "tenant_id": str(tenant_id) if tenant_id else (str(current_tenant.get()) if current_tenant.get() else None),
            "refs": [str(r)[:64] for r in refs][:10], "browser": browser[:60] if browser else None,
            "at": (at or _now()).isoformat(),
        }
        event["signature"] = signature(event["source"], event["exc_type"], event["location"], event["context"],
                                       mask(sig_text) if sig_text is not None else title_m, meta_code, origin)
        _push(event)
        return event
    except Exception:  # noqa: BLE001 — surveiller ne doit jamais casser ce qui est surveillé
        return None


_FAILURE = re.compile(r"(?i)impossible|échec|échou|refus|illisible")
ALWAYS_CAPTURED = {"app.services.prospect_mailer", "app.integrations.whatsapp.client"}
_SKIP = re.compile(r"(?i)\[SMTP non configuré\]|nouvel essai")


def should_capture(record: logging.LogRecord) -> bool:
    if not record.name.startswith("app.") or record.name.startswith("app.services.monitoring"):
        return False
    template = str(record.msg)
    if _SKIP.search(template):
        return False
    exc = record.exc_info[1] if record.exc_info else None
    # Un envoi WhatsApp refusé est déjà remonté par le client WhatsApp (avec son code Meta et toute la pile).
    if exc is not None and type(exc).__name__ == "WhatsAppSendError" and record.name != "app.integrations.whatsapp.client":
        return False
    if record.args and "WhatsAppSendError" in [str(a) for a in (record.args if isinstance(record.args, tuple) else ())]:
        return False
    if record.levelno >= logging.ERROR:
        return True
    return record.levelno >= logging.WARNING and (exc is not None or bool(_FAILURE.search(template))
                                                  or record.name in ALWAYS_CAPTURED)


def source_for(record: logging.LogRecord) -> str:
    name, template = record.name, str(record.msg).lower()
    if name.startswith("app.integrations.whatsapp") or "whatsapp" in template:
        return "SEND_WHATSAPP"
    if name.startswith("app.agents") or name == "app.services.signal_service":
        return "AI"
    if name in ("app.services.email_service", "app.services.prospect_mailer") or "email" in template:
        return "SEND_EMAIL"
    if name.startswith("app.api.webhooks"):
        return "WEBHOOK"
    if name.startswith("app.workers"):
        return "TASK"
    return "APP"


def _refs(args) -> list[str]:
    values = args.values() if isinstance(args, dict) else (args or ())
    refs = []
    for value in values:
        text = str(value)
        if _UUID.fullmatch(text) or re.fullmatch(r"\d{8,20}", text):
            refs.append(text)
    return refs


def capture_log_record(record: logging.LogRecord) -> dict | None:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001
        message = str(record.msg)
    source = source_for(record)
    stack = _app_frames(traceback.extract_stack())
    origin = _fmt(stack[0]) if stack else None
    exc_type = trace = None
    location = f"{_rel(record.pathname) or record.pathname}:{record.lineno} ({record.funcName})"
    if record.exc_info and record.exc_info[1] is not None:
        etype, exc, tb = record.exc_info
        exc_type = etype.__name__
        deepest = _app_frames(traceback.extract_tb(tb))
        if deepest:
            location = _fmt(deepest[-1])
        trace = "".join(traceback.format_exception(etype, exc, tb)[-40:])
        message = f"{message}\n{exc_type} : {exc}"
    else:
        trace = message.splitlines()[0] + "\n" + "\n".join(f"  {f[0]}:{f[1]}  {f[2]}" for f in stack[-8:])
    meta = re.search(r"\bcode (\d+)", message) if source == "SEND_WHATSAPP" else None
    title = message.splitlines()[0]
    if meta:
        label = META_CODES.get(meta.group(1), (None,))[0]
        title = f"Meta refuse le message : {label} (code {meta.group(1)})" if label else f"Meta refuse le message (code {meta.group(1)})"
    return capture(source, title=title, message=message, exc_type=exc_type, location=location, origin=origin,
                   trace=trace, meta_code=meta.group(1) if meta else None, refs=_refs(record.args),
                   sig_text=record.getMessage().splitlines()[0] if not meta else title)


class MonitorHandler(logging.Handler):
    """Branché sur le journal « app » : chaque erreur (et chaque échec signalé en avertissement) part au monitoring."""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, "active", False):
            return
        _guard.active = True
        try:
            if should_capture(record):
                capture_log_record(record)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _guard.active = False


def install() -> None:
    """Branche le monitoring sur le journal de l'application (une seule fois par processus)."""
    app_logger = logging.getLogger("app")
    if not any(isinstance(h, MonitorHandler) for h in app_logger.handlers):
        app_logger.addHandler(MonitorHandler())


def capture_api_exception(request, exc: BaseException) -> dict | None:
    try:
        route = getattr(request.scope.get("route"), "path", None) or request.url.path
        context = f"{request.method} {route}"
        frames = _app_frames(traceback.extract_tb(exc.__traceback__))
        tenant = getattr(request.state, "monitor_tenant", None)
        return capture("API", title=f"Erreur 500 sur {context}", message=f"{type(exc).__name__} : {exc}",
                       exc_type=type(exc).__name__, location=_fmt(frames[-1]) if frames else None,
                       origin=_fmt(frames[0]) if frames else None, context=context, tenant_id=tenant,
                       trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-40:]))
    except Exception:  # noqa: BLE001
        return None


def capture_task_failure(task_name: str, exc: BaseException, tb=None) -> dict | None:
    try:
        tb = tb or exc.__traceback__
        frames = _app_frames(traceback.extract_tb(tb)) if tb else []
        label = TASK_LABELS.get(task_name, task_name)
        return capture("TASK", title=f"Tâche « {label} » en échec : {type(exc).__name__}", message=f"{type(exc).__name__} : {exc}",
                       exc_type=type(exc).__name__, location=_fmt(frames[-1]) if frames else None,
                       origin=_fmt(frames[0]) if frames else None, context=task_name,
                       trace="".join(traceback.format_exception(type(exc), exc, tb)[-40:]))
    except Exception:  # noqa: BLE001
        return None


BROWSER_PAGES = {"dashboard": "Tableau de bord", "demo": "Démo", "superadmin": "Super Admin", "landing": "Présentation"}
_BROWSER_IGNORED = re.compile(r"(?i)^script error\.?$|ResizeObserver loop|Failed to fetch|NetworkError|Load failed|AbortError|"
                              r"The operation was aborted|Network request failed")


def browser_name(user_agent: str) -> str:
    ua = user_agent or ""
    device = "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else \
        "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else ""
    name = "Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Samsung Internet" if "SamsungBrowser" in ua else \
        "Chrome" if ("Chrome/" in ua or "CriOS" in ua) else "Firefox" if ("Firefox/" in ua or "FxiOS" in ua) else \
        "Safari" if "Safari/" in ua else "Autre"
    return f"{name} {device}".strip()


def _js_function(stack: str) -> str | None:
    for line in (stack or "").splitlines():
        m = re.search(r"at ([\w$.<>]+) \(", line) or re.match(r"\s*([\w$.<>]+)@", line)
        if m and m.group(1) not in ("Object.<anonymous>", "<anonymous>"):
            return m.group(1)
    return None


def capture_browser(payload: dict, user_agent: str = "", tenant_id=None) -> dict | None:
    """Erreur JavaScript remontée par une page. Ignore le bruit (réseau coupé, extensions, erreurs sans détail)."""
    page = payload.get("page") if payload.get("page") in BROWSER_PAGES else None
    message = str(payload.get("message") or "")[:500]
    source = str(payload.get("source") or "")
    if not page or not message or _BROWSER_IGNORED.search(message.strip()):
        return None
    if source and not re.match(r"^https?://", source):  # extension du navigateur (chrome-extension://…)
        return None
    stack = str(payload.get("stack") or "")[:3000]
    if re.search(r"(?i)(chrome|moz|safari)-extension://", stack):
        return None
    function = _js_function(stack)
    label = BROWSER_PAGES[page]
    location = f"{page}/index.html" + (f" · {function}" if function else f":{int(payload.get('line') or 0)}")
    return capture("BROWSER", title=f"{message.splitlines()[0][:200]} ({label})", message=message,
                   exc_type=(message.split(":")[0][:60] if ":" in message[:60] else None), location=location,
                   context=label, trace=stack or None, tenant_id=tenant_id, browser=browser_name(user_agent),
                   sig_text=message.splitlines()[0])


# ---------------------------------------------------------------- explication (Où / Conséquence / Piste)

def explain(issue) -> dict:
    source, code = issue.source, issue.meta_code
    text = f"{issue.title} {issue.exc_type or ''} {issue.last_message or ''}".lower()
    origin_where = where_label(None, source, issue.context) if source in ("API", "TASK", "BROWSER") else None
    where = origin_where or where_label(issue.location, source)
    consequence, hint = "Une opération de Bob a échoué.", "Lire la trace ci-dessous : elle montre la ligne en cause."
    if source == "SEND_WHATSAPP":
        if code in META_CODES:
            _, consequence, hint = META_CODES[code]
        else:
            consequence = "Un message WhatsApp n'est pas parti."
            hint = "Lire le code Meta dans la trace (liste des codes d'erreur WhatsApp sur developers.facebook.com)."
    elif source == "SEND_EMAIL":
        prospect = "prospect" in (issue.location or "") or "prospection" in text
        if "authentication" in text or "535" in text:
            consequence = "Les emails ne partent plus : identifiants refusés."
            hint = ("Vérifier PROSPECT_SMTP_USERNAME et le mot de passe d'application Gmail dans le .env du serveur (sans les coller ici)."
                    if prospect else "Vérifier SMTP_USERNAME et SMTP_PASSWORD dans le .env du serveur (sans les coller ici).")
        elif "timeout" in text or "timed out" in text or "connection" in text or "délai" in text:
            consequence = "Le serveur d'envoi d'emails ne répond pas : les emails sont retardés ou perdus."
            hint = "Vérifier l'hôte et le port SMTP, puis « Tester » dans l'état du service."
        else:
            consequence = "Un email n'est pas parti."
            hint = "Lire la trace ; « Tester » l'envoi dans l'état du service."
    elif source == "AI":
        if "timeout" in text or "timed out" in text or "délai" in text:
            consequence = "Bob a répondu lentement ou avec un message de secours."
        elif "429" in text or "rate" in text or "quota" in text:
            consequence = "Limite de l'IA atteinte : Bob a répondu avec un message de secours."
        else:
            consequence = "Bob n'a pas pu rédiger sa réponse : le client a reçu un message de secours."
        hint = "Vérifier l'état de Mistral (console Mistral : clé, quota, incidents)."
    elif source == "TASK":
        consequence = "Le traitement automatique ne s'est pas fait jusqu'au bout (les autres boutiques continuent)."
        hint = "Lire la trace ; une fois corrigé, la tâche repasse toute seule à l'heure prévue."
    elif source == "API":
        consequence = "Une action du tableau de bord affiche « Erreur interne »."
        hint = f"Refaire l'action ({issue.context}) ; la trace montre la ligne en cause."
    elif source == "BROWSER":
        consequence = f"La page « {issue.context} » peut ne plus réagir pour l'utilisateur."
        browsers = ", ".join(issue.browsers or [])
        hint = "Ouvrir la page" + (f" sur le même navigateur ({browsers})" if browsers else "") + " ; la trace montre la fonction en cause."
    elif source == "WEBHOOK":
        consequence = "Un message reçu d'un client n'a pas été traité jusqu'au bout."
        hint = "Lire la trace ; vérifier la conversation dans le tableau de bord de la boutique."
    if "jeton chiffré illisible" in text:
        consequence = "Les jetons enregistrés (WhatsApp, catalogue) ne sont plus lisibles : les envois échouent."
        hint = "La clé de chiffrement ou SECRET_KEY a changé : remettre l'ancienne valeur (ne jamais changer SECRET_KEY)."
    return {"where": where, "consequence": consequence, "hint": hint}


# ---------------------------------------------------------------- regroupement en base

def _parse_at(value) -> datetime:
    try:
        return _utc(datetime.fromisoformat(value)) if isinstance(value, str) else (_utc(value) or _now())
    except ValueError:
        return _now()


async def _resolve_tenants(db, events: list[dict]) -> dict:
    """Identifiants trouvés dans les messages → boutique (UUID d'une boutique, ou numéro WhatsApp Meta d'une boutique)."""
    from app.models.tenant import Tenant
    from app.models.whatsapp_account import WhatsAppAccount

    uuids, digits = set(), set()
    for ev in events:
        for ref in ev.get("refs") or []:
            if _UUID.fullmatch(ref):
                uuids.add(uuid.UUID(ref))
            else:
                digits.add(ref)
    found: dict = {}
    if uuids:
        for tid in (await db.execute(select(Tenant.id).where(Tenant.id.in_(uuids)))).scalars():
            found[str(tid)] = str(tid)
    if digits:
        for pid, tid in (await db.execute(select(WhatsAppAccount.phone_number_id, WhatsAppAccount.tenant_id)
                                          .where(WhatsAppAccount.phone_number_id.in_(digits)))).all():
            found[pid] = str(tid)
    return found


def _tenant_of(ev: dict, found: dict) -> str | None:
    if ev.get("tenant_id"):
        return ev["tenant_id"]
    for ref in ev.get("refs") or []:
        if ref in found:
            return found[ref]
    return None


async def ingest(db, events: list[dict], now: datetime | None = None) -> dict:
    from sqlalchemy.exc import IntegrityError

    from app.models.monitoring import MonitorIssue, MonitorOccurrence

    now = now or _now()
    report = {"events": len(events), "new": 0, "reopened": 0}
    if not events:
        return report
    found = await _resolve_tenants(db, events)
    groups: dict[str, list[dict]] = {}
    for ev in events:
        if ev.get("signature"):
            groups.setdefault(ev["signature"], []).append(ev)
    for sig, evs in groups.items():
        evs.sort(key=lambda e: _parse_at(e.get("at")))
        first, last = evs[0], evs[-1]
        issue = (await db.execute(select(MonitorIssue).where(MonitorIssue.signature == sig))).scalar_one_or_none()
        if issue is None:
            try:
                async with db.begin_nested():
                    issue = MonitorIssue(signature=sig, source=first["source"], title=first["title"] or "Erreur",
                                         exc_type=first.get("exc_type"), context=first.get("context"),
                                         meta_code=first.get("meta_code"), status="NEW", count=0,
                                         first_seen=_parse_at(first.get("at")), last_seen=_parse_at(first.get("at")),
                                         tenant_ids=[], browsers=[])
                    db.add(issue)
                report["new"] += 1
            except IntegrityError:  # créée entre-temps par une autre collecte
                issue = (await db.execute(select(MonitorIssue).where(MonitorIssue.signature == sig))).scalar_one()
        last_at = _parse_at(last.get("at"))
        if issue.status == "RESOLVED" and last_at > (_utc(issue.resolved_at) or _utc(issue.first_seen)):
            issue.status, issue.status_changed_at, issue.alerted_at = "REOPENED", now, None
            issue.reopened_count = (issue.reopened_count or 0) + 1
            report["reopened"] += 1
        issue.count = (issue.count or 0) + len(evs)
        if _utc(issue.last_seen) is None or last_at >= _utc(issue.last_seen):
            issue.last_seen = last_at
            issue.last_message = last.get("message") or issue.last_message
            issue.location = last.get("location") or issue.location
        with_trace = [e for e in evs if e.get("trace")]
        if with_trace:
            issue.last_trace = with_trace[-1]["trace"]
        tenants = list(issue.tenant_ids or [])
        browsers = list(issue.browsers or [])
        for ev in evs:
            tid = _tenant_of(ev, found)
            if tid and tid not in tenants and len(tenants) < MAX_TENANTS:
                tenants.append(tid)
            if ev.get("browser") and ev["browser"] not in browsers and len(browsers) < MAX_BROWSERS:
                browsers.append(ev["browser"])
        issue.tenant_ids, issue.browsers = tenants, browsers
        for ev in evs[-MAX_OCCURRENCES_PER_DRAIN:]:
            tid = _tenant_of(ev, found)
            db.add(MonitorOccurrence(issue_id=issue.id, at=_parse_at(ev.get("at")), tenant_id=uuid.UUID(tid) if tid else None,
                                     message=(ev.get("message") or "")[:500], context=ev.get("context")))
    await db.commit()
    return report


async def drain(db, now: datetime | None = None) -> dict:
    return await ingest(db, pop_events(), now)


# ---------------------------------------------------------------- alertes par email

def _fr_time(value: datetime) -> str:
    return _utc(value).strftime("%d/%m à %H h %M") + " (UTC)"


def _issue_block(issue, names: dict) -> str:
    info = explain(issue)
    shops = [names.get(t, "?") for t in (issue.tenant_ids or [])]
    back = " — revenue après avoir été corrigée" if issue.status == "REOPENED" else ""
    return (f"• [{SOURCES.get(issue.source, issue.source)}] {issue.title}{back}\n"
            f"  Où : {info['where']}\n  Conséquence : {info['consequence']}\n  Piste : {info['hint']}\n"
            f"  {issue.count} fois, dernière le {_fr_time(issue.last_seen)}"
            + (f" · boutique{'s' if len(shops) > 1 else ''} : {', '.join(shops[:5])}" if shops else "") + "\n")


async def send_alerts(db, now: datetime | None = None, send=None) -> dict:
    from app.core.config import get_settings
    from app.models.monitoring import MonitorIssue, MonitorOccurrence
    from app.models.superadmin_user import SuperAdminUser
    from app.models.tenant import Tenant

    if send is None:
        from app.services.email_service import send_email as send
    now = now or _now()
    settings = get_settings()
    link = f"{settings.public_base_url.rstrip('/')}/superadmin/#monitoring"
    admins = (await db.execute(select(SuperAdminUser.email).where(SuperAdminUser.active.is_(True)))).scalars().all()
    names = {str(t.id): t.name for t in (await db.execute(select(Tenant))).scalars()}
    report = {"new": 0, "spikes": 0, "emails": 0}

    pending = (await db.execute(select(MonitorIssue).where(MonitorIssue.alerted_at.is_(None), MonitorIssue.status.in_(UNREAD_STATUSES))
                                .order_by(MonitorIssue.first_seen))).scalars().all()
    last_alert = (await db.execute(select(func.max(MonitorIssue.alerted_at)))).scalar()
    if pending and (last_alert is None or now - _utc(last_alert) >= NEW_ALERT_COOLDOWN):
        one = len(pending) == 1
        subject = (f"Bob — nouvelle erreur technique : {pending[0].title[:90]}" if one
                   else f"Bob — {len(pending)} nouvelles erreurs techniques")
        body = ("Bonjour,\n\nLe monitoring de Bob a repéré " + ("une nouvelle erreur" if one else f"{len(pending)} nouvelles erreurs")
                + " :\n\n" + "\n".join(_issue_block(i, names) for i in pending[:15])
                + (f"\n… et {len(pending) - 15} autres.\n" if len(pending) > 15 else "")
                + f"\nDétail et trace (secrets masqués) : {link}\n\n— Bob AI")
        for email in admins:
            send(to=email, subject=subject, body=body)
            report["emails"] += 1
        for issue in pending:
            issue.alerted_at = now
        report["new"] = len(pending)

    threshold = settings.monitoring_spike_per_hour
    hour_ago = now - timedelta(hours=1)
    counts = dict((await db.execute(select(MonitorOccurrence.issue_id, func.count()).where(MonitorOccurrence.at >= hour_ago)
                                    .group_by(MonitorOccurrence.issue_id))).all())
    spikes = []
    for issue_id, n in counts.items():
        if n < threshold:
            continue
        issue = await db.get(MonitorIssue, issue_id)
        if issue is None or (issue.spike_alerted_at and now - _utc(issue.spike_alerted_at) < SPIKE_REALERT):
            continue
        spikes.append((issue, n))
    if spikes:
        spikes.sort(key=lambda item: item[1], reverse=True)
        subject = f"Bob — pic d'erreurs : {spikes[0][0].title[:80]} ({spikes[0][1]} fois en 1 h)"
        body = ("Bonjour,\n\nUne erreur se répète beaucoup en ce moment (plus de "
                f"{threshold} fois en une heure) :\n\n"
                + "\n".join(f"{n} fois en 1 h —\n" + _issue_block(issue, names) for issue, n in spikes)
                + f"\nDétail et trace (secrets masqués) : {link}\n\n— Bob AI")
        for email in admins:
            send(to=email, subject=subject, body=body)
            report["emails"] += 1
        for issue, _ in spikes:
            issue.spike_alerted_at = now
        report["spikes"] = len(spikes)
    await db.commit()
    return report


async def purge(db, now: datetime | None = None) -> dict:
    from app.models.monitoring import MonitorIssue, MonitorOccurrence

    limit = (now or _now()) - timedelta(days=RETENTION_DAYS)
    occ = (await db.execute(delete(MonitorOccurrence).where(MonitorOccurrence.at < limit))).rowcount
    old_ids = (await db.execute(select(MonitorIssue.id).where(MonitorIssue.last_seen < limit))).scalars().all()
    if old_ids:
        await db.execute(delete(MonitorOccurrence).where(MonitorOccurrence.issue_id.in_(old_ids)))
        await db.execute(delete(MonitorIssue).where(MonitorIssue.id.in_(old_ids)))
    await db.commit()
    return {"occurrences": occ or 0, "issues": len(old_ids)}
