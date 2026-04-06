"""create esim profile tracking tables

Revision ID: 0004_profile_tracking
Revises: 0003_customer_orders
Create Date: 2026-04-05 02:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_profile_tracking"
down_revision = "0003_customer_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

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

    op.create_table(
        "esim_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("esim_tran_no", sa.String(length=120), nullable=True),
        sa.Column("iccid", sa.String(length=120), nullable=True),
        sa.Column("imsi", sa.String(length=120), nullable=True),
        sa.Column("msisdn", sa.String(length=120), nullable=True),
        sa.Column("activation_code", sa.Text(), nullable=True),
        sa.Column("qr_code_url", sa.Text(), nullable=True),
        sa.Column("install_url", sa.Text(), nullable=True),
        sa.Column("provider_status", sa.String(length=80), nullable=True),
        sa.Column("app_status", sa.String(length=80), nullable=True),
        sa.Column("installed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("data_type", sa.String(length=80), nullable=True),
        sa.Column("total_data_mb", sa.Integer(), nullable=True),
        sa.Column("used_data_mb", sa.Integer(), nullable=True),
        sa.Column("remaining_data_mb", sa.Integer(), nullable=True),
        sa.Column("validity_days", sa.Integer(), nullable=True),
        sa.Column("package_code", sa.String(length=120), nullable=True),
        sa.Column("package_name", sa.String(length=255), nullable=True),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("country_name", sa.String(length=255), nullable=True),
        sa.Column("booked_via_platform", sa.String(length=80), nullable=True),
        sa.Column("canceled_via_platform", sa.String(length=80), nullable=True),
        sa.Column("refunded_via_platform", sa.String(length=80), nullable=True),
        sa.Column("revoked_via_platform", sa.String(length=80), nullable=True),
        sa.Column("suspended_via_platform", sa.String(length=80), nullable=True),
        sa.Column("unsuspended_via_platform", sa.String(length=80), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsuspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_provider_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], name="fk_esim_profiles_order_item_id", ondelete="set null"),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], name="fk_esim_profiles_user_id", ondelete="set null"),
        sa.UniqueConstraint("esim_tran_no", name="uq_esim_profiles_esim_tran_no"),
        sa.UniqueConstraint("iccid", name="uq_esim_profiles_iccid"),
    )
    op.create_index("ix_esim_profiles_order_item_id", "esim_profiles", ["order_item_id"], unique=False)
    op.create_index("ix_esim_profiles_user_id", "esim_profiles", ["user_id"], unique=False)
    op.create_index("ix_esim_profiles_esim_tran_no", "esim_profiles", ["esim_tran_no"], unique=False)
    op.create_index("ix_esim_profiles_iccid", "esim_profiles", ["iccid"], unique=False)
    op.create_index("ix_esim_profiles_app_status", "esim_profiles", ["app_status"], unique=False)
    op.create_index("ix_esim_profiles_package_code", "esim_profiles", ["package_code"], unique=False)
    op.create_index("ix_esim_profiles_country_code", "esim_profiles", ["country_code"], unique=False)

    op.create_table(
        "esim_lifecycle_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("customer_order_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("service_type", sa.String(length=32), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=True),
        sa.Column("actor_phone", sa.String(length=64), nullable=True),
        sa.Column("platform_code", sa.String(length=80), nullable=True),
        sa.Column("status_before", sa.String(length=80), nullable=True),
        sa.Column("status_after", sa.String(length=80), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["customer_order_id"], ["customer_orders.id"], name="fk_esim_lifecycle_events_customer_order_id", ondelete="set null"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], name="fk_esim_lifecycle_events_order_item_id", ondelete="set null"),
        sa.ForeignKeyConstraint(["profile_id"], ["esim_profiles.id"], name="fk_esim_lifecycle_events_profile_id", ondelete="set null"),
    )
    op.create_index("ix_esim_lifecycle_events_customer_order_id", "esim_lifecycle_events", ["customer_order_id"], unique=False)
    op.create_index("ix_esim_lifecycle_events_order_item_id", "esim_lifecycle_events", ["order_item_id"], unique=False)
    op.create_index("ix_esim_lifecycle_events_profile_id", "esim_lifecycle_events", ["profile_id"], unique=False)
    op.create_index("ix_esim_lifecycle_events_service_type", "esim_lifecycle_events", ["service_type"], unique=False)
    op.create_index("ix_esim_lifecycle_events_event_type", "esim_lifecycle_events", ["event_type"], unique=False)
    op.create_index("ix_esim_lifecycle_events_actor_phone", "esim_lifecycle_events", ["actor_phone"], unique=False)

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

    if dialect_name == "postgresql":
        for table_name in (
            "provider_field_rules",
            "esim_profiles",
            "esim_lifecycle_events",
            "provider_payload_snapshots",
        ):
            op.execute(f"drop trigger if exists trg_{table_name}_updated_at on public.{table_name};")
            op.execute(
                f"""
                create trigger trg_{table_name}_updated_at
                before update on public.{table_name}
                for each row
                execute function public.set_updated_at();
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        for table_name in (
            "provider_payload_snapshots",
            "esim_lifecycle_events",
            "esim_profiles",
            "provider_field_rules",
        ):
            op.execute(f"drop trigger if exists trg_{table_name}_updated_at on public.{table_name};")

    op.drop_index("ix_provider_payload_snapshots_profile_id", table_name="provider_payload_snapshots")
    op.drop_index("ix_provider_payload_snapshots_order_item_id", table_name="provider_payload_snapshots")
    op.drop_index("ix_provider_payload_snapshots_customer_order_id", table_name="provider_payload_snapshots")
    op.drop_index("ix_provider_payload_snapshots_entity_type", table_name="provider_payload_snapshots")
    op.drop_index("ix_provider_payload_snapshots_provider", table_name="provider_payload_snapshots")
    op.drop_table("provider_payload_snapshots")

    op.drop_index("ix_esim_lifecycle_events_actor_phone", table_name="esim_lifecycle_events")
    op.drop_index("ix_esim_lifecycle_events_event_type", table_name="esim_lifecycle_events")
    op.drop_index("ix_esim_lifecycle_events_service_type", table_name="esim_lifecycle_events")
    op.drop_index("ix_esim_lifecycle_events_profile_id", table_name="esim_lifecycle_events")
    op.drop_index("ix_esim_lifecycle_events_order_item_id", table_name="esim_lifecycle_events")
    op.drop_index("ix_esim_lifecycle_events_customer_order_id", table_name="esim_lifecycle_events")
    op.drop_table("esim_lifecycle_events")

    op.drop_index("ix_esim_profiles_country_code", table_name="esim_profiles")
    op.drop_index("ix_esim_profiles_package_code", table_name="esim_profiles")
    op.drop_index("ix_esim_profiles_app_status", table_name="esim_profiles")
    op.drop_index("ix_esim_profiles_iccid", table_name="esim_profiles")
    op.drop_index("ix_esim_profiles_esim_tran_no", table_name="esim_profiles")
    op.drop_index("ix_esim_profiles_user_id", table_name="esim_profiles")
    op.drop_index("ix_esim_profiles_order_item_id", table_name="esim_profiles")
    op.drop_table("esim_profiles")

    op.drop_index("ix_provider_field_rules_entity_type", table_name="provider_field_rules")
    op.drop_index("ix_provider_field_rules_provider", table_name="provider_field_rules")
    op.drop_table("provider_field_rules")
