"""Payouts, and the link from an earning to the payment that settled it.

Revision ID: c4a81b2f90de
Revises: e071e0df80eb
Create Date: 2026-10-01

Nothing existing is altered destructively: two nullable columns are added to
`earning`, so every row already there stays valid and simply reads as "not
approved, not paid yet".
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4a81b2f90de'
down_revision = 'e071e0df80eb'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'payout',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('reference', sa.String(length=64), nullable=True),
        sa.Column('affiliate_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=True),
        sa.Column('method', sa.String(length=32), nullable=False,
                  server_default='manual'),
        sa.Column('status', sa.String(length=16), nullable=False,
                  server_default='requested'),
        sa.Column('failure_reason', sa.String(length=512), nullable=True),
        sa.Column('bank_account_name', sa.String(length=255), nullable=True),
        sa.Column('bank_account_number', sa.String(length=32), nullable=True),
        sa.Column('bank_name', sa.String(length=128), nullable=True),
        sa.Column('bank_code', sa.String(length=16), nullable=True),
        sa.Column('provider_reference', sa.String(length=64), nullable=True),
        sa.Column('provider_status', sa.String(length=32), nullable=True),
        sa.Column('note', sa.String(length=512), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['affiliate_id'], ['affiliate.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('payout', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payout_reference'), ['reference'], unique=True)
        batch_op.create_index(batch_op.f('ix_payout_affiliate_id'), ['affiliate_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payout_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_payout_provider_reference'), ['provider_reference'], unique=False)

    with op.batch_alter_table('affiliate', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bank_name', sa.String(length=128), nullable=True))

    with op.batch_alter_table('earning', schema=None) as batch_op:
        batch_op.add_column(sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('payout_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_earning_payout_id'), ['payout_id'], unique=False)
        batch_op.create_foreign_key('fk_earning_payout', 'payout', ['payout_id'], ['id'])


def downgrade():
    with op.batch_alter_table('earning', schema=None) as batch_op:
        batch_op.drop_constraint('fk_earning_payout', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_earning_payout_id'))
        batch_op.drop_column('payout_id')
        batch_op.drop_column('approved_at')

    with op.batch_alter_table('affiliate', schema=None) as batch_op:
        batch_op.drop_column('bank_name')

    with op.batch_alter_table('payout', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payout_provider_reference'))
        batch_op.drop_index(batch_op.f('ix_payout_status'))
        batch_op.drop_index(batch_op.f('ix_payout_affiliate_id'))
        batch_op.drop_index(batch_op.f('ix_payout_reference'))
    op.drop_table('payout')
