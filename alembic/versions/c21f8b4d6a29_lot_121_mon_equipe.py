"""Lot 121 — « Mon équipe » : choix Bob seul / Bob + Naomy, essai équipe. Boutiques déjà reliées à Naomy : équipe.

Revision ID: c21f8b4d6a29
Revises: c23a7d2e5f81
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = 'c21f8b4d6a29'
down_revision = 'c23a7d2e5f81'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('team_mode', sa.String(length=8), nullable=True))
    op.add_column('tenants', sa.Column('team_trial_at', sa.DateTime(timezone=True), nullable=True))
    # Bascule automatique : une boutique déjà reliée à Naomy (publications) ou créée depuis Naomy est en équipe.
    op.execute("""
        UPDATE tenants SET team_mode = 'TEAM'
        WHERE created_via = 'NAOMY'
           OR id IN (SELECT tenant_id FROM naomy_connections WHERE status = 'ACTIVE' AND scope = 'complete')
    """)
    op.execute("UPDATE tenants SET team_trial_at = created_at WHERE created_via = 'NAOMY'")


def downgrade() -> None:
    op.drop_column('tenants', 'team_trial_at')
    op.drop_column('tenants', 'team_mode')
