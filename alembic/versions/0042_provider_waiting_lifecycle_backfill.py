"""backfill provider-waiting eSIM lifecycle drift

Revision ID: 0042_provider_waiting_lifecycle_backfill
Revises: 0041_esim_onboarding_autoactivate_backfill
Create Date: 2026-05-31 00:00:00

Provider-active rows are no longer allowed to become app-active until the app
has a confirmed install signal. This migration demotes old uninstalled ACTIVE
drift to PROVIDER_WAITING and creates missing profile placeholders for older
eSIM order items that already have provider order numbers.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0042_provider_waiting_lifecycle_backfill"
down_revision = "0041_esim_onboarding_autoactivate_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    now_sql = "now()" if is_postgres else "CURRENT_TIMESTAMP"
    installed_false = "COALESCE(installed, false) = false" if is_postgres else "COALESCE(installed, 0) = 0"

    bind.execute(
        sa.text(
            f"""
            UPDATE esim_profiles
            SET app_status = 'PROVIDER_WAITING',
                updated_at = {now_sql}
            WHERE {installed_false}
              AND (
                upper(coalesce(app_status, '')) IN ('ACTIVE', 'ONBOARDING', 'IN_USE')
                OR upper(coalesce(provider_status, '')) IN (
                    'ACTIVE', 'ENABLED', 'ONBOARDING', 'IN_USE', 'INSTALLATION', 'INSTALLED'
                )
              )
            """
        )
    )

    bind.execute(
        sa.text(
            f"""
            UPDATE order_items
            SET item_status = 'PROVIDER_WAITING',
                updated_at = {now_sql}
            WHERE id IN (
                SELECT order_item_id
                FROM esim_profiles
                WHERE order_item_id IS NOT NULL
                  AND {installed_false}
                  AND upper(coalesce(app_status, '')) = 'PROVIDER_WAITING'
            )
              AND upper(coalesce(item_status, '')) IN ('ACTIVE', 'ONBOARDING', 'IN_USE')
            """
        )
    )

    bind.execute(
        sa.text(
            f"""
            UPDATE customer_orders
            SET order_status = 'PROVIDER_WAITING',
                updated_at = {now_sql}
            WHERE id IN (
                SELECT oi.customer_order_id
                FROM order_items oi
                JOIN esim_profiles ep ON ep.order_item_id = oi.id
                WHERE oi.customer_order_id IS NOT NULL
                  AND {installed_false.replace('installed', 'ep.installed')}
                  AND upper(coalesce(ep.app_status, '')) = 'PROVIDER_WAITING'
            )
              AND upper(coalesce(order_status, '')) IN ('ACTIVE', 'ONBOARDING', 'IN_USE')
            """
        )
    )

    custom_fields_expr = (
        "jsonb_strip_nulls(COALESCE(oi.custom_fields, '{}'::jsonb) || "
        "jsonb_build_object('backfilledProfilePlaceholder', true, 'providerOrderNo', oi.provider_order_no))"
        if is_postgres
        else "COALESCE(oi.custom_fields, '{}')"
    )

    bind.execute(
        sa.text(
            f"""
            INSERT INTO esim_profiles (
                order_item_id, user_id, provider_status, app_status, installed,
                last_provider_sync_at, custom_fields, created_at, updated_at
            )
            SELECT
                oi.id,
                co.user_id,
                oi.provider_status,
                CASE
                    WHEN upper(coalesce(oi.item_status, '')) IN ('ACTIVE', 'ONBOARDING', 'IN_USE')
                      OR upper(coalesce(oi.provider_status, '')) IN (
                        'ACTIVE', 'ENABLED', 'ONBOARDING', 'IN_USE', 'INSTALLATION', 'INSTALLED'
                      )
                    THEN 'PROVIDER_WAITING'
                    ELSE COALESCE(NULLIF(oi.item_status, ''), 'BOOKED')
                END,
                {"false" if is_postgres else "0"},
                COALESCE(oi.last_provider_sync_at, {now_sql}),
                {custom_fields_expr},
                COALESCE(oi.created_at, {now_sql}),
                {now_sql}
            FROM order_items oi
            JOIN customer_orders co ON co.id = oi.customer_order_id
            WHERE lower(coalesce(oi.service_type, '')) = 'esim'
              AND oi.provider_order_no IS NOT NULL
              AND oi.provider_order_no <> ''
              AND NOT EXISTS (
                SELECT 1 FROM esim_profiles ep WHERE ep.order_item_id = oi.id
              )
            """
        )
    )


def downgrade() -> None:
    # This is a corrective production backfill. Re-activating uninstalled rows
    # on downgrade would violate the current lifecycle invariant.
    pass
