"""lot 60 : rapport mensuel aux administrateurs (date d'envoi et fin de la période comptée)

Revision ID: b60d3e8f1a25
Revises: a58c6e2f9d47
Create Date: 2026-10-10 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b60d3e8f1a25'
down_revision = 'a58c6e2f9d47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('report_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tenants', sa.Column('report_period_end', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'report_period_end')
    op.drop_column('tenants', 'report_sent_at')
