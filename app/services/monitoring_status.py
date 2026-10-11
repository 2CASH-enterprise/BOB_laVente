"""
Lot 67 — état du service (voyants du monitoring) : API, base de données, Redis, tâches automatiques (dernier
passage de chacune), envoi d'emails (SMTP), Gmail de prospection, IA, jetons WhatsApp / Meta.
Vert (ok), orange (warn), rouge (ko), gris (idle : rien à signaler encore). Aucun secret n'est lu ni renvoyé :
seulement « configuré ou non », des heures et des comptes.
"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.services import monitoring

TEST_TIMEOUT = 8


def _ago(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "jamais"
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "à l'instant"
    if seconds < 3600:
        return f"il y a {seconds // 60} min"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"il y a {hours} h" + (f" {minutes:02d}" if minutes else "")
    return f"il y a {seconds // 86400} j"


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return monitoring._utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def task_interval_minutes(schedule) -> int:
    """Intervalle attendu entre deux passages d'une tâche programmée (crontab)."""
    hours = getattr(schedule, "hour", set(range(24)))
    minutes = getattr(schedule, "minute", {0})
    if len(hours) < 24:
        return 1440 // max(1, len(hours))
    return max(1, 60 // max(1, len(minutes)))


def tasks_status(now: datetime, runs: dict | None = None) -> list[dict]:
    from app.workers.celery_app import celery_app

    runs = monitoring.read_task_runs() if runs is None else runs
    out = []
    for entry in celery_app.conf.beat_schedule.values():
        name = entry["task"]
        every = task_interval_minutes(entry["schedule"])
        run = runs.get(name) or {}
        ended = _parse(run.get("ended"))
        state = run.get("state")
        if ended is None:
            status = "idle"
        elif state == "FAILURE":
            status = "ko"
        elif now - ended > timedelta(minutes=every * 2 + 5):
            status = "warn"
        else:
            status = "ok"
        out.append({"task": name, "label": monitoring.TASK_LABELS.get(name, name), "every_minutes": every,
                    "last_run": ended.isoformat() if ended else None, "last_run_ago": _ago(ended, now) if ended else "pas encore vue",
                    "state": state, "status": status})
    return out


def _check(key, name, status, detail, test=False) -> dict:
    return {"key": key, "name": name, "status": status, "detail": detail, "test": test}


async def checks(db, now: datetime | None = None) -> dict:
    from app.core.config import get_settings
    from app.models.llm_usage import LlmUsage
    from app.models.monitoring import MonitorIssue, MonitorOccurrence
    from app.models.prospect import ProspectionSettings
    from app.models.tenant import Tenant
    from app.models.whatsapp_account import WhatsAppAccount

    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    hour_ago, day_ago = now - timedelta(hours=1), now - timedelta(hours=24)
    out = []

    async def errors_since(source: str, since: datetime) -> int:
        return (await db.execute(select(func.count(MonitorOccurrence.id)).join(MonitorIssue, MonitorIssue.id == MonitorOccurrence.issue_id)
                                 .where(MonitorIssue.source == source, MonitorOccurrence.at >= since))).scalar_one()

    # API : elle répond (c'est elle qui sert cette page) ; erreurs 500 de la dernière heure.
    api_errors = await errors_since("API", hour_ago)
    out.append(_check("api", "API", "warn" if api_errors else "ok",
                      f"{api_errors} erreur{'s' if api_errors > 1 else ''} 500 en 1 h" if api_errors else "Répond · aucune erreur 500 en 1 h"))

    # Base de données
    start = time.monotonic()
    try:
        await db.execute(text("SELECT 1"))
        out.append(_check("db", "Base de données", "ok", f"Connectée · {round((time.monotonic() - start) * 1000)} ms"))
    except Exception:  # noqa: BLE001
        out.append(_check("db", "Base de données", "ko", "Ne répond pas"))

    # Redis
    if monitoring._queue_mode() != "redis":
        out.append(_check("redis", "Redis", "idle", "Non utilisé (file en mémoire)"))
    else:
        ping = await asyncio.to_thread(monitoring.redis_ping)
        out.append(_check("redis", "Redis", "ok" if ping is not None else "ko",
                          f"Connecté · {ping:g} ms" if ping is not None else "Injoignable : tâches et limites d'appels arrêtées"))

    # Tâches automatiques
    tasks = await asyncio.to_thread(tasks_status, now)
    seen = [t for t in tasks if t["status"] != "idle"]
    late = [t for t in tasks if t["status"] == "warn"]
    failed = [t for t in tasks if t["status"] == "ko"]
    drain = next((t for t in tasks if t["task"] == "app.workers.monitoring.drain_task"), None)
    if not seen:
        out.append(_check("tasks", "Tâches automatiques", "idle", "Aucun passage vu pour l'instant (worker et beat démarrés ?)"))
    elif drain and drain["status"] == "warn":
        out.append(_check("tasks", "Tâches automatiques", "ko", f"Plus aucun passage depuis {drain['last_run_ago'].replace('il y a ', '')} : worker ou beat arrêté ?"))
    elif failed or late:
        names = ", ".join(t["label"] for t in (failed + late)[:2])
        out.append(_check("tasks", "Tâches automatiques", "ko" if failed else "warn",
                          f"{len(failed)} en échec" * bool(failed) + (" · " if failed and late else "") + f"{len(late)} en retard" * bool(late)
                          + f" : {names}"))
    else:
        out.append(_check("tasks", "Tâches automatiques", "ok", f"{len(seen)} sur {len(seen)} passées à l'heure"))

    marks = await asyncio.to_thread(monitoring.read_marks)

    # Envoi d'emails (SMTP)
    ok_at, fail_at = _parse(marks.get("smtp_ok")), _parse(marks.get("smtp_fail"))
    if not settings.smtp_host:
        out.append(_check("smtp", "Envoi d'emails (SMTP)", "ko", "Non configuré : aucun email ne part"))
    elif fail_at and (ok_at is None or fail_at > ok_at):
        out.append(_check("smtp", "Envoi d'emails (SMTP)", "ko", f"Dernier envoi en échec {_ago(fail_at, now)}", test=True))
    elif ok_at:
        out.append(_check("smtp", "Envoi d'emails (SMTP)", "ok", f"Dernier envoi réussi {_ago(ok_at, now)}", test=True))
    else:
        out.append(_check("smtp", "Envoi d'emails (SMTP)", "idle", "Configuré · aucun envoi depuis le démarrage", test=True))

    # Gmail de prospection
    row = await db.get(ProspectionSettings, 1)
    g_ok, g_fail, inbox = _parse(marks.get("gmail_ok")), _parse(marks.get("gmail_fail")), _parse(marks.get("inbox_ok"))
    error_at = monitoring._utc(row.last_error_at) if row and row.last_error and row.last_error_at else None
    if not (settings.prospect_smtp_username and settings.prospect_smtp_password):
        out.append(_check("gmail", "Gmail de prospection", "idle", "Non configuré"))
    elif error_at and (g_ok is None or error_at > g_ok):
        out.append(_check("gmail", "Gmail de prospection", "ko", f"Erreur {_ago(error_at, now)} : {row.last_error[:80]}", test=True))
    else:
        parts = [f"dernier envoi {_ago(g_ok, now)}" if g_ok else "aucun envoi récent",
                 f"boîte lue {_ago(inbox, now)}" if inbox else "boîte pas encore lue"]
        stale = inbox is not None and now - inbox > timedelta(minutes=45)
        out.append(_check("gmail", "Gmail de prospection", "warn" if stale else ("ok" if (g_ok or inbox) else "idle"),
                          "Connecté · " + " · ".join(parts) if (g_ok or inbox) else "Configuré · " + " · ".join(parts), test=True))

    # IA
    ai_errors = await errors_since("AI", hour_ago)
    last_ai = monitoring._utc((await db.execute(select(func.max(LlmUsage.created_at)))).scalar())
    key_ok = bool(settings.mistral_api_key if settings.llm_provider == "mistral" else settings.anthropic_api_key)
    if not key_ok:
        out.append(_check("ai", "IA (Mistral)" if settings.llm_provider == "mistral" else "IA", "ko", "Clé non configurée"))
    else:
        out.append(_check("ai", "IA (Mistral)" if settings.llm_provider == "mistral" else "IA", "warn" if ai_errors else "ok",
                          (f"{ai_errors} erreur{'s' if ai_errors > 1 else ''} en 1 h · " if ai_errors else "")
                          + (f"dernière réponse {_ago(last_ai, now)}" if last_ai else "aucune réponse encore")))

    # Jetons WhatsApp / Meta : boutiques dont un envoi a été refusé pour jeton expiré ces dernières 24 h.
    accounts = (await db.execute(select(func.count(WhatsAppAccount.id)).join(Tenant, Tenant.id == WhatsAppAccount.tenant_id)
                                 .where(Tenant.is_demo.is_(False)))).scalar_one()
    token_issues = (await db.execute(select(MonitorIssue).where(
        MonitorIssue.source.in_(("SEND_WHATSAPP", "APP")), MonitorIssue.last_seen >= day_ago,
        MonitorIssue.status != "RESOLVED"))).scalars().all()
    expired, unreadable = set(), False
    for issue in token_issues:
        if issue.meta_code in monitoring.TOKEN_CODES:
            expired.update(issue.tenant_ids or ["?"])
        if "jeton chiffré illisible" in (issue.title or "").lower():
            unreadable = True
    wa_ok = _parse(marks.get("whatsapp_ok"))
    if unreadable:
        out.append(_check("whatsapp", "Jetons WhatsApp / Meta", "ko", "Jetons enregistrés illisibles (clé de chiffrement changée ?)"))
    elif expired:
        names = {str(t.id): t.name for t in (await db.execute(select(Tenant).where(Tenant.id.in_(
            [uuid.UUID(t) for t in expired if t != "?"])))).scalars()} if any(t != "?" for t in expired) else {}
        label = ", ".join(names.get(t, "boutique inconnue") for t in list(expired)[:2])
        out.append(_check("whatsapp", "Jetons WhatsApp / Meta", "ko",
                          f"{len(expired)} boutique{'s' if len(expired) > 1 else ''} : jeton expiré ({label})"))
    elif accounts:
        out.append(_check("whatsapp", "Jetons WhatsApp / Meta", "ok",
                          f"{accounts} numéro{'s' if accounts > 1 else ''} connecté{'s' if accounts > 1 else ''}"
                          + (f" · dernier envoi {_ago(wa_ok, now)}" if wa_ok else "")))
    else:
        out.append(_check("whatsapp", "Jetons WhatsApp / Meta", "idle", "Aucun numéro connecté"))

    return {"checks": out, "tasks": tasks, "checked_at": now.isoformat()}


# ---------------------------------------------------------------- « Tester » (connexion seulement, aucun email envoyé)

def _smtp_login(host: str, port: int, username: str, password: str, use_tls: bool = True) -> None:
    import smtplib

    with smtplib.SMTP(host, port, timeout=TEST_TIMEOUT) as server:
        if use_tls:
            server.starttls()
        if username:
            server.login(username, password)


def _imap_login(host: str, username: str, password: str) -> None:
    import imaplib

    with imaplib.IMAP4_SSL(host, timeout=TEST_TIMEOUT) as box:
        box.login(username, password)


def run_test(key: str, smtp=None, imap=None) -> dict:
    """Teste la connexion d'un canal. Le message d'erreur est masqué ; aucun email n'est envoyé."""
    from app.core.config import get_settings

    s = get_settings()
    smtp, imap = smtp or _smtp_login, imap or _imap_login
    try:
        if key == "smtp":
            if not s.smtp_host:
                return {"ok": False, "detail": "SMTP non configuré"}
            smtp(s.smtp_host, s.smtp_port, s.smtp_username, s.smtp_password, s.smtp_use_tls)
            return {"ok": True, "detail": "Connexion et identifiants acceptés"}
        if key == "gmail":
            if not (s.prospect_smtp_username and s.prospect_smtp_password):
                return {"ok": False, "detail": "Gmail de prospection non configuré"}
            smtp(s.prospect_smtp_host, s.prospect_smtp_port, s.prospect_smtp_username, s.prospect_smtp_password, True)
            imap(s.prospect_imap_host, s.prospect_smtp_username, s.prospect_smtp_password)
            return {"ok": True, "detail": "Envoi et lecture de la boîte acceptés"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": monitoring.mask(f"{type(exc).__name__} : {exc}")[:300]}
    return {"ok": False, "detail": "Test inconnu"}

