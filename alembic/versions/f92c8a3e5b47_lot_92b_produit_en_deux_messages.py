"""Lot 92b — produit ajouté en deux messages (photo puis nom et prix, ou l'inverse) : réponse attendue plus longue.

Revision ID: f92c8a3e5b47
Revises: e92b4c7d1f36
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'f92c8a3e5b47'
down_revision = 'e92b4c7d1f36'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('naomy_connections', 'awaiting', existing_type=sa.String(length=80), type_=sa.String(length=400),
                    existing_nullable=True)


def downgrade() -> None:
    op.execute("UPDATE naomy_connections SET awaiting = NULL, awaiting_until = NULL WHERE length(awaiting) > 80")
    op.alter_column('naomy_connections', 'awaiting', existing_type=sa.String(length=400), type_=sa.String(length=80),
                    existing_nullable=True)
