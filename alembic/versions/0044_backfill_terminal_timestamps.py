"""backfill canceled_at / refunded_at / revoked_at for terminal eSIM profiles

Revision ID: 0044_backfill_terminal_timestamps
Revises: 0043_activate_provider_installed_profiles
Create Date: 2026-05-31 22:00:00

Production has many ``CANCELLED`` profiles with ``canceled_at = NULL`` because
the webhook/provider-sync path used to only flip ``app_status`` and never
stamp the matching lifecycle timestamp. Same for ``REFUNDED`` and ``REVOKED``.

This migration stamps the missing timestamps using ``updated_at`` as the
best-available approximation of when the state actually changed. After this
runs, admin reports and audit SQL can rely on the timestamps being present
whenever the corresponding status is set. The runtime helper
``_apply_terminal_side_effects`` keeps the invariant going forward.

Idempotent — re-running it on a clean DB is a no-op.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0044_backfill_terminal_timestamps"
down_revision = "0043_activate_provider_installed_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    now_sql = "now()" if is_postgres else "CURRENT_TIMESTAMP"

    # esim_profiles: stamp canceled_at, refunded_at, revoked_at.
    bind.execute(
        sa.text(
            f"""
            UPDATE esim_profiles
            SET canceled_at = COALESCE(canceled_at, updated_at, {now_sql})
            WHERE upper(coalesce(app_status, '')) = 'CANCELLED'
              AND canceled_at IS NULL
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE esim_profiles
            SET refunded_at = COALESCE(refunded_at, updated_at, {now_sql})
            WHERE upper(coalesce(app_status, '')) = 'REFUNDED'
              AND refunded_at IS NULL
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE esim_profiles
            SET revoked_at = COALESCE(revoked_at, updated_at, {now_sql})
            WHERE upper(coalesce(app_status, '')) = 'REVOKED'
              AND revoked_at IS NULL
            """
        )
    )

    # order_items: same pattern.
    bind.execute(
        sa.text(
            f"""
            UPDATE order_items
            SET canceled_at = COALESCE(canceled_at, updated_at, {now_sql})
            WHERE upper(coalesce(item_status, '')) = 'CANCELLED'
              AND canceled_at IS NULL
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE order_items
            SET refunded_at = COALESCE(refunded_at, updated_at, {now_sql})
            WHERE upper(coalesce(item_status, '')) = 'REFUNDED'
              AND refunded_at IS NULL
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            UPDATE order_items
            SET revoked_at = COALESCE(revoked_at, updated_at, {now_sql})
            WHERE upper(coalesce(item_status, '')) = 'REVOKED'
              AND revoked_at IS NULL
            """
        )
    )


def downgrade() -> None:
    # Restoring NULLs would only obscure the actual cancellation/refund times
    # we just inferred from updated_at. Intentional no-op.
    pass
