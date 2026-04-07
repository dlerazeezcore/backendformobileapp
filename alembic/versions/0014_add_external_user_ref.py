"""add external_user_ref to payment_attempts

Revision ID: 0014_add_external_user_ref
Revises: 0013_payment_persistence_v2
Create Date: 2026-04-08 00:25:00
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0014_add_external_user_ref"
down_revision = "0013_payment_persistence_v2"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _extract_external_ref(metadata: dict[str, Any]) -> str | None:
    for key in ("externalUserRef", "customerUserId", "customer_user_id", "userId", "user_id"):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
            continue
        return str(value)
    return None


def upgrade() -> None:
    if "payment_attempts" not in _table_names():
        return
    columns = _column_names("payment_attempts")
    if "external_user_ref" not in columns:
        op.add_column("payment_attempts", sa.Column("external_user_ref", sa.Text(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, metadata, external_user_ref FROM payment_attempts")).mappings()
    for row in rows:
        if row.get("external_user_ref"):
            continue
        metadata = _safe_json(row.get("metadata"))
        ref = _extract_external_ref(metadata)
        if not ref:
            continue
        bind.execute(
            sa.text("UPDATE payment_attempts SET external_user_ref = :ref WHERE id = :id"),
            {"ref": ref, "id": row["id"]},
        )


def downgrade() -> None:
    if "payment_attempts" not in _table_names():
        return
    columns = _column_names("payment_attempts")
    if "external_user_ref" in columns:
        op.drop_column("payment_attempts", "external_user_ref")
