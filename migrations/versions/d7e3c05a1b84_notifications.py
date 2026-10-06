"""Notifications, addressed to a user so admins and affiliates share one table.

Revision ID: d7e3c05a1b84
Revises: c4a81b2f90de
Create Date: 2026-10-02

Purely additive: one new table, nothing existing is touched.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd7e3c05a1b84'
down_revision = 'c4a81b2f90de'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'notification',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=48), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('body', sa.String(length=1024), nullable=True),
        sa.Column('href', sa.String(length=255), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notification_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notification_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_notification_created_at'), ['created_at'], unique=False)


def downgrade():
    with op.batch_alter_table('notification', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notification_created_at'))
        batch_op.drop_index(batch_op.f('ix_notification_kind'))
        batch_op.drop_index(batch_op.f('ix_notification_user_id'))
    op.drop_table('notification')
