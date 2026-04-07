"""enforce successful payment_attempt policy and add hot-path indexes

Revision ID: 0016_payment_policy_indexes
Revises: 0015_payment_attempt_admin_owner
Create Date: 2026-04-08 01:25:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_payment_policy_indexes"
down_revision = "0015_payment_attempt_admin_owner"
branch_labels = None
depends_on = None

SUCCESS_ONLY_CHECK = "ck_payment_attempts_success_only"
EXCHANGE_LOOKUP_INDEX = "ix_exchange_rates_lookup_active"
PRICING_LOOKUP_INDEX = "ix_pricing_rules_active_scope"
DISCOUNT_LOOKUP_INDEX = "ix_discount_rules_active_scope"


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _check_constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {item["name"] for item in inspector.get_check_constraints(table_name) if item.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names()
    if "payment_attempts" in tables:
        # Product policy: payment_attempts should only persist successful attempts.
        bind.execute(sa.text("DELETE FROM payment_attempts WHERE status NOT IN ('paid', 'refunded')"))

        if bind.dialect.name != "sqlite" and SUCCESS_ONLY_CHECK not in _check_constraint_names("payment_attempts"):
            op.create_check_constraint(
                SUCCESS_ONLY_CHECK,
                "payment_attempts",
                "status IN ('paid', 'refunded')",
            )

    if "exchange_rates" in tables and EXCHANGE_LOOKUP_INDEX not in _index_names("exchange_rates"):
        op.create_index(
            EXCHANGE_LOOKUP_INDEX,
            "exchange_rates",
            ["base_currency", "quote_currency", "active", "effective_at"],
            unique=False,
        )

    if "pricing_rules" in tables and PRICING_LOOKUP_INDEX not in _index_names("pricing_rules"):
        op.create_index(
            PRICING_LOOKUP_INDEX,
            "pricing_rules",
            [
                "service_type",
                "active",
                "rule_scope",
                "package_code",
                "country_code",
                "provider_code",
                "priority",
                "created_at",
            ],
            unique=False,
        )

    if "discount_rules" in tables and DISCOUNT_LOOKUP_INDEX not in _index_names("discount_rules"):
        op.create_index(
            DISCOUNT_LOOKUP_INDEX,
            "discount_rules",
            [
                "service_type",
                "active",
                "rule_scope",
                "package_code",
                "country_code",
                "provider_code",
                "priority",
                "created_at",
            ],
            unique=False,
        )


def downgrade() -> None:
    tables = _table_names()
    if "discount_rules" in tables and DISCOUNT_LOOKUP_INDEX in _index_names("discount_rules"):
        op.drop_index(DISCOUNT_LOOKUP_INDEX, table_name="discount_rules")
    if "pricing_rules" in tables and PRICING_LOOKUP_INDEX in _index_names("pricing_rules"):
        op.drop_index(PRICING_LOOKUP_INDEX, table_name="pricing_rules")
    if "exchange_rates" in tables and EXCHANGE_LOOKUP_INDEX in _index_names("exchange_rates"):
        op.drop_index(EXCHANGE_LOOKUP_INDEX, table_name="exchange_rates")
    bind = op.get_bind()
    if (
        "payment_attempts" in tables
        and bind.dialect.name != "sqlite"
        and SUCCESS_ONLY_CHECK in _check_constraint_names("payment_attempts")
    ):
        op.drop_constraint(SUCCESS_ONLY_CHECK, "payment_attempts", type_="check")
