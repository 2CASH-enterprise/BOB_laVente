"""lot 27 liens commerciaux : commercial d'un lien, client rattache au commercial

Revision ID: d27f5c2e9a41
Revises: c26d4a1b8e30
Create Date: 2026-09-29 21:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'd27f5c2e9a41'
down_revision = 'c26d4a1b8e30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('contact_points', sa.Column('owner_name', sa.String(length=80), nullable=True))
    op.add_column('contact_points', sa.Column('owner_email', sa.String(length=255), nullable=True))
    op.add_column('customers', sa.Column('referred_contact_point_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_customers_referred_contact_point', 'customers', 'contact_points',
                          ['referred_contact_point_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_customers_referred_contact_point', 'customers', type_='foreignkey')
    op.drop_column('customers', 'referred_contact_point_id')
    op.drop_column('contact_points', 'owner_email')
    op.drop_column('contact_points', 'owner_name')
