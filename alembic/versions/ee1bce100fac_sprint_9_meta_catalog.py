"""sprint 9 - meta catalog

Revision ID: ee1bce100fac
Revises: 012e399c3def
Create Date: 2026-09-21 07:24:37.231347

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ee1bce100fac'
down_revision = '012e399c3def'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE n'est pas détecté par autogenerate (limitation connue
    # d'Alembic avec les enums PostgreSQL) — écrit à la main. Section 58.4.
    op.execute("ALTER TYPE ecommerce_platform ADD VALUE IF NOT EXISTS 'META_CATALOG'")


def downgrade() -> None:
    # PostgreSQL ne permet pas de retirer une valeur d'un type ENUM directement.
    # Un downgrade réel nécessiterait de recréer le type — non implémenté (cas rare
    # en pratique, et aucune donnée n'utilise cette valeur avant que ce sprint ne soit
    # déployé).
    pass
