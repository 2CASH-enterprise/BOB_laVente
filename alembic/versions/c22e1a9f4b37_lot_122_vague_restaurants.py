"""Lot 122 — vague de prospection importée : groupe (ordre de passage), rang dans le fichier, relation passée.

Revision ID: c22e1a9f4b37
Revises: c20d6f3b8a15
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'c22e1a9f4b37'
down_revision = 'c20d6f3b8a15'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('prospects', sa.Column('priority', sa.Integer(), nullable=True))
    op.add_column('prospects', sa.Column('import_rank', sa.Integer(), nullable=True))
    op.add_column('prospects', sa.Column('past_relation', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('prospects', 'past_relation')
    op.drop_column('prospects', 'import_rank')
    op.drop_column('prospects', 'priority')
