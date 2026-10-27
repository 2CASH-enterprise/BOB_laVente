"""Lot 90 — mode simple du tableau de bord : mode choisi par chaque utilisateur (les comptes existants gardent FULL).

Revision ID: d90a3c7e5b12
Revises: c89e5b1f7a24
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'd90a3c7e5b12'
down_revision = 'c89e5b1f7a24'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('ui_mode', sa.String(length=8), nullable=True))
    # Décision du 27/10 : les commerçants déjà inscrits gardent le tableau de bord complet (bandeau pour essayer).
    op.execute("UPDATE users SET ui_mode = 'FULL'")


def downgrade() -> None:
    op.drop_column('users', 'ui_mode')
