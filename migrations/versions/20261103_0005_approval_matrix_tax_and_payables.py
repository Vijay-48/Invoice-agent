"""approval matrix, TDS on invoices, payment terms and payables

Revision ID: 0005
Revises: 0004
Created: 2026-11-03
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('approvals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('user_name', sa.String(length=100), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_id', 'user_id')
    )
    op.create_index(op.f('ix_approvals_invoice_id'), 'approvals', ['invoice_id'], unique=False)
    op.add_column('users', sa.Column('approval_limit', sa.Numeric(precision=14, scale=2), nullable=True))
    op.add_column('invoices', sa.Column('tds_section', sa.String(length=10), nullable=True))
    op.add_column('invoices', sa.Column('tds_base', sa.Numeric(precision=14, scale=2), nullable=True))
    op.add_column('invoices', sa.Column('tds_rate', sa.Numeric(precision=5, scale=2), nullable=True))
    op.add_column('invoices', sa.Column('tds_amount', sa.Numeric(precision=14, scale=2), nullable=True))
    op.add_column('invoices', sa.Column('due_date', sa.Date(), nullable=True))
    op.add_column('invoices', sa.Column('paid_on', sa.Date(), nullable=True))
    op.add_column('invoices', sa.Column('paid_reference', sa.String(length=100), nullable=True))
    op.add_column('invoices', sa.Column('paid_by', sa.String(length=100), nullable=True))
    op.create_index(op.f('ix_invoices_due_date'), 'invoices', ['due_date'], unique=False)
    op.add_column('suppliers', sa.Column('payment_terms_days', sa.Integer(), nullable=True))
    op.add_column('suppliers', sa.Column('msme_category', sa.String(length=10), nullable=True))
    op.add_column('suppliers', sa.Column('udyam_number', sa.String(length=19), nullable=True))
    # Invoices already approved get their due date (invoice date + 30 days, the default terms)
    op.execute("UPDATE invoices SET due_date = invoice_date::date + 30 "
               "WHERE invoice_date ~ '^\\d{4}-\\d{2}-\\d{2}$' "
               "AND coalesce(extracted->>'document_type', '') NOT IN ('credit_note', 'journal_entry')")


def downgrade() -> None:
    op.drop_column('suppliers', 'udyam_number')
    op.drop_column('suppliers', 'msme_category')
    op.drop_column('suppliers', 'payment_terms_days')
    op.drop_index(op.f('ix_invoices_due_date'), table_name='invoices')
    for column in ('paid_by', 'paid_reference', 'paid_on', 'due_date', 'tds_amount', 'tds_rate', 'tds_base',
                   'tds_section'):
        op.drop_column('invoices', column)
    op.drop_column('users', 'approval_limit')
    op.drop_index(op.f('ix_approvals_invoice_id'), table_name='approvals')
    op.drop_table('approvals')
