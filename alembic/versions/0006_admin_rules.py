"""create admin pricing and merchandising tables

Revision ID: 0006_admin_rules
Revises: 0005_esim_profile_cleanup
Create Date: 2026-04-05 03:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_admin_rules"
down_revision = "0005_esim_profile_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("base_currency", sa.String(length=8), nullable=False),
        sa.Column("quote_currency", sa.String(length=8), nullable=False),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_exchange_rates_base_currency", "exchange_rates", ["base_currency"], unique=False)
    op.create_index("ix_exchange_rates_quote_currency", "exchange_rates", ["quote_currency"], unique=False)

    op.create_table(
        "pricing_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("service_type", sa.String(length=32), nullable=False, server_default="esim"),
        sa.Column("rule_scope", sa.String(length=32), nullable=False, server_default="global"),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("package_code", sa.String(length=120), nullable=True),
        sa.Column("provider_code", sa.String(length=80), nullable=True),
        sa.Column("adjustment_type", sa.String(length=16), nullable=False, server_default="percent"),
        sa.Column("adjustment_value", sa.Float(), nullable=False),
        sa.Column("applies_to", sa.String(length=32), nullable=False, server_default="provider_cost"),
        sa.Column("currency_code", sa.String(length=8), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pricing_rules_service_type", "pricing_rules", ["service_type"], unique=False)
    op.create_index("ix_pricing_rules_rule_scope", "pricing_rules", ["rule_scope"], unique=False)
    op.create_index("ix_pricing_rules_country_code", "pricing_rules", ["country_code"], unique=False)
    op.create_index("ix_pricing_rules_package_code", "pricing_rules", ["package_code"], unique=False)
    op.create_index("ix_pricing_rules_provider_code", "pricing_rules", ["provider_code"], unique=False)

    op.create_table(
        "discount_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("service_type", sa.String(length=32), nullable=False, server_default="esim"),
        sa.Column("rule_scope", sa.String(length=32), nullable=False, server_default="global"),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("package_code", sa.String(length=120), nullable=True),
        sa.Column("provider_code", sa.String(length=80), nullable=True),
        sa.Column("discount_type", sa.String(length=16), nullable=False, server_default="percent"),
        sa.Column("discount_value", sa.Float(), nullable=False),
        sa.Column("applies_to", sa.String(length=32), nullable=False, server_default="provider_cost"),
        sa.Column("currency_code", sa.String(length=8), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_discount_rules_service_type", "discount_rules", ["service_type"], unique=False)
    op.create_index("ix_discount_rules_rule_scope", "discount_rules", ["rule_scope"], unique=False)
    op.create_index("ix_discount_rules_country_code", "discount_rules", ["country_code"], unique=False)
    op.create_index("ix_discount_rules_package_code", "discount_rules", ["package_code"], unique=False)
    op.create_index("ix_discount_rules_provider_code", "discount_rules", ["provider_code"], unique=False)

    op.create_table(
        "featured_locations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("service_type", sa.String(length=32), nullable=False, server_default="esim"),
        sa.Column("location_type", sa.String(length=32), nullable=False, server_default="country"),
        sa.Column("badge_text", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_popular", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_featured_locations_code", "featured_locations", ["code"], unique=False)
    op.create_index("ix_featured_locations_service_type", "featured_locations", ["service_type"], unique=False)

    if dialect_name == "postgresql":
        for table_name in ("exchange_rates", "pricing_rules", "discount_rules", "featured_locations"):
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
        for table_name in ("featured_locations", "discount_rules", "pricing_rules", "exchange_rates"):
            op.execute(f"drop trigger if exists trg_{table_name}_updated_at on public.{table_name};")

    op.drop_index("ix_featured_locations_service_type", table_name="featured_locations")
    op.drop_index("ix_featured_locations_code", table_name="featured_locations")
    op.drop_table("featured_locations")

    op.drop_index("ix_discount_rules_provider_code", table_name="discount_rules")
    op.drop_index("ix_discount_rules_package_code", table_name="discount_rules")
    op.drop_index("ix_discount_rules_country_code", table_name="discount_rules")
    op.drop_index("ix_discount_rules_rule_scope", table_name="discount_rules")
    op.drop_index("ix_discount_rules_service_type", table_name="discount_rules")
    op.drop_table("discount_rules")

    op.drop_index("ix_pricing_rules_provider_code", table_name="pricing_rules")
    op.drop_index("ix_pricing_rules_package_code", table_name="pricing_rules")
    op.drop_index("ix_pricing_rules_country_code", table_name="pricing_rules")
    op.drop_index("ix_pricing_rules_rule_scope", table_name="pricing_rules")
    op.drop_index("ix_pricing_rules_service_type", table_name="pricing_rules")
    op.drop_table("pricing_rules")

    op.drop_index("ix_exchange_rates_quote_currency", table_name="exchange_rates")
    op.drop_index("ix_exchange_rates_base_currency", table_name="exchange_rates")
    op.drop_table("exchange_rates")
