"""Referral bonus: invite codes, who invited whom, bonus rows (TPD-1875)

Revision ID: b8e4f2a1c3d5
Revises: a7d3e1c9b2f0
Create Date: 2026-11-11

Additive only. Every NOT NULL column carries a server default so this runs on
tables that already have rows. Invite codes are not backfilled here: an
affiliate is given one on approval or the first time they open the invite
page, which keeps code generation in one place.
"""
import sqlalchemy as sa
from alembic import op

revision = "b8e4f2a1c3d5"
down_revision = "a7d3e1c9b2f0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("affiliate") as batch_op:
        batch_op.add_column(sa.Column("referral_code", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("referred_by_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_affiliate_referral_code", ["referral_code"], unique=True)
        batch_op.create_index("ix_affiliate_referred_by_id", ["referred_by_id"], unique=False)
        batch_op.create_foreign_key("fk_affiliate_referred_by_id", "affiliate",
                                    ["referred_by_id"], ["id"])

    with op.batch_alter_table("earning") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=16), nullable=False,
                                      server_default="sale"))
        batch_op.add_column(sa.Column("source_earning_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("bonus", sa.Numeric(14, 2), nullable=False,
                                      server_default="0"))
        batch_op.add_column(sa.Column("bonus_percent", sa.Numeric(5, 2), nullable=True))
        batch_op.create_index("ix_earning_kind", ["kind"], unique=False)
        batch_op.create_index("ix_earning_source_earning_id", ["source_earning_id"], unique=False)
        batch_op.create_foreign_key("fk_earning_source_earning_id", "earning",
                                    ["source_earning_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("earning") as batch_op:
        batch_op.drop_constraint("fk_earning_source_earning_id", type_="foreignkey")
        batch_op.drop_index("ix_earning_source_earning_id")
        batch_op.drop_index("ix_earning_kind")
        batch_op.drop_column("bonus_percent")
        batch_op.drop_column("bonus")
        batch_op.drop_column("source_earning_id")
        batch_op.drop_column("kind")

    with op.batch_alter_table("affiliate") as batch_op:
        batch_op.drop_constraint("fk_affiliate_referred_by_id", type_="foreignkey")
        batch_op.drop_index("ix_affiliate_referred_by_id")
        batch_op.drop_index("ix_affiliate_referral_code")
        batch_op.drop_column("referred_by_id")
        batch_op.drop_column("referral_code")
