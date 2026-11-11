"""Mark which reported order line is the delivery charge

Revision ID: a7d3e1c9b2f0
Revises: f1a2b3c4d5e6
Create Date: 2026-11-11

Additive only. The server default is what lets this run on a table that
already has rows (a NOT NULL column without one fails there).
"""
import sqlalchemy as sa
from alembic import op

revision = "a7d3e1c9b2f0"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("earning_line") as batch_op:
        batch_op.add_column(sa.Column(
            "is_delivery", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))


def downgrade():
    with op.batch_alter_table("earning_line") as batch_op:
        batch_op.drop_column("is_delivery")
