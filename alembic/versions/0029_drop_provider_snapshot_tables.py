"""drop unused provider_payload_snapshots and provider_field_rules tables

Revision ID: 0029_drop_provider_snapshot_tables
Revises: 0028_drop_featured_locations_badge_text
Create Date: 2026-05-05 17:40:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_drop_provider_snapshot_tables"
down_revision = "0028_drop_featured_locations_badge_text"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if table_name not in _table_names():
        return
    if index_name not in _index_names(table_name):
        return
    op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    tables = _table_names()

    if "provider_payload_snapshots" in tables:
        if dialect_name == "postgresql":
            op.execute("drop trigger if exists trg_provider_payload_snapshots_updated_at on public.provider_payload_snapshots;")
        for index_name in (
            "ix_provider_payload_snapshots_profile_id",
            "ix_provider_payload_snapshots_order_item_id",
            "ix_provider_payload_snapshots_customer_order_id",
            "ix_provider_payload_snapshots_entity_type",
            "ix_provider_payload_snapshots_provider",
        ):
            _drop_index_if_exists("provider_payload_snapshots", index_name)
        op.drop_table("provider_payload_snapshots")

    tables = _table_names()
    if "provider_field_rules" in tables:
        if dialect_name == "postgresql":
            op.execute("drop trigger if exists trg_provider_field_rules_updated_at on public.provider_field_rules;")
        for index_name in (
            "ix_provider_field_rules_entity_type",
            "ix_provider_field_rules_provider",
        ):
            _drop_index_if_exists("provider_field_rules", index_name)
        op.drop_table("provider_field_rules")


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    tables = _table_names()

    if "provider_field_rules" not in tables:
        op.create_table(
            "provider_field_rules",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False, server_default="esim_access"),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("field_paths", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        )
        op.create_index("ix_provider_field_rules_provider", "provider_field_rules", ["provider"], unique=False)
        op.create_index("ix_provider_field_rules_entity_type", "provider_field_rules", ["entity_type"], unique=False)

    tables = _table_names()
    if "provider_payload_snapshots" not in tables:
        op.create_table(
            "provider_payload_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False, server_default="esim_access"),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("direction", sa.String(length=32), nullable=False, server_default="response"),
            sa.Column("customer_order_id", sa.Integer(), nullable=True),
            sa.Column("order_item_id", sa.Integer(), nullable=True),
            sa.Column("profile_id", sa.Integer(), nullable=True),
            sa.Column("selected_field_paths", sa.JSON(), nullable=False),
            sa.Column("filtered_payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["customer_order_id"], ["customer_orders.id"], name="fk_provider_payload_snapshots_customer_order_id", ondelete="set null"),
            sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], name="fk_provider_payload_snapshots_order_item_id", ondelete="set null"),
            sa.ForeignKeyConstraint(["profile_id"], ["esim_profiles.id"], name="fk_provider_payload_snapshots_profile_id", ondelete="set null"),
        )
        op.create_index("ix_provider_payload_snapshots_provider", "provider_payload_snapshots", ["provider"], unique=False)
        op.create_index("ix_provider_payload_snapshots_entity_type", "provider_payload_snapshots", ["entity_type"], unique=False)
        op.create_index("ix_provider_payload_snapshots_customer_order_id", "provider_payload_snapshots", ["customer_order_id"], unique=False)
        op.create_index("ix_provider_payload_snapshots_order_item_id", "provider_payload_snapshots", ["order_item_id"], unique=False)
        op.create_index("ix_provider_payload_snapshots_profile_id", "provider_payload_snapshots", ["profile_id"], unique=False)
