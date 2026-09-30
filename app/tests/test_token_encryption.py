"""Lot 30 — jetons chiffrés au repos : jamais en clair dans la base, lus en clair par l'application."""
import logging

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text

from app.core import crypto
from app.models.ecommerce_connection import EcommerceConnection
from app.models.tenant import Tenant
from app.models.whatsapp_account import WhatsAppAccount

TOKEN = "EAAG-secret-system-user-token-1234567890"


@pytest.fixture(autouse=True)
def _fresh_cipher():
    crypto._cipher.cache_clear()
    yield
    crypto._cipher.cache_clear()


def test_round_trip_and_never_plain():
    stored = crypto.encrypt(TOKEN)
    assert stored.startswith("enc:v1:") and TOKEN not in stored
    assert crypto.decrypt(stored) == TOKEN
    assert crypto.encrypt(TOKEN) != stored  # chiffrement aléatoire : deux copies ne se ressemblent pas


def test_never_encrypted_twice_and_none_kept():
    stored = crypto.encrypt(TOKEN)
    assert crypto.encrypt(stored) == stored
    assert crypto.encrypt(None) is None and crypto.decrypt(None) is None


def test_old_plain_tokens_stay_readable():
    assert crypto.decrypt(TOKEN) == TOKEN


def test_wrong_key_gives_empty_token_and_logs_no_secret(monkeypatch, caplog):
    stored = crypto.encrypt(TOKEN)
    monkeypatch.setattr("app.core.config.get_settings", lambda: type("S", (), {"secret_key": "autre-cle", "token_encryption_key": None})())
    crypto._cipher.cache_clear()
    with caplog.at_level(logging.ERROR):
        assert crypto.decrypt(stored) == ""
    assert "illisible" in caplog.text and TOKEN not in caplog.text and stored not in caplog.text


def test_explicit_key_added_later_keeps_old_tokens_readable(monkeypatch):
    before = crypto.encrypt(TOKEN)  # clé dérivée de SECRET_KEY
    from app.core.config import get_settings

    real = get_settings()
    explicit = Fernet.generate_key().decode()
    monkeypatch.setattr("app.core.config.get_settings",
                        lambda: type("S", (), {"secret_key": real.secret_key, "token_encryption_key": explicit})())
    crypto._cipher.cache_clear()
    after = crypto.encrypt(TOKEN)
    assert crypto.decrypt(before) == TOKEN and crypto.decrypt(after) == TOKEN
    assert Fernet(explicit.encode()).decrypt(after[len("enc:v1:"):].encode()).decode() == TOKEN


async def _raw(db_session, table, column):
    return (await db_session.execute(text(f"SELECT {column} FROM {table}"))).scalars().all()


@pytest.mark.asyncio
async def test_whatsapp_token_is_encrypted_in_the_database(db_session, unique_email):
    tenant = Tenant(name="T", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-crypto-1", system_user_token=TOKEN))
    await db_session.commit()

    [raw] = await _raw(db_session, "whatsapp_accounts", "system_user_token")
    assert raw.startswith("enc:v1:") and TOKEN not in raw
    account = (await db_session.execute(select(WhatsAppAccount).execution_options(populate_existing=True))).scalar_one()
    assert account.system_user_token == TOKEN


@pytest.mark.asyncio
async def test_catalog_token_is_encrypted_in_the_database(db_session, unique_email):
    tenant = Tenant(name="T", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    from app.models.ecommerce_connection import EcommercePlatform

    platform = list(EcommercePlatform)[0]
    db_session.add(EcommerceConnection(tenant_id=tenant.id, platform=platform, shop_domain="cat-1", access_token=TOKEN))
    await db_session.commit()

    [raw] = await _raw(db_session, "ecommerce_connections", "access_token")
    assert raw.startswith("enc:v1:") and TOKEN not in raw
    connection = (await db_session.execute(select(EcommerceConnection).execution_options(populate_existing=True))).scalar_one()
    assert connection.access_token == TOKEN


@pytest.mark.asyncio
async def test_a_plain_token_left_by_an_old_version_is_still_used(db_session, unique_email):
    tenant = Tenant(name="T", country="SN", currency="XOF", email=unique_email)
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(WhatsAppAccount(tenant_id=tenant.id, waba_id="w", phone_number_id="pn-crypto-2", system_user_token="x"))
    await db_session.commit()
    await db_session.execute(text("UPDATE whatsapp_accounts SET system_user_token = :t"), {"t": TOKEN})
    await db_session.commit()

    account = (await db_session.execute(select(WhatsAppAccount).execution_options(populate_existing=True))).scalar_one()
    assert account.system_user_token == TOKEN


def test_migration_encrypts_then_restores():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("m30", Path("alembic/versions/a30c7e2f4b18_lot_30_chiffrement_des_jetons.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = {"whatsapp_accounts": [[1, TOKEN], [2, crypto.encrypt("deja")]], "ecommerce_connections": [[3, None]]}

    class _Bind:
        def execute(self, stmt, params=None):
            sql = str(stmt)
            table = next(t for t in rows if t in sql)
            if sql.startswith("SELECT"):
                return type("R", (), {"all": lambda self: [tuple(r) for r in rows[table]]})()
            for r in rows[table]:
                if r[0] == params["id"]:
                    r[1] = params["value"]

    module.op = type("Op", (), {"get_bind": staticmethod(lambda: _Bind())})
    already = rows["whatsapp_accounts"][1][1]
    module.upgrade()
    assert crypto.is_encrypted(rows["whatsapp_accounts"][0][1]) and rows["whatsapp_accounts"][1][1] == already
    assert rows["ecommerce_connections"][0][1] is None
    module.downgrade()
    assert [r[1] for r in rows["whatsapp_accounts"]] == [TOKEN, "deja"]
