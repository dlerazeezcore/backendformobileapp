"""activate provider-installed eSIM profiles

Revision ID: 0043_activate_provider_installed_profiles
Revises: 0042_provider_waiting_lifecycle_backfill
Create Date: 2026-05-31 00:00:00

Provider INSTALLATION plus installation/download evidence means the eSIM has
been installed on a device. Promote those rows from PROVIDER_WAITING to ACTIVE
without touching rows that are still uninstalled.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0043_activate_provider_installed_profiles"
down_revision = "0042_provider_waiting_lifecycle_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    now_sql = "now()" if is_postgres else "CURRENT_TIMESTAMP"
    installed_true = "COALESCE(installed, false) = true" if is_postgres else "COALESCE(installed, 0) = 1"

    if is_postgres:
        install_evidence_sql = """
            (
                COALESCE(custom_fields->'providerInstallEvidence', '{}'::jsonb) <> '{}'::jsonb
                OR custom_fields ? 'provider_install_evidence'
                OR upper(coalesce(provider_status, '')) IN ('INSTALLATION', 'INSTALLED', 'ENABLED')
            )
        """
        activation_anchor_sql = "COALESCE(installed_at, last_provider_sync_at, now())"
        expires_sql = """
            expires_at = COALESCE(
                expires_at,
                CASE
                    WHEN validity_days IS NOT NULL AND validity_days > 0
                    THEN COALESCE(activated_at, installed_at, last_provider_sync_at, now())
                         + (validity_days || ' days')::interval
                    ELSE NULL
                END
            ),
        """
    else:
        install_evidence_sql = """
            (
                COALESCE(custom_fields, '') LIKE '%providerInstallEvidence%'
                OR upper(coalesce(provider_status, '')) IN ('INSTALLATION', 'INSTALLED', 'ENABLED')
            )
        """
        activation_anchor_sql = f"COALESCE(installed_at, last_provider_sync_at, {now_sql})"
        expires_sql = ""

    bind.execute(
        sa.text(
            f"""
            UPDATE esim_profiles
            SET app_status = 'ACTIVE',
                activated_at = COALESCE(activated_at, {activation_anchor_sql}),
                {expires_sql}
                updated_at = {now_sql}
            WHERE {installed_true}
              AND upper(coalesce(app_status, '')) = 'PROVIDER_WAITING'
              AND {install_evidence_sql}
            """
        )
    )

    bind.execute(
        sa.text(
            f"""
            UPDATE order_items
            SET item_status = 'ACTIVE',
                updated_at = {now_sql}
            WHERE id IN (
                SELECT order_item_id
                FROM esim_profiles
                WHERE order_item_id IS NOT NULL
                  AND {installed_true}
                  AND upper(coalesce(app_status, '')) = 'ACTIVE'
                  AND activated_at IS NOT NULL
            )
              AND upper(coalesce(item_status, '')) = 'PROVIDER_WAITING'
            """
        )
    )

    bind.execute(
        sa.text(
            f"""
            UPDATE customer_orders
            SET order_status = 'ACTIVE',
                updated_at = {now_sql}
            WHERE id IN (
                SELECT oi.customer_order_id
                FROM order_items oi
                JOIN esim_profiles ep ON ep.order_item_id = oi.id
                WHERE oi.customer_order_id IS NOT NULL
                  AND {"COALESCE(ep.installed, false) = true" if is_postgres else "COALESCE(ep.installed, 0) = 1"}
                  AND upper(coalesce(ep.app_status, '')) = 'ACTIVE'
                  AND ep.activated_at IS NOT NULL
            )
              AND upper(coalesce(order_status, '')) = 'PROVIDER_WAITING'
            """
        )
    )

    bind.execute(
        sa.text(
            f"""
            INSERT INTO esim_lifecycle_events (
                customer_order_id, order_item_id, profile_id,
                service_type, event_type, source, actor_type,
                status_before, status_after, note, event_timestamp,
                payload, created_at, updated_at
            )
            SELECT oi.customer_order_id, oi.id, ep.id,
                   'esim', 'PROVIDER_INSTALL_ACTIVATED_BACKFILL', 'migration', 'system',
                   'PROVIDER_WAITING', 'ACTIVE',
                   'Backfill 0043: provider installation evidence promoted profile to ACTIVE.',
                   {now_sql},
                   '{{}}', {now_sql}, {now_sql}
            FROM esim_profiles ep
            JOIN order_items oi ON oi.id = ep.order_item_id
            WHERE upper(coalesce(ep.app_status, '')) = 'ACTIVE'
              AND ep.activated_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM esim_lifecycle_events ev
                  WHERE ev.profile_id = ep.id
                    AND ev.event_type = 'PROVIDER_INSTALL_ACTIVATED_BACKFILL'
              )
            """
        )
    )


def downgrade() -> None:
    pass
