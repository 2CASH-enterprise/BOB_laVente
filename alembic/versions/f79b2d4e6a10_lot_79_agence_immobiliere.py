"""lot 79 agence immobiliere (fiche projet, profil de l'agence)

Revision ID: f79b2d4e6a10
Revises: e78a3c5f1b20
Create Date: 2026-10-17 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f79b2d4e6a10'
down_revision = 'e78a3c5f1b20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('prospect_profiles', sa.Column('real_estate', sa.JSON(), nullable=True))
    op.add_column('tenants', sa.Column('real_estate_profile', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'real_estate_profile')
    op.drop_column('prospect_profiles', 'real_estate')
