"""GRN creator (user id) and source, for GRNs drafted on WhatsApp

Revision ID: 0007
Revises: 0006
Created: 2026-11-03
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('goods_receipts', sa.Column('created_by_id', sa.Integer(), nullable=True))
    op.add_column('goods_receipts', sa.Column('source', sa.String(length=10), server_default='api', nullable=False))
    op.alter_column('goods_receipts', 'source', server_default=None)
    op.create_foreign_key(None, 'goods_receipts', 'users', ['created_by_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('goods_receipts_created_by_id_fkey', 'goods_receipts', type_='foreignkey')
    op.drop_column('goods_receipts', 'source')
    op.drop_column('goods_receipts', 'created_by_id')
