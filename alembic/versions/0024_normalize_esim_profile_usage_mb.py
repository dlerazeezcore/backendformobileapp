"""normalize esim profile usage columns to canonical MB

Revision ID: 0024_norm_profile_usage_mb
Revises: 0023_backfill_order_payments
Create Date: 2026-04-11 00:55:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0024_norm_profile_usage_mb"
down_revision = "0023_backfill_order_payments"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _usage_unit_from_values(total_raw: int | None, used_raw: int | None, remaining_raw: int | None) -> str:
    candidates = [value for value in (total_raw, used_raw, remaining_raw) if value is not None]
    if not candidates:
        return "mb"
    max_value = max(candidates)
    if max_value >= 5_000_000:
        return "bytes"
    if max_value >= 5_000:
        return "kb"
    return "mb"


def _to_mb(value: int | None, unit: str) -> int | None:
    if value is None:
        return None
    if value < 0:
        return 0
    if unit == "bytes":
        return int(round(value / (1024 * 1024)))
    if unit == "kb":
        return int(round(value / 1024))
    return value


def upgrade() -> None:
    if "esim_profiles" not in _table_names():
        return

    bind = op.get_bind()
    profiles = sa.Table("esim_profiles", sa.MetaData(), autoload_with=bind)

    rows = bind.execute(
        sa.select(
            profiles.c.id,
            profiles.c.total_data_mb,
            profiles.c.used_data_mb,
            profiles.c.remaining_data_mb,
            profiles.c.custom_fields,
        )
    ).mappings()

    for row in rows:
        total_raw = row.get("total_data_mb")
        used_raw = row.get("used_data_mb")
        remaining_raw = row.get("remaining_data_mb")
        unit = _usage_unit_from_values(total_raw, used_raw, remaining_raw)

        total_mb = _to_mb(total_raw, unit)
        used_mb = _to_mb(used_raw, unit)
        remaining_mb = _to_mb(remaining_raw, unit)
        if remaining_mb is None and total_mb is not None and used_mb is not None:
            remaining_mb = max(total_mb - used_mb, 0)

        custom_fields = row.get("custom_fields")
        if not isinstance(custom_fields, dict):
            custom_fields = {}
        custom_fields = dict(custom_fields)
        custom_fields["usageUnit"] = "MB"
        if total_mb is not None:
            custom_fields["packageDataMb"] = int(total_mb)

        changed = (
            total_mb != total_raw
            or used_mb != used_raw
            or remaining_mb != remaining_raw
            or custom_fields != (row.get("custom_fields") if isinstance(row.get("custom_fields"), dict) else {})
        )
        if not changed:
            continue

        bind.execute(
            sa.update(profiles)
            .where(profiles.c.id == row.get("id"))
            .values(
                total_data_mb=total_mb,
                used_data_mb=used_mb,
                remaining_data_mb=remaining_mb,
                custom_fields=custom_fields,
            )
        )


def downgrade() -> None:
    # Irreversible data normalization.
    return
