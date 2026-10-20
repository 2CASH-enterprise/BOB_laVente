"""Lot 88 — import de catalogue PDF : journal des imports (quotas, coût, aperçu à vérifier).

Revision ID: b88d4f2a6c13
Revises: a87c3e5f1d20
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'b88d4f2a6c13'
down_revision = 'a87c3e5f1d20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'catalog_imports',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('source', sa.String(length=12), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('filename', sa.String(length=200), nullable=True),
        sa.Column('pages', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_usd', sa.Numeric(8, 4), nullable=False, server_default='0'),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_catalog_imports_tenant_id', 'catalog_imports', ['tenant_id'])
    op.create_index('ix_catalog_imports_created_at', 'catalog_imports', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_catalog_imports_created_at', table_name='catalog_imports')
    op.drop_index('ix_catalog_imports_tenant_id', table_name='catalog_imports')
    op.drop_table('catalog_imports')
