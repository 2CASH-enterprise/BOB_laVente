"""lot 61 : prospection (Super Admin) — prospects, lien personnel suivi, historique

Revision ID: c61f2a9b7e30
Revises: b60d3e8f1a25
Create Date: 2026-10-10 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c61f2a9b7e30'
down_revision = 'b60d3e8f1a25'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'prospects',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('contact_name', sa.String(length=200), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('phone', sa.String(length=32), nullable=True),
        sa.Column('country', sa.String(length=2), nullable=False),
        sa.Column('city', sa.String(length=120), nullable=True),
        sa.Column('sector', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=120), nullable=True),
        sa.Column('owner_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='ACTIVE', nullable=False),
        sa.Column('status_reason', sa.String(length=200), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('next_action_on', sa.Date(), nullable=True),
        sa.Column('contacted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_contact_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_click_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_click_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('click_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('demo_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('demo_tenant_id', sa.Uuid(), nullable=True),
        sa.Column('signed_up_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tenant_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['superadmin_users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['demo_tenant_id'], ['tenants.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_prospects_code'), 'prospects', ['code'], unique=True)
    op.create_index(op.f('ix_prospects_email'), 'prospects', ['email'], unique=False)
    op.create_index(op.f('ix_prospects_phone'), 'prospects', ['phone'], unique=False)
    op.create_index(op.f('ix_prospects_owner_id'), 'prospects', ['owner_id'], unique=False)
    op.create_index(op.f('ix_prospects_status'), 'prospects', ['status'], unique=False)
    op.create_index(op.f('ix_prospects_tenant_id'), 'prospects', ['tenant_id'], unique=False)
    op.create_table(
        'prospect_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('prospect_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('channel', sa.String(length=16), nullable=True),
        sa.Column('detail', sa.String(length=500), nullable=True),
        sa.Column('actor', sa.String(length=255), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['prospect_id'], ['prospects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_prospect_events_prospect_id'), 'prospect_events', ['prospect_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_prospect_events_prospect_id'), table_name='prospect_events')
    op.drop_table('prospect_events')
    for name in ('tenant_id', 'status', 'owner_id', 'phone', 'email', 'code'):
        op.drop_index(op.f(f'ix_prospects_{name}'), table_name='prospects')
    op.drop_table('prospects')
