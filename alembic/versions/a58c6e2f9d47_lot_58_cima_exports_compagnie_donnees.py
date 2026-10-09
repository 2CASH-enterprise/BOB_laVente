"""lot 58 courtier : compagnie (raison sociale, adresse, partenaires), liens tarifs et confidentialité, information donnée au client

Revision ID: a58c6e2f9d47
Revises: f57a3d8c1e94
Create Date: 2026-10-09 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a58c6e2f9d47'
down_revision = 'f57a3d8c1e94'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('insurer_legal_name', sa.String(length=150), nullable=True))
    op.add_column('tenants', sa.Column('insurer_address', sa.String(length=300), nullable=True))
    op.add_column('tenants', sa.Column('insurance_partners', sa.JSON(), nullable=True))
    op.add_column('tenants', sa.Column('tariff_url', sa.String(length=300), nullable=True))
    op.add_column('tenants', sa.Column('privacy_policy_url', sa.String(length=300), nullable=True))
    op.add_column('customers', sa.Column('privacy_notice_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'privacy_notice_at')
    op.drop_column('tenants', 'privacy_policy_url')
    op.drop_column('tenants', 'tariff_url')
    op.drop_column('tenants', 'insurance_partners')
    op.drop_column('tenants', 'insurer_address')
    op.drop_column('tenants', 'insurer_legal_name')
