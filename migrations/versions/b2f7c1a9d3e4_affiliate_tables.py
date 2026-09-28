"""affiliate tables

Revision ID: b2f7c1a9d3e4
Revises: e830d8584d47
Create Date: 2026-09-24 09:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2f7c1a9d3e4'
down_revision = 'e830d8584d47'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('affiliate',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('backend_ref', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('phone', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('bank_code', sa.String(length=16), nullable=True),
    sa.Column('bank_account_number', sa.String(length=32), nullable=True),
    sa.Column('bank_account_name', sa.String(length=255), nullable=True),
    sa.Column('paystack_recipient_code', sa.String(length=64), nullable=True),
    sa.Column('odoo_affiliate_id', sa.Integer(), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('affiliate', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_affiliate_backend_ref'), ['backend_ref'], unique=True)
        batch_op.create_index(batch_op.f('ix_affiliate_email'), ['email'], unique=False)
        batch_op.create_index(batch_op.f('ix_affiliate_odoo_affiliate_id'), ['odoo_affiliate_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_affiliate_status'), ['status'], unique=False)

    op.create_table('affiliate_link',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('backend_ref', sa.String(length=64), nullable=True),
    sa.Column('affiliate_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=255), nullable=True),
    sa.Column('target_type', sa.String(length=16), nullable=False),
    sa.Column('product_odoo_id', sa.Integer(), nullable=True),
    sa.Column('markup_percent', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('odoo_link_id', sa.Integer(), nullable=True),
    sa.Column('odoo_pricelist_id', sa.Integer(), nullable=True),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['affiliate_id'], ['affiliate.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('affiliate_link', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_affiliate_link_active'), ['active'], unique=False)
        batch_op.create_index(batch_op.f('ix_affiliate_link_affiliate_id'), ['affiliate_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_affiliate_link_backend_ref'), ['backend_ref'], unique=True)
        batch_op.create_index(batch_op.f('ix_affiliate_link_code'), ['code'], unique=True)
        batch_op.create_index(batch_op.f('ix_affiliate_link_odoo_link_id'), ['odoo_link_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_affiliate_link_product_odoo_id'), ['product_odoo_id'], unique=False)

    op.create_table('earning',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('odoo_order_ref', sa.String(length=64), nullable=False),
    sa.Column('odoo_order_id', sa.Integer(), nullable=True),
    sa.Column('affiliate_id', sa.Integer(), nullable=True),
    sa.Column('link_id', sa.Integer(), nullable=True),
    sa.Column('affiliate_backend_ref', sa.String(length=64), nullable=True),
    sa.Column('link_backend_ref', sa.String(length=64), nullable=True),
    sa.Column('affiliate_code', sa.String(length=64), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=True),
    sa.Column('markup_percent', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('amount_total', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('amount_untaxed', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('earning', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('customer_email', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('last_event', sa.String(length=32), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['affiliate_id'], ['affiliate.id'], ),
    sa.ForeignKeyConstraint(['link_id'], ['affiliate_link.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('earning', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_earning_affiliate_backend_ref'), ['affiliate_backend_ref'], unique=False)
        batch_op.create_index(batch_op.f('ix_earning_affiliate_code'), ['affiliate_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_earning_affiliate_id'), ['affiliate_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_earning_link_id'), ['link_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_earning_odoo_order_ref'), ['odoo_order_ref'], unique=True)
        batch_op.create_index(batch_op.f('ix_earning_status'), ['status'], unique=False)


def downgrade():
    with op.batch_alter_table('earning', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_earning_status'))
        batch_op.drop_index(batch_op.f('ix_earning_odoo_order_ref'))
        batch_op.drop_index(batch_op.f('ix_earning_link_id'))
        batch_op.drop_index(batch_op.f('ix_earning_affiliate_id'))
        batch_op.drop_index(batch_op.f('ix_earning_affiliate_code'))
        batch_op.drop_index(batch_op.f('ix_earning_affiliate_backend_ref'))

    op.drop_table('earning')
    with op.batch_alter_table('affiliate_link', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_affiliate_link_product_odoo_id'))
        batch_op.drop_index(batch_op.f('ix_affiliate_link_odoo_link_id'))
        batch_op.drop_index(batch_op.f('ix_affiliate_link_code'))
        batch_op.drop_index(batch_op.f('ix_affiliate_link_backend_ref'))
        batch_op.drop_index(batch_op.f('ix_affiliate_link_affiliate_id'))
        batch_op.drop_index(batch_op.f('ix_affiliate_link_active'))

    op.drop_table('affiliate_link')
    with op.batch_alter_table('affiliate', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_affiliate_status'))
        batch_op.drop_index(batch_op.f('ix_affiliate_odoo_affiliate_id'))
        batch_op.drop_index(batch_op.f('ix_affiliate_email'))
        batch_op.drop_index(batch_op.f('ix_affiliate_backend_ref'))

    op.drop_table('affiliate')
