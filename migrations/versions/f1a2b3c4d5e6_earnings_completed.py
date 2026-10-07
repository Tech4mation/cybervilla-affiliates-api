"""Earnings are completed, not pending or approved

A sale the store has confirmed as paid is money owed. The separate
approval step is gone: a person still vets the money, but once, when the
payout is released, which is the step that actually moves it.

So three statuses remain — completed, paid, reversed — and the old two
collapse into one. "pending" in particular was misleading: it suggested
the sale might not be real, when Odoo had already taken the customer's
money.

Note this only touches the `earning` table. `users.status` and
`affiliate.status` use the words pending and approved for something
entirely different — whether a person may use the portal — and are left
alone.

Revision ID: f1a2b3c4d5e6
Revises: e0c0c61d2358
Create Date: 2026-10-07

"""
from alembic import op

revision = 'f1a2b3c4d5e6'
down_revision = 'e0c0c61d2358'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE earning SET status = 'completed' "
               "WHERE status IN ('pending', 'approved', 'payable')")


def downgrade():
    # Everything that was approved or pending comes back as pending. The
    # distinction between them was not recorded anywhere else, so it cannot
    # be restored; approved_at is the only hint and it was never reliable.
    op.execute("UPDATE earning SET status = 'pending' WHERE status = 'completed'")
