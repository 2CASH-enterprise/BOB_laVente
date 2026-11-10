"""Lot 119 — rôles du Super Admin (plusieurs par personne) et invitation par e-mail. Comptes existants : Propriétaire.

Revision ID: c19b4e2a7d61
Revises: c18f3a6d1b94
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'c19b4e2a7d61'
down_revision = 'c18f3a6d1b94'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('superadmin_users', sa.Column('roles', sa.JSON(), nullable=False, server_default='["OWNER"]'))
    op.alter_column('superadmin_users', 'roles', server_default=None)
    op.add_column('superadmin_users', sa.Column('invite_token_hash', sa.String(length=64), nullable=True))
    op.add_column('superadmin_users', sa.Column('invite_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('superadmin_users', sa.Column('invited_by', sa.Uuid(), nullable=True))
    op.create_unique_constraint('uq_superadmin_users_invite_token_hash', 'superadmin_users', ['invite_token_hash'])


def downgrade() -> None:
    op.drop_constraint('uq_superadmin_users_invite_token_hash', 'superadmin_users', type_='unique')
    op.drop_column('superadmin_users', 'invited_by')
    op.drop_column('superadmin_users', 'invite_expires_at')
    op.drop_column('superadmin_users', 'invite_token_hash')
    op.drop_column('superadmin_users', 'roles')
