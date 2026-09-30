"""lot 28 sources des clients : canal des liens, publicite Meta

Revision ID: e28a6d3b7c52
Revises: d27f5c2e9a41
Create Date: 2026-09-30 11:10:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'e28a6d3b7c52'
down_revision = 'd27f5c2e9a41'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contact_points', sa.Column('channel', sa.String(length=16), nullable=True))
    op.add_column('customers', sa.Column('acquisition_ad_id', sa.String(length=64), nullable=True))
    # Les liens déjà attribués à un commercial (lot 27) prennent le canal « Commercial ».
    op.execute("UPDATE contact_points SET channel = 'COMMERCIAL' WHERE owner_email IS NOT NULL")


def downgrade() -> None:
    op.drop_column('customers', 'acquisition_ad_id')
    op.drop_column('contact_points', 'channel')
