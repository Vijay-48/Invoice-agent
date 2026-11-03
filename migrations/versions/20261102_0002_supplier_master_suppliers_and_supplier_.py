"""supplier master: suppliers and supplier_events

Revision ID: 0002
Revises: 0001
Created: 2026-11-02 12:14:11.202122
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('suppliers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin', sa.String(length=15), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('pan', sa.String(length=10), nullable=True),
    sa.Column('bank_account', sa.String(length=34), nullable=True),
    sa.Column('bank_ifsc', sa.String(length=11), nullable=True),
    sa.Column('bank_set_by', sa.String(length=100), nullable=True),
    sa.Column('bank_set_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('bank_source_invoice_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['bank_source_invoice_id'], ['invoices.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('gstin')
    )
    op.create_table('supplier_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('actor', sa.String(length=100), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_supplier_events_supplier_id'), 'supplier_events', ['supplier_id'], unique=False)

    # One supplier per GSTIN that already has approved invoices, under the name it is billed by most often.
    # No bank details: invoices read before this migration didn't extract them.
    op.execute("""
        INSERT INTO suppliers (gstin, name, pan)
        SELECT supplier_gstin, mode() WITHIN GROUP (ORDER BY supplier_name), substr(supplier_gstin, 3, 10)
        FROM invoices
        WHERE status IN ('APPROVED', 'EXPORTED') AND supplier_gstin ~ '^[0-9]{2}[A-Z0-9]{13}$'
          AND supplier_name IS NOT NULL AND coalesce(extracted->>'document_type', '') <> 'journal_entry'
        GROUP BY supplier_gstin
    """)
    op.execute("""
        INSERT INTO supplier_events (supplier_id, kind, actor, message)
        SELECT id, 'created', 'system', 'Added from the approved invoices (migration 0002)' FROM suppliers
    """)


def downgrade() -> None:
    op.drop_index(op.f('ix_supplier_events_supplier_id'), table_name='supplier_events')
    op.drop_table('supplier_events')
    op.drop_table('suppliers')
