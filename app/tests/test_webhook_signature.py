import hashlib
import hmac
import json

import pytest

from app.integrations.whatsapp.client import verify_whatsapp_signature


def test_valid_signature_accepted():
    secret = "app-secret-test"
    body = b'{"hello": "world"}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_whatsapp_signature(secret, body, signature) is True


def test_invalid_signature_rejected():
    secret = "app-secret-test"
    body = b'{"hello": "world"}'
    assert verify_whatsapp_signature(secret, body, "sha256=deadbeef") is False


def test_missing_signature_rejected():
    assert verify_whatsapp_signature("secret", b"body", None) is False


def test_signature_without_prefix_rejected():
    assert verify_whatsapp_signature("secret", b"body", "deadbeef") is False


def test_tampered_body_rejected():
    """Un corps modifié après signature doit être détecté."""
    secret = "app-secret-test"
    original_body = b'{"amount": 100}'
    signature = "sha256=" + hmac.new(secret.encode(), original_body, hashlib.sha256).hexdigest()

    tampered_body = b'{"amount": 999999}'
    assert verify_whatsapp_signature(secret, tampered_body, signature) is False


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature_when_secret_configured(client, db_session):
    from app.core.config import get_settings

    settings = get_settings()
    original_secret = settings.whatsapp_app_secret
    settings.whatsapp_app_secret = "configured-secret"
    try:
        response = await client.post(
            "/webhooks/whatsapp",
            content=json.dumps({"entry": []}),
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=wrong"},
        )
        assert response.status_code == 403
    finally:
        settings.whatsapp_app_secret = original_secret


@pytest.mark.asyncio
async def test_webhook_accepts_valid_signature_when_secret_configured(client, db_session):
    from app.core.config import get_settings

    settings = get_settings()
    original_secret = settings.whatsapp_app_secret
    settings.whatsapp_app_secret = "configured-secret"
    try:
        body = json.dumps({"entry": []}).encode()
        signature = "sha256=" + hmac.new(b"configured-secret", body, hashlib.sha256).hexdigest()
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
        )
        assert response.status_code == 200
    finally:
        settings.whatsapp_app_secret = original_secret


@pytest.mark.asyncio
async def test_webhook_skips_verification_when_no_secret_configured(client, db_session):
    """Comportement de développement par défaut : sans secret configuré, pas de blocage (documenté)."""
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.whatsapp_app_secret == ""  # valeur par défaut en environnement de test

    response = await client.post("/webhooks/whatsapp", json={"entry": []})
    assert response.status_code == 200
