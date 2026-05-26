"""convert remaining json columns to jsonb for consistency + indexable/faster reads

Revision ID: 0036_json_to_jsonb
Revises: 0035_user_email_unique_ci
Create Date: 2026-05-26 09:10:00

The payment tables already use jsonb; the rest used plain json. Standardize on
jsonb everywhere (binary, deduped keys, indexable). Postgres-only; SQLite stores
JSON as TEXT so this is a no-op there.
"""
from __future__ import annotations

from alembic import op


revision = "0036_json_to_jsonb"
down_revision = "0035_user_email_unique_ci"
branch_labels = None
depends_on = None


_JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("admin_users", "custom_fields"),
    ("discount_rules", "custom_fields"),
    ("esim_lifecycle_events", "payload"),
    ("esim_profiles", "custom_fields"),
    ("exchange_rates", "custom_fields"),
    ("featured_locations", "custom_fields"),
    ("order_items", "custom_fields"),
    ("pricing_rules", "custom_fields"),
    ("push_devices", "custom_fields"),
    ("push_notifications", "data_payload"),
    ("push_notifications", "invalid_tokens"),
    ("push_notifications", "provider_response"),
    ("push_notifications", "target_user_ids"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, column in _JSON_COLUMNS:
        op.execute(
            f'ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb'
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, column in _JSON_COLUMNS:
        op.execute(
            f'ALTER TABLE {table} ALTER COLUMN {column} TYPE json USING {column}::json'
        )
