"""lot 82 liens de publication (Facebook, Instagram…) suivis jusqu'à la démo et au compte

Revision ID: d82f1a3b6c54
Revises: c78e9a1b5d43
Create Date: 2026-10-17 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd82f1a3b6c54'
down_revision = 'c78e9a1b5d43'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ad_links',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('code', sa.String(length=48), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('channel', sa.String(length=24), nullable=False),
        sa.Column('sector', sa.String(length=32), nullable=True),
        sa.Column('clicks', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_click_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('archived', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ad_links_code', 'ad_links', ['code'], unique=True)
    op.create_table(
        'ad_link_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('link_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['link_id'], ['ad_links.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('link_id', 'kind', 'tenant_id', name='uq_ad_link_event'),
    )
    op.create_index('ix_ad_link_events_link_id', 'ad_link_events', ['link_id'])
    op.create_index('ix_ad_link_events_tenant_id', 'ad_link_events', ['tenant_id'])


def downgrade() -> None:
    op.drop_index('ix_ad_link_events_tenant_id', table_name='ad_link_events')
    op.drop_index('ix_ad_link_events_link_id', table_name='ad_link_events')
    op.drop_table('ad_link_events')
    op.drop_index('ix_ad_links_code', table_name='ad_links')
    op.drop_table('ad_links')
