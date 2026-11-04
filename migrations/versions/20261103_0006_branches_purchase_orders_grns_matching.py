"""branches, purchase orders, GRNs and invoice to PO matching

Revision ID: 0006
Revises: 0005
Created: 2026-11-03
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('branches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('city', sa.String(length=60), nullable=True),
    sa.Column('state_code', sa.String(length=2), nullable=True),
    sa.Column('gstin', sa.String(length=15), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_index(op.f('ix_branches_gstin'), 'branches', ['gstin'], unique=False)
    op.create_table('purchase_orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('po_number', sa.String(length=50), nullable=False),
    sa.Column('po_number_norm', sa.String(length=50), nullable=False),
    sa.Column('supplier_gstin', sa.String(length=15), nullable=True),
    sa.Column('supplier_name', sa.String(length=255), nullable=False),
    sa.Column('branch_id', sa.Integer(), nullable=True),
    sa.Column('po_date', sa.Date(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('po_number_norm')
    )
    op.create_index(op.f('ix_purchase_orders_status'), 'purchase_orders', ['status'], unique=False)
    op.create_index(op.f('ix_purchase_orders_supplier_gstin'), 'purchase_orders', ['supplier_gstin'], unique=False)
    op.create_table('po_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('po_id', sa.Integer(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('hsn_sac', sa.String(length=10), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=14, scale=3), nullable=False),
    sa.Column('unit', sa.String(length=20), nullable=True),
    sa.Column('unit_price', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('gst_rate', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('branch_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ),
    sa.ForeignKeyConstraint(['po_id'], ['purchase_orders.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('po_id', 'line_no')
    )
    op.create_index(op.f('ix_po_lines_po_id'), 'po_lines', ['po_id'], unique=False)
    op.create_table('goods_receipts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('po_id', sa.Integer(), nullable=False),
    sa.Column('branch_id', sa.Integer(), nullable=True),
    sa.Column('received_on', sa.Date(), nullable=False),
    sa.Column('challan_number', sa.String(length=50), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=False),
    sa.Column('confirmed_by', sa.String(length=100), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ),
    sa.ForeignKeyConstraint(['po_id'], ['purchase_orders.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_goods_receipts_po_id'), 'goods_receipts', ['po_id'], unique=False)
    op.create_table('grn_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('grn_id', sa.Integer(), nullable=False),
    sa.Column('po_line_id', sa.Integer(), nullable=False),
    sa.Column('received_qty', sa.Numeric(precision=14, scale=3), nullable=False),
    sa.Column('accepted_qty', sa.Numeric(precision=14, scale=3), nullable=False),
    sa.Column('rejected_qty', sa.Numeric(precision=14, scale=3), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['grn_id'], ['goods_receipts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['po_line_id'], ['po_lines.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_grn_lines_grn_id'), 'grn_lines', ['grn_id'], unique=False)
    op.create_index(op.f('ix_grn_lines_po_line_id'), 'grn_lines', ['po_line_id'], unique=False)
    op.create_table('invoice_po_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('invoice_id', sa.Integer(), nullable=False),
    sa.Column('invoice_line', sa.Integer(), nullable=False),
    sa.Column('po_line_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=14, scale=3), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['po_line_id'], ['po_lines.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_invoice_po_lines_invoice_id'), 'invoice_po_lines', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_invoice_po_lines_po_line_id'), 'invoice_po_lines', ['po_line_id'], unique=False)
    op.add_column('users', sa.Column('branch_id', sa.Integer(), nullable=True))
    op.create_foreign_key(None, 'users', 'branches', ['branch_id'], ['id'])
    op.add_column('invoices', sa.Column('po_id', sa.Integer(), nullable=True))
    op.add_column('invoices', sa.Column('branch_id', sa.Integer(), nullable=True))
    op.add_column('invoices', sa.Column('match_status', sa.String(length=20), nullable=True))
    op.create_index(op.f('ix_invoices_po_id'), 'invoices', ['po_id'], unique=False)
    op.create_index(op.f('ix_invoices_branch_id'), 'invoices', ['branch_id'], unique=False)
    op.create_foreign_key(None, 'invoices', 'purchase_orders', ['po_id'], ['id'])
    op.create_foreign_key(None, 'invoices', 'branches', ['branch_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('invoices_branch_id_fkey', 'invoices', type_='foreignkey')
    op.drop_constraint('invoices_po_id_fkey', 'invoices', type_='foreignkey')
    op.drop_index(op.f('ix_invoices_branch_id'), table_name='invoices')
    op.drop_index(op.f('ix_invoices_po_id'), table_name='invoices')
    for column in ('match_status', 'branch_id', 'po_id'):
        op.drop_column('invoices', column)
    op.drop_constraint('users_branch_id_fkey', 'users', type_='foreignkey')
    op.drop_column('users', 'branch_id')
    op.drop_table('invoice_po_lines')
    op.drop_table('grn_lines')
    op.drop_table('goods_receipts')
    op.drop_table('po_lines')
    op.drop_table('purchase_orders')
    op.drop_index(op.f('ix_branches_gstin'), table_name='branches')
    op.drop_table('branches')
