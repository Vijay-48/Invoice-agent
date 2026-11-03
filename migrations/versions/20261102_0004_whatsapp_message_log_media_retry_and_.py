"""whatsapp message log, media retry and senders on users

Revision ID: 0004
Revises: 0003
Created: 2026-11-02 16:56:43.328884
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('whatsapp_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_sid', sa.String(length=64), nullable=False),
    sa.Column('sender', sa.String(length=40), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('text_length', sa.Integer(), nullable=False),
    sa.Column('media_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_sid')
    )
    op.create_index(op.f('ix_whatsapp_messages_created_at'), 'whatsapp_messages', ['created_at'], unique=False)
    op.create_index(op.f('ix_whatsapp_messages_user_id'), 'whatsapp_messages', ['user_id'], unique=False)
    op.create_table('whatsapp_media',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('content_type', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.String(length=300), nullable=True),
    sa.Column('invoice_id', sa.Integer(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_try_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['message_id'], ['whatsapp_messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id', 'position')
    )
    op.create_index('ix_whatsapp_media_retry', 'whatsapp_media', ['status', 'next_try_at'], unique=False)
    op.add_column('users', sa.Column('whatsapp_number', sa.String(length=20), nullable=True))
    op.create_unique_constraint('users_whatsapp_number_key', 'users', ['whatsapp_number'])


def downgrade() -> None:
    op.drop_constraint('users_whatsapp_number_key', 'users', type_='unique')
    op.drop_column('users', 'whatsapp_number')
    op.drop_index('ix_whatsapp_media_retry', table_name='whatsapp_media')
    op.drop_table('whatsapp_media')
    op.drop_index(op.f('ix_whatsapp_messages_user_id'), table_name='whatsapp_messages')
    op.drop_index(op.f('ix_whatsapp_messages_created_at'), table_name='whatsapp_messages')
    op.drop_table('whatsapp_messages')
