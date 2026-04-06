"""create esim_orders

Revision ID: 0002_create_esim_orders
Revises: 0001_baseline_app_users
Create Date: 2026-04-05 00:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_create_esim_orders"
down_revision = "0001_baseline_app_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    op.create_table(
        "esim_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False, server_default="esim_access"),
        sa.Column("provider_order_no", sa.String(length=120), nullable=True),
        sa.Column("provider_transaction_id", sa.String(length=255), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("purchase_channel", sa.String(length=80), nullable=True),
        sa.Column("booked_via_platform", sa.String(length=80), nullable=True),
        sa.Column("canceled_via_platform", sa.String(length=80), nullable=True),
        sa.Column("refunded_via_platform", sa.String(length=80), nullable=True),
        sa.Column("revoked_via_platform", sa.String(length=80), nullable=True),
        sa.Column("provider_status", sa.String(length=80), nullable=True),
        sa.Column("lifecycle_status", sa.String(length=80), nullable=True),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("country_name", sa.String(length=255), nullable=True),
        sa.Column("package_code", sa.String(length=120), nullable=True),
        sa.Column("package_slug", sa.String(length=120), nullable=True),
        sa.Column("package_name", sa.String(length=255), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("currency_code", sa.String(length=8), nullable=True),
        sa.Column("exchange_rate", sa.Float(), nullable=True),
        sa.Column("provider_price_minor", sa.Integer(), nullable=True),
        sa.Column("sale_price_minor", sa.Integer(), nullable=True),
        sa.Column("refund_amount_minor", sa.Integer(), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_provider_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("quantity > 0", name="ck_esim_orders_quantity_positive"),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], name="fk_esim_orders_user_id", ondelete="set null"),
        sa.UniqueConstraint("provider_order_no", name="uq_esim_orders_provider_order_no"),
        sa.UniqueConstraint("provider_transaction_id", name="uq_esim_orders_provider_transaction_id"),
    )
    op.create_index("ix_esim_orders_provider_order_no", "esim_orders", ["provider_order_no"], unique=False)
    op.create_index("ix_esim_orders_provider_transaction_id", "esim_orders", ["provider_transaction_id"], unique=False)
    op.create_index("ix_esim_orders_lifecycle_status", "esim_orders", ["lifecycle_status"], unique=False)
    op.create_index("ix_esim_orders_country_code", "esim_orders", ["country_code"], unique=False)
    op.create_index("ix_esim_orders_package_code", "esim_orders", ["package_code"], unique=False)
    op.create_index("ix_esim_orders_user_id", "esim_orders", ["user_id"], unique=False)

    if dialect_name == "postgresql":
        op.execute("drop trigger if exists trg_esim_orders_updated_at on public.esim_orders;")
        op.execute(
            """
            create trigger trg_esim_orders_updated_at
            before update on public.esim_orders
            for each row
            execute function public.set_updated_at();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        op.execute("drop trigger if exists trg_esim_orders_updated_at on public.esim_orders;")

    op.drop_index("ix_esim_orders_user_id", table_name="esim_orders")
    op.drop_index("ix_esim_orders_package_code", table_name="esim_orders")
    op.drop_index("ix_esim_orders_country_code", table_name="esim_orders")
    op.drop_index("ix_esim_orders_lifecycle_status", table_name="esim_orders")
    op.drop_index("ix_esim_orders_provider_transaction_id", table_name="esim_orders")
    op.drop_index("ix_esim_orders_provider_order_no", table_name="esim_orders")
    op.drop_table("esim_orders")
