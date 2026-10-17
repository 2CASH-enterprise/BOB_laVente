"""lot 78 B texte de recherche des fiches produits

Revision ID: c78e9a1b5d43
Revises: b81d4f6a8c32
Create Date: 2026-10-17 12:00:00.000000

"""
import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c78e9a1b5d43'
down_revision = 'b81d4f6a8c32'
branch_labels = None
depends_on = None


def _flatten(sheet, variants):
    parts = [str(v) for v in (sheet or {}).values() if v not in (None, "")]
    for variant in variants or []:
        parts += [str(v) for v in ((variant or {}).get("options") or {}).values() if v]
    text = " | ".join(dict.fromkeys(p.strip() for p in parts if p.strip()))
    return text[:4000] or None


def upgrade() -> None:
    op.add_column('products', sa.Column('sheet_text', sa.Text(), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, sheet, variants FROM products WHERE sheet IS NOT NULL OR variants IS NOT NULL")).fetchall()
    for row in rows:
        sheet = row.sheet if not isinstance(row.sheet, str) else json.loads(row.sheet)
        variants = row.variants if not isinstance(row.variants, str) else json.loads(row.variants)
        text = _flatten(sheet if isinstance(sheet, dict) else None, variants if isinstance(variants, list) else None)
        if text:
            bind.execute(sa.text("UPDATE products SET sheet_text = :t WHERE id = :i"), {"t": text, "i": row.id})


def downgrade() -> None:
    op.drop_column('products', 'sheet_text')
