"""clean featured_locations: drop test/junk rows + superseded disabled snapshots

Revision ID: 0038_clean_featured_locations
Revises: 0037_merge_duplicate_esim_placeholders
Create Date: 2026-05-26 09:30:00

The table accumulated test data ('TZFIX'/'TZAPI'/'IQCHK', 'Baghdad Promo',
'Local Time'), a 'JR' Jordan typo (valid code is 'JO'), and many superseded
enabled=false snapshots (the old save path inserted a new row + disabled the old
on every edit). Public reads already filter to enabled+popular, so deleting the
disabled history has no user-visible effect and keeps the table lean. Idempotent.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0038_clean_featured_locations"
down_revision = "0037_merge_duplicate_esim_placeholders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        sa.text(
            "DELETE FROM featured_locations WHERE upper(code) IN ('TZFIX','TZAPI','IQCHK','JR')"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM featured_locations "
            "WHERE lower(name) LIKE 'baghdad promo%' OR lower(name) LIKE 'local time%'"
        )
    )
    bind.execute(
        sa.text("DELETE FROM featured_locations WHERE enabled = :flag"),
        {"flag": False},
    )


def downgrade() -> None:
    # Cleanup of stale/junk rows is not reversible.
    pass
