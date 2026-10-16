"""lot 78 fiches produits par categorie (commerce)

Revision ID: e78a3c5f1b20
Revises: b52d7f1a9c30
Create Date: 2026-10-16 16:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e78a3c5f1b20'
down_revision = 'b52d7f1a9c30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('products', sa.Column('bob_category', sa.String(length=24), nullable=True))
    op.add_column('products', sa.Column('sheet', sa.JSON(), nullable=True))
    op.add_column('products', sa.Column('variants', sa.JSON(), nullable=True))
    op.add_column('tenants', sa.Column('default_bob_category', sa.String(length=24), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'default_bob_category')
    op.drop_column('products', 'variants')
    op.drop_column('products', 'sheet')
    op.drop_column('products', 'bob_category')
