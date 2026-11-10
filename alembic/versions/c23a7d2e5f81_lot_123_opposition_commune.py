"""Lot 123 — liste d'opposition commune Bob ↔ Naomy (contrat 1.6) : type d'empreinte et origine.

Revision ID: c23a7d2e5f81
Revises: c22e1a9f4b37
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'c23a7d2e5f81'
down_revision = 'c22e1a9f4b37'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('prospect_suppressions', sa.Column('kind', sa.String(length=10), nullable=False, server_default='email'))
    op.add_column('prospect_suppressions', sa.Column('origin', sa.String(length=10), nullable=False, server_default='BOB'))
    op.create_index(op.f('ix_prospect_suppressions_created_at'), 'prospect_suppressions', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_prospect_suppressions_created_at'), table_name='prospect_suppressions')
    op.drop_column('prospect_suppressions', 'origin')
    op.drop_column('prospect_suppressions', 'kind')
