"""gstr2b reconciliation: returns and documents

Revision ID: 0003
Revises: 0002
Created: 2026-11-02 12:25:47.701982
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('gstr2b_returns',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('gstin', sa.String(length=15), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('generated_on', sa.String(length=10), nullable=True),
    sa.Column('file_name', sa.String(length=255), nullable=False),
    sa.Column('file_hash', sa.String(length=64), nullable=False),
    sa.Column('documents', sa.Integer(), nullable=False),
    sa.Column('uploaded_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('gstin', 'period')
    )
    op.create_table('gstr2b_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('return_id', sa.Integer(), nullable=False),
    sa.Column('section', sa.String(length=10), nullable=False),
    sa.Column('doc_type', sa.String(length=15), nullable=False),
    sa.Column('supplier_gstin', sa.String(length=15), nullable=False),
    sa.Column('supplier_name', sa.String(length=255), nullable=True),
    sa.Column('number', sa.String(length=100), nullable=False),
    sa.Column('number_norm', sa.String(length=100), nullable=False),
    sa.Column('doc_date', sa.String(length=10), nullable=True),
    sa.Column('taxable', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('igst', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('cgst', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('sgst', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('cess', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('total', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('itc_available', sa.Boolean(), nullable=False),
    sa.Column('itc_reason', sa.String(length=100), nullable=True),
    sa.Column('reverse_charge', sa.Boolean(), nullable=False),
    sa.Column('supplier_filed_on', sa.String(length=10), nullable=True),
    sa.ForeignKeyConstraint(['return_id'], ['gstr2b_returns.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_gstr2b_documents_match', 'gstr2b_documents', ['supplier_gstin', 'number_norm'], unique=False)
    op.create_index(op.f('ix_gstr2b_documents_return_id'), 'gstr2b_documents', ['return_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_gstr2b_documents_return_id'), table_name='gstr2b_documents')
    op.drop_index('ix_gstr2b_documents_match', table_name='gstr2b_documents')
    op.drop_table('gstr2b_documents')
    op.drop_table('gstr2b_returns')
