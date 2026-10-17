"""lot 81 assistant de pige et ventes DVF

Revision ID: b81d4f6a8c32
Revises: a80c3e5d7f21
Create Date: 2026-10-17 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b81d4f6a8c32'
down_revision = 'a80c3e5d7f21'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('pige_listings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=False),
    sa.Column('source_url', sa.String(length=500), nullable=True),
    sa.Column('site', sa.String(length=40), nullable=True),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('transaction', sa.String(length=16), nullable=True),
    sa.Column('property_type', sa.String(length=16), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('postal_code', sa.String(length=5), nullable=True),
    sa.Column('surface', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('rooms', sa.Integer(), nullable=True),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('dpe', sa.String(length=1), nullable=True),
    sa.Column('owner_name', sa.String(length=100), nullable=True),
    sa.Column('owner_phone', sa.String(length=30), nullable=True),
    sa.Column('phone_hash', sa.String(length=64), nullable=True),
    sa.Column('no_agency', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('online_days', sa.Integer(), nullable=True),
    sa.Column('price_dropped', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('extraction', sa.String(length=8), server_default='RULES', nullable=False),
    sa.Column('analysis', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='A_APPELER', nullable=False),
    sa.Column('next_call_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pige_listings_tenant_id'), 'pige_listings', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_pige_listings_phone_hash'), 'pige_listings', ['phone_hash'], unique=False)
    op.create_table('dvf_sales',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('department', sa.String(length=3), nullable=False),
    sa.Column('postal_code', sa.String(length=5), nullable=True),
    sa.Column('commune', sa.String(length=100), nullable=True),
    sa.Column('sold_on', sa.Date(), nullable=False),
    sa.Column('property_type', sa.String(length=16), nullable=False),
    sa.Column('surface', sa.Integer(), nullable=False),
    sa.Column('rooms', sa.Integer(), nullable=True),
    sa.Column('price', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_dvf_sales_department'), 'dvf_sales', ['department'], unique=False)
    op.create_index(op.f('ix_dvf_sales_postal_code'), 'dvf_sales', ['postal_code'], unique=False)
    op.create_table('dvf_departments',
    sa.Column('code', sa.String(length=3), nullable=False),
    sa.Column('loaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sales', sa.Integer(), server_default='0', nullable=False),
    sa.Column('years', sa.String(length=60), nullable=True),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.PrimaryKeyConstraint('code')
    )


def downgrade() -> None:
    op.drop_table('dvf_departments')
    op.drop_index(op.f('ix_dvf_sales_postal_code'), table_name='dvf_sales')
    op.drop_index(op.f('ix_dvf_sales_department'), table_name='dvf_sales')
    op.drop_table('dvf_sales')
    op.drop_index(op.f('ix_pige_listings_phone_hash'), table_name='pige_listings')
    op.drop_index(op.f('ix_pige_listings_tenant_id'), table_name='pige_listings')
    op.drop_table('pige_listings')
