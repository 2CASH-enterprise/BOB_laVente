"""Lot 73 — démos effacées après 30 jours : le comptage par lien de publication est gardé (tenant_id vide).

Revision ID: f73a1c2e4b90
Revises: e83a2c4d6f71
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = 'f73a1c2e4b90'
down_revision = 'e83a2c4d6f71'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('ad_link_events_tenant_id_fkey', 'ad_link_events', type_='foreignkey')
    op.alter_column('ad_link_events', 'tenant_id', existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key('ad_link_events_tenant_id_fkey', 'ad_link_events', 'tenants', ['tenant_id'], ['id'],
                          ondelete='SET NULL')


def downgrade() -> None:
    op.execute("DELETE FROM ad_link_events WHERE tenant_id IS NULL")
    op.drop_constraint('ad_link_events_tenant_id_fkey', 'ad_link_events', type_='foreignkey')
    op.alter_column('ad_link_events', 'tenant_id', existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key('ad_link_events_tenant_id_fkey', 'ad_link_events', 'tenants', ['tenant_id'], ['id'],
                          ondelete='CASCADE')
