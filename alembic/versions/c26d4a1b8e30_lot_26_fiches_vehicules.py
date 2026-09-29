"""lot 26 fiches vehicules : caracteristiques d'un vehicule sur le produit

Revision ID: c26d4a1b8e30
Revises: b25e7f3a9c20
Create Date: 2026-09-29 19:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'c26d4a1b8e30'
down_revision = 'b25e7f3a9c20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('products', sa.Column('vehicle', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('products', 'vehicle')
