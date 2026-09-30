"""lot 36 issue du rendez-vous : venu, vendu, a relancer, absent ; relance unique

Revision ID: d36f1c7a2e45
Revises: c35e8b1d4f27
Create Date: 2026-09-30 17:20:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'd36f1c7a2e45'
down_revision = 'c35e8b1d4f27'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('appointment_requests', sa.Column('outcome', sa.String(length=16), nullable=True))
    op.add_column('appointment_requests', sa.Column('outcome_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('appointment_requests', sa.Column('outcome_by', sa.String(length=64), nullable=True))
    op.add_column('appointment_requests', sa.Column('followup_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('appointment_requests', sa.Column('followup_channel', sa.String(length=16), nullable=True))


def downgrade() -> None:
    for column in ('followup_channel', 'followup_sent_at', 'outcome_by', 'outcome_at', 'outcome'):
        op.drop_column('appointment_requests', column)
