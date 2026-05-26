"""merge ghost BOOKED placeholders into the real synced eSIM profile

Revision ID: 0037_merge_duplicate_esim_placeholders
Revises: 0036_json_to_jsonb
Create Date: 2026-05-26 09:20:00

Before the sync_profiles fix, an order could end up with two esim_profiles rows
for the same order_item_id: a placeholder (NULL iccid/esim_tran_no) created at
purchase time, plus the real provider-synced row. This data migration repoints
the placeholder's lifecycle events to the real row and deletes the placeholder.
Idempotent and a no-op on a clean DB.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0037_merge_duplicate_esim_placeholders"
down_revision = "0036_json_to_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Find ghost placeholders that share an order_item_id with a real (iccid/tran)
    # profile. Keep the real row; merge + delete the placeholder.
    pairs = bind.execute(
        sa.text(
            """
            SELECT ghost.id AS ghost_id, real.id AS real_id
            FROM esim_profiles ghost
            JOIN esim_profiles real
              ON real.order_item_id = ghost.order_item_id
             AND real.id <> ghost.id
            WHERE ghost.order_item_id IS NOT NULL
              AND ghost.iccid IS NULL
              AND ghost.esim_tran_no IS NULL
              AND (real.iccid IS NOT NULL OR real.esim_tran_no IS NOT NULL)
            """
        )
    ).fetchall()

    for ghost_id, real_id in pairs:
        bind.execute(
            sa.text(
                "UPDATE esim_lifecycle_events SET profile_id = :real_id WHERE profile_id = :ghost_id"
            ),
            {"real_id": real_id, "ghost_id": ghost_id},
        )
        bind.execute(
            sa.text("DELETE FROM esim_profiles WHERE id = :ghost_id"),
            {"ghost_id": ghost_id},
        )


def downgrade() -> None:
    # Deleted placeholder rows are not reconstructable; nothing to do.
    pass
