"""Lot 94 — parcours à boutons sur WhatsApp (commerce) : interrupteur par boutique, actif par défaut.

Revision ID: a94d2f6b8c15
Revises: f92c8a3e5b47
Create Date: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa

revision = 'a94d2f6b8c15'
down_revision = 'f92c8a3e5b47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('guided_flow_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('tenants', 'guided_flow_enabled')
