"""lot 30 chiffrement des jetons en base

Revision ID: a30c7e2f4b18
Revises: f29b4c8e1d63
Create Date: 2026-09-30 12:20:00

Chiffre les jetons déjà enregistrés (WhatsApp, catalogue Meta, Shopify). Aucune colonne ne change :
seules les valeurs en clair sont réécrites chiffrées. Relancer la migration ne rechiffre jamais un
jeton déjà chiffré. Le retour arrière les remet en clair.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a30c7e2f4b18'
down_revision = 'f29b4c8e1d63'
branch_labels = None
depends_on = None

_COLUMNS = (("whatsapp_accounts", "system_user_token"), ("ecommerce_connections", "access_token"))


def _rewrite(transform, only_if) -> None:
    bind = op.get_bind()
    for table, column in _COLUMNS:
        rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table}")).all()
        for row_id, value in rows:
            if value is not None and only_if(value):
                bind.execute(sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
                             {"value": transform(value), "id": row_id})


def upgrade() -> None:
    from app.core.crypto import encrypt, is_encrypted

    _rewrite(encrypt, lambda v: not is_encrypted(v))


def downgrade() -> None:
    from app.core.crypto import decrypt, is_encrypted

    _rewrite(decrypt, is_encrypted)
