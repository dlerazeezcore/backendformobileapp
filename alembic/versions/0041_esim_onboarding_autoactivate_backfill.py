"""auto-activate eSIM rows the provider already reported as ONBOARDING / IN_USE

Revision ID: 0041_esim_onboarding_autoactivate_backfill
Revises: 0040_app_release_info
Create Date: 2026-05-31 00:00:00

Until this revision, the backend did not treat provider status ``ONBOARDING``
as activation. The frontend's lifecycle rule (``status='active'`` iff
``installed`` AND ``activated_at IS NOT NULL``) therefore left those rows
stuck on the *inactive* tab even though the provider had already started
charging the bundle clock on first connection.

The code fix (``_ESIM_STATUS_ALIASES`` adds ``ONBOARDING -> ACTIVE`` and
``_apply_active_side_effects`` sets ``installed`` / ``activated_at`` /
``expires_at``) applies from now on. This migration is the one-shot backfill
for rows that already exist in production.

Idempotent — re-running it on a clean DB is a no-op.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0041_esim_onboarding_autoactivate_backfill"
down_revision = "0040_app_release_info"
branch_labels = None
depends_on = None


def _candidates_select() -> str:
    """Rows that look like the user has installed but our DB never flipped to
    ACTIVE because the old normalizer didn't know about ONBOARDING.
    """
    return """
        SELECT id, validity_days, installed_at, activated_at, expires_at,
               app_status, provider_status, order_item_id
        FROM esim_profiles
        WHERE (
            upper(coalesce(app_status, '')) IN ('ONBOARDING', 'IN_USE')
            OR upper(coalesce(provider_status, '')) IN ('ONBOARDING', 'IN_USE')
        )
        AND (
            installed = false
            OR installed IS NULL
            OR activated_at IS NULL
        )
    """


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    now_sql = "now()" if is_postgres else "CURRENT_TIMESTAMP"
    rows = bind.execute(sa.text(_candidates_select())).fetchall()
    for row in rows:
        params = {
            "id": row.id,
            "installed_at_already": row.installed_at is not None,
            "activated_at_already": row.activated_at is not None,
            "expires_at_already": row.expires_at is not None,
        }
        # Compute expires_at = coalesce(activated_at, now) + validity_days.
        # Postgres can do interval math inline; SQLite does it by reading the
        # row back, so handle that branch in Python below.
        if is_postgres and row.validity_days and row.expires_at is None:
            bind.execute(
                sa.text(
                    """
                    UPDATE esim_profiles
                    SET app_status = 'ACTIVE',
                        installed = true,
                        installed_at = COALESCE(installed_at, now()),
                        activated_at = COALESCE(activated_at, now()),
                        expires_at = COALESCE(
                            expires_at,
                            COALESCE(activated_at, now()) + (:days || ' days')::interval
                        ),
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": row.id, "days": int(row.validity_days)},
            )
        else:
            bind.execute(
                sa.text(
                    f"""
                    UPDATE esim_profiles
                    SET app_status = 'ACTIVE',
                        installed = 1,
                        installed_at = COALESCE(installed_at, {now_sql}),
                        activated_at = COALESCE(activated_at, {now_sql}),
                        updated_at = {now_sql}
                    WHERE id = :id
                    """
                ),
                {"id": row.id},
            )
            # SQLite has no interval cast; compute expires_at in Python only
            # if we know validity_days and didn't set one already.
            if row.validity_days and row.expires_at is None and not is_postgres:
                bind.execute(
                    sa.text(
                        """
                        UPDATE esim_profiles
                        SET expires_at = datetime(COALESCE(activated_at, CURRENT_TIMESTAMP),
                                                  '+' || :days || ' days')
                        WHERE id = :id
                        """
                    ),
                    {"id": row.id, "days": int(row.validity_days)},
                )
        # Flip the parent order_item / customer_order to ACTIVE too so the
        # admin views and SQL audits don't show stale BOOKED / PENDING values.
        if row.order_item_id is not None:
            bind.execute(
                sa.text(
                    f"""
                    UPDATE order_items
                    SET item_status = 'ACTIVE',
                        updated_at = {now_sql}
                    WHERE id = :id
                    """
                ),
                {"id": row.order_item_id},
            )
            bind.execute(
                sa.text(
                    f"""
                    UPDATE customer_orders
                    SET order_status = 'ACTIVE',
                        updated_at = {now_sql}
                    WHERE id = (
                        SELECT customer_order_id FROM order_items WHERE id = :id
                    )
                    """
                ),
                {"id": row.order_item_id},
            )
        # Append an audit entry so the lifecycle history reflects the
        # auto-activation rather than appearing to silently change app_status.
        bind.execute(
            sa.text(
                f"""
                INSERT INTO esim_lifecycle_events (
                    customer_order_id, order_item_id, profile_id,
                    service_type, event_type, source, actor_type,
                    status_before, status_after, note, event_timestamp,
                    payload, created_at, updated_at
                )
                SELECT oi.customer_order_id, oi.id, :profile_id,
                       'esim', 'AUTO_ACTIVATED_BACKFILL', 'migration', 'system',
                       :status_before, 'ACTIVE',
                       'Backfill 0041: provider reported ONBOARDING/IN_USE; promoting to ACTIVE.',
                       {now_sql},
                       '{{}}', {now_sql}, {now_sql}
                FROM order_items oi
                WHERE oi.id = :order_item_id
                """
            ),
            {
                "profile_id": row.id,
                "status_before": row.app_status,
                "order_item_id": row.order_item_id,
            },
        )


def downgrade() -> None:
    # Reverting auto-activation would put real installed eSIMs back into the
    # inactive tab even though the provider has already started charging the
    # bundle. Intentionally a no-op.
    pass
