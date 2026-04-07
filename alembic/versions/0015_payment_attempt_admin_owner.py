"""add admin owner to payment attempts and enforce owned payments

Revision ID: 0015_payment_attempt_admin_owner
Revises: 0014_add_external_user_ref
Create Date: 2026-04-08 02:35:00
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0015_payment_attempt_admin_owner"
down_revision = "0014_add_external_user_ref"
branch_labels = None
depends_on = None


CHECK_NAME = "ck_payment_attempts_has_owner"
INDEX_NAME = "ix_payment_attempts_admin_user_id"
FK_NAME = "fk_payment_attempts_admin_user_id_admin_users"


def _table_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _check_constraint_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {constraint["name"] for constraint in inspector.get_check_constraints(table_name) if constraint.get("name")}


def _has_admin_user_fk() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys("payment_attempts"):
        constrained = fk.get("constrained_columns") or []
        referred_table = fk.get("referred_table")
        referred_columns = fk.get("referred_columns") or []
        if constrained == ["admin_user_id"] and referred_table == "admin_users" and referred_columns == ["id"]:
            return True
    return False


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


def _maybe_uuid(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
    else:
        candidate = str(value).strip()
        if not candidate:
            return None
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        return None


def _resolve_admin_ref(metadata: dict[str, Any], external_user_ref: Any) -> str | None:
    for key in ("linkedAdminUserId", "adminUserId", "admin_user_id"):
        resolved = _maybe_uuid(metadata.get(key))
        if resolved:
            return resolved
    return _maybe_uuid(external_user_ref)


def upgrade() -> None:
    if "payment_attempts" not in _table_names() or "admin_users" not in _table_names():
        return

    bind = op.get_bind()

    columns = _column_names("payment_attempts")
    if "admin_user_id" not in columns:
        op.add_column("payment_attempts", sa.Column("admin_user_id", sa.Uuid(as_uuid=False), nullable=True))

    if bind.dialect.name != "sqlite" and not _has_admin_user_fk():
        op.create_foreign_key(
            FK_NAME,
            "payment_attempts",
            "admin_users",
            ["admin_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if INDEX_NAME not in _index_names("payment_attempts"):
        op.create_index(INDEX_NAME, "payment_attempts", ["admin_user_id"], unique=False)

    admin_ids = {
        str(row[0])
        for row in bind.execute(sa.text("SELECT id FROM admin_users")).all()
        if row and row[0] is not None
    }

    rows = bind.execute(
        sa.text(
            """
            SELECT id, user_id, admin_user_id, external_user_ref, metadata
            FROM payment_attempts
            """
        )
    ).mappings()

    for row in rows:
        if row.get("admin_user_id") is not None or row.get("user_id") is not None:
            continue
        metadata = _safe_json(row.get("metadata"))
        candidate_admin = _resolve_admin_ref(metadata, row.get("external_user_ref"))
        if candidate_admin and candidate_admin in admin_ids:
            bind.execute(
                sa.text("UPDATE payment_attempts SET admin_user_id = :admin_id WHERE id = :id"),
                {"admin_id": candidate_admin, "id": row["id"]},
            )

    # Business rule: every payment attempt belongs to a logged-in subject.
    bind.execute(sa.text("DELETE FROM payment_attempts WHERE user_id IS NULL AND admin_user_id IS NULL"))

    if bind.dialect.name != "sqlite" and CHECK_NAME not in _check_constraint_names("payment_attempts"):
        op.create_check_constraint(
            CHECK_NAME,
            "payment_attempts",
            "(user_id IS NOT NULL) OR (admin_user_id IS NOT NULL)",
        )


def downgrade() -> None:
    if "payment_attempts" not in _table_names():
        return

    bind = op.get_bind()
    if bind.dialect.name != "sqlite" and CHECK_NAME in _check_constraint_names("payment_attempts"):
        op.drop_constraint(CHECK_NAME, "payment_attempts", type_="check")

    if INDEX_NAME in _index_names("payment_attempts"):
        op.drop_index(INDEX_NAME, table_name="payment_attempts")

    if bind.dialect.name != "sqlite" and _has_admin_user_fk():
        op.drop_constraint(FK_NAME, "payment_attempts", type_="foreignkey")

    columns = _column_names("payment_attempts")
    if "admin_user_id" in columns:
        op.drop_column("payment_attempts", "admin_user_id")
