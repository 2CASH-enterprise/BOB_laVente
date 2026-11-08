"""Lot 112 — teinte des affiches retenue par le commerçant (elle colore aussi le reçu en image).

Revision ID: c12a4e7b9d30
Revises: c08d3f5a7b21
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'c12a4e7b9d30'
down_revision = 'c08d3f5a7b21'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('poster_accent', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'poster_accent')
