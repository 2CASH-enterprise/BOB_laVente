"""
Lot 87 c — (1) une photo portée sans réponse de FASHN pendant 30 min passe en échec : la photo du quota est rendue et le
commerçant peut relancer ; (2) Photoroom, le détourage sur le serveur et FASHN ont leur voyant dans « État des
services » du Super Admin (jamais les clés : seulement configuré ou non, heures, comptes, coûts, causes).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.image_job import ImageJob
from app.services import monitoring_status
from app.services import photo_studio as ps
from app.tests.test_lot87_retouche_photos import DASH, _cutter, _product, _shop, studio_env  # noqa: F401


async def _runner(image, prompt, key):
    return "pred-lent"


async def _requested(db_session, studio_env, monkeypatch, quota=1):
    monkeypatch.setattr(get_settings(), "fashn_api_key", "cle-fashn")
    shop, _ = await _shop(db_session)
    monkeypatch.setitem(ps.WORN_QUOTA, "PRO", quota)
    dress = await _product(db_session, shop, studio_env)
    assert await ps.request_worn(db_session, dress, shop, runner=_runner) == {"status": "PENDING"}
    job = (await db_session.execute(select(ImageJob).where(ImageJob.kind == "WORN", ImageJob.product_id == dress.id))).scalar_one()
    return shop, dress, job


async def _age(db_session, job, minutes):
    job.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    await db_session.commit()


@pytest.mark.asyncio
async def test_worn_photo_still_processing_waits_then_fails_after_30_minutes(db_session, studio_env, monkeypatch):
    shop, dress, job = await _requested(db_session, studio_env, monkeypatch)

    async def processing(prediction_id, key):
        return {"status": "processing"}

    await _age(db_session, job, 12)
    assert (await ps.collect_worn(db_session, poller=processing))["waiting"] == 1
    assert job.status == "PENDING" and (await ps.quotas(db_session, shop))["worn_left"] == 0

    await _age(db_session, job, 31)
    assert (await ps.collect_worn(db_session, poller=processing))["timed_out"] == 1
    await db_session.refresh(dress)
    assert job.status == "FAILED" and job.error == "FASHN n'a pas répondu en 30 min" and dress.worn_status == "FAILED"
    assert (await ps.quotas(db_session, shop))["worn_left"] == 1  # la photo du quota est rendue
    assert dress.image_url.endswith("/orig.jpg")  # la carte n'a pas bougé
    other = await _product(db_session, shop, studio_env, name="Chemise lin")
    assert await ps.request_worn(db_session, other, shop, runner=_runner) == {"status": "PENDING"}  # on peut relancer


@pytest.mark.asyncio
async def test_unreachable_fashn_also_times_out_but_a_late_result_is_kept(db_session, studio_env, monkeypatch):
    _, dress, job = await _requested(db_session, studio_env, monkeypatch)

    async def down(prediction_id, key):
        raise RuntimeError("timeout")

    await _age(db_session, job, 5)
    assert (await ps.collect_worn(db_session, poller=down))["waiting"] == 1
    await _age(db_session, job, 45)
    assert (await ps.collect_worn(db_session, poller=down))["timed_out"] == 1

    _, dress2, job2 = await _requested(db_session, studio_env, monkeypatch, quota=5)
    await _age(db_session, job2, 40)
    import base64

    from app.tests.test_lot87_retouche_photos import _photo_bytes
    worn = "data:image/jpeg;base64," + base64.b64encode(_photo_bytes((20, 60, 200), (300, 400))).decode()

    async def completed(prediction_id, key):
        return {"status": "completed", "output": [worn]}

    assert (await ps.collect_worn(db_session, poller=completed))["ready"] == 1  # terminée, même en retard : gardée
    assert job2.status == "DONE"


@pytest.mark.asyncio
async def test_photoroom_failure_is_written_on_the_server_retouch(db_session, studio_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "photoroom_api_key", "cle")
    shop, _ = await _shop(db_session)
    product = await _product(db_session, shop, studio_env)

    async def down(data, key):
        raise RuntimeError("Photoroom a répondu 402")

    assert await ps.process_white(db_session, product, shop, photoroom=down, cutter=_cutter) == "DONE"
    job = (await db_session.execute(select(ImageJob))).scalar_one()
    assert (job.engine, job.status, job.error) == ("SERVER", "DONE", "Photoroom indisponible : Photoroom a répondu 402")


_seen: dict = {}


async def _stamp(db_session, step):
    """SQLite garde les heures à la seconde : chaque nouvelle retouche reçoit une heure distincte (ordre garanti)."""
    for job in (await db_session.execute(select(ImageJob))).scalars().all():
        if job.id not in _seen:
            _seen[job.id] = step
            job.created_at = datetime.now(timezone.utc) - timedelta(minutes=10) + timedelta(seconds=step)
    await db_session.commit()


async def _checks(db_session):
    now = datetime.now(timezone.utc)
    return {c["key"]: c for c in await monitoring_status.photo_checks(db_session, now, get_settings())}


@pytest.mark.asyncio
async def test_service_lights_without_keys(db_session, studio_env, monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "photo_model_path", str(tmp_path / "absent.onnx"))
    checks = await _checks(db_session)
    assert list(checks) == ["photoroom", "cutout", "fashn"]
    assert checks["photoroom"] == {"key": "photoroom", "name": "Photoroom (fond blanc)", "status": "idle",
                                   "detail": "Non configuré : les fonds blancs sont faits sur le serveur", "test": False}
    assert checks["fashn"]["status"] == "idle" and checks["fashn"]["detail"] == "Non configuré : photo portée désactivée"
    assert checks["cutout"]["status"] == "ko" and "Modèle de détourage absent" in checks["cutout"]["detail"]

    model = tmp_path / "isnet.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(settings, "photo_model_path", str(model))
    assert (await _checks(db_session))["cutout"]["detail"] == "Prêt · aucune photo encore"


@pytest.mark.asyncio
async def test_service_lights_follow_the_real_retouches(db_session, studio_env, monkeypatch, tmp_path):
    settings = get_settings()
    model = tmp_path / "isnet.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(settings, "photo_model_path", str(model))
    shop, _ = await _shop(db_session)
    product = await _product(db_session, shop, studio_env)

    # Serveur : une réussite, puis un échec (mémoire), puis de nouveau une réussite.
    assert await ps.process_white(db_session, product, shop, cutter=_cutter) == "DONE"
    await _stamp(db_session, 1)
    cutout = (await _checks(db_session))["cutout"]
    assert cutout["status"] == "ok" and cutout["detail"] == "Dernière photo il y a 9 min · 1 ce mois-ci · gratuit"

    async def killed(data, path):
        raise RuntimeError("détourage impossible : mémoire du serveur insuffisante (processus arrêté)")

    await ps.process_white(db_session, product, shop, cutter=killed)
    await _stamp(db_session, 2)
    cutout = (await _checks(db_session))["cutout"]
    assert cutout["status"] == "ko" and "mémoire du serveur insuffisante" in cutout["detail"]
    await ps.process_white(db_session, product, shop, cutter=_cutter)
    await _stamp(db_session, 3)
    cutout = (await _checks(db_session))["cutout"]
    assert cutout["status"] == "warn" and cutout["detail"].startswith("1 échec en 24 h · dernière réussite")

    # Photoroom configuré : en panne (le serveur a pris le relais), puis de nouveau bon.
    monkeypatch.setattr(settings, "photoroom_api_key", "cle")

    async def down(data, key):
        raise RuntimeError("Photoroom a répondu 503")

    async def up(data, key):
        from app.tests.test_lot87_retouche_photos import _cutout_png
        return _cutout_png()

    await ps.process_white(db_session, product, shop, photoroom=down, cutter=_cutter)
    await _stamp(db_session, 4)
    photoroom = (await _checks(db_session))["photoroom"]
    assert photoroom["status"] == "ko" and "Photoroom a répondu 503 · le serveur prend le relais" in photoroom["detail"]
    await ps.process_white(db_session, product, shop, photoroom=up, cutter=_cutter)
    await _stamp(db_session, 5)
    photoroom = (await _checks(db_session))["photoroom"]
    assert photoroom["status"] == "ok" and photoroom["detail"].endswith("· 1 ce mois-ci · 0.02 $")

    # FASHN : une demande en cours, puis prête.
    _, dress, _ = await _requested(db_session, studio_env, monkeypatch, quota=5)
    fashn = (await _checks(db_session))["fashn"]
    assert fashn["status"] == "idle" and fashn["detail"] == "Configuré · aucune photo encore · 1 en cours"
    import base64

    from app.tests.test_lot87_retouche_photos import _photo_bytes
    worn = "data:image/jpeg;base64," + base64.b64encode(_photo_bytes((20, 60, 200), (300, 400))).decode()

    async def completed(prediction_id, key):
        return {"status": "completed", "output": [worn]}

    await ps.collect_worn(db_session, poller=completed)
    fashn = (await _checks(db_session))["fashn"]
    assert fashn["status"] == "ok" and fashn["detail"].endswith("· 1 ce mois-ci · 0.15 $")


@pytest.mark.asyncio
async def test_errors_shown_in_the_monitoring_are_masked(db_session, studio_env, monkeypatch):
    _, _, job = await _requested(db_session, studio_env, monkeypatch)
    job.status, job.error = "FAILED", "Refusé : Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    await db_session.commit()
    fashn = (await _checks(db_session))["fashn"]
    assert fashn["status"] == "ko" and "abcdefghijklmnop" not in fashn["detail"] and "••••" in fashn["detail"]


def test_dashboard_says_a_failed_worn_photo_was_not_counted():
    assert "La photo portée n'a pas pu être créée cette fois-ci. Rien n'a été décompté" in DASH
    assert 'v.worn_status === "FAILED"' in DASH
