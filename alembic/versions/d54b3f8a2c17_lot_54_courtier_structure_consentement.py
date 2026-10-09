"""lot 54 courtier : structure du cabinet, agrément, réclamations, consentement horodaté

Revision ID: d54b3f8a2c17
Revises: c53a8e2f4b61
Create Date: 2026-10-09 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd54b3f8a2c17'
down_revision = 'c53a8e2f4b61'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('insurance_structure', sa.String(length=24), nullable=True))
    op.add_column('tenants', sa.Column('insurer_name', sa.String(length=150), nullable=True))
    op.add_column('tenants', sa.Column('insurance_license', sa.String(length=80), nullable=True))
    op.add_column('tenants', sa.Column('complaints_contact', sa.String(length=200), nullable=True))
    op.add_column('quote_requests', sa.Column('consent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('quote_requests', 'consent_at')
    op.drop_column('tenants', 'complaints_contact')
    op.drop_column('tenants', 'insurance_license')
    op.drop_column('tenants', 'insurer_name')
    op.drop_column('tenants', 'insurance_structure')
