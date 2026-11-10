"""Lot 120 — interrupteur entre les Super Admin : jetons de passage servis, membres à transmettre à Naomy.

Revision ID: c20d6f3b8a15
Revises: c19b4e2a7d61
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'c20d6f3b8a15'
down_revision = 'c19b4e2a7d61'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'console_jumps',
        sa.Column('jti', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('jti'),
    )
    op.create_index(op.f('ix_console_jumps_expires_at'), 'console_jumps', ['expires_at'], unique=False)
    op.create_table(
        'console_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('agent', sa.String(length=16), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_console_events_status'), 'console_events', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_console_events_status'), table_name='console_events')
    op.drop_table('console_events')
    op.drop_index(op.f('ix_console_jumps_expires_at'), table_name='console_jumps')
    op.drop_table('console_jumps')
