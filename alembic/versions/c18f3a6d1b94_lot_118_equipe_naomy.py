"""Lot 118 — comptes Bob créés depuis Naomy (« Mon équipe ») : origine du compte, liens de connexion à usage unique.

Revision ID: c18f3a6d1b94
Revises: c16e2b7d4a83
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'c18f3a6d1b94'
down_revision = 'c16e2b7d4a83'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('created_via', sa.String(length=8), nullable=True))
    op.create_table(
        'magic_logins',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('purpose', sa.String(length=16), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index(op.f('ix_magic_logins_tenant_id'), 'magic_logins', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_magic_logins_tenant_id'), table_name='magic_logins')
    op.drop_table('magic_logins')
    op.drop_column('tenants', 'created_via')
