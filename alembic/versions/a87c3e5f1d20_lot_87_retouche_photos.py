"""Lot 87 — Bob retouche les photos : fond blanc studio, photo portée, montage ; journal des retouches.

Revision ID: a87c3e5f1d20
Revises: f73a1c2e4b90
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'a87c3e5f1d20'
down_revision = 'f73a1c2e4b90'
branch_labels = None
depends_on = None

COLUMNS = [
    ('image_original_url', sa.String(length=1000)), ('image_studio_url', sa.String(length=1000)),
    ('image_status', sa.String(length=16)), ('worn_status', sa.String(length=16)),
    ('worn_preview_url', sa.String(length=1000)), ('worn_image_url', sa.String(length=1000)),
    ('card_style', sa.String(length=16)),
]


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column('products', sa.Column(name, kind, nullable=True))
    op.create_table(
        'image_jobs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('product_id', sa.Uuid(), nullable=True),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('engine', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('external_id', sa.String(length=128), nullable=True),
        sa.Column('cost_usd', sa.Numeric(8, 4), nullable=False, server_default='0'),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_image_jobs_tenant_id', 'image_jobs', ['tenant_id'])
    op.create_index('ix_image_jobs_product_id', 'image_jobs', ['product_id'])
    op.create_index('ix_image_jobs_created_at', 'image_jobs', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_image_jobs_created_at', table_name='image_jobs')
    op.drop_index('ix_image_jobs_product_id', table_name='image_jobs')
    op.drop_index('ix_image_jobs_tenant_id', table_name='image_jobs')
    op.drop_table('image_jobs')
    for name, _ in reversed(COLUMNS):
        op.drop_column('products', name)
