"""allow anonymous push device ownership

Revision ID: 0019_push_allow_anonymous
Revises: 0018_push_device_owner
Create Date: 2026-04-08 19:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_push_allow_anonymous"
down_revision = "0018_push_device_owner"
branch_labels = None
depends_on = None


OLD_CONSTRAINT = (
    "(user_id IS NOT NULL AND admin_user_id IS NULL) OR "
    "(user_id IS NULL AND admin_user_id IS NOT NULL)"
)
NEW_CONSTRAINT = (
    "(user_id IS NOT NULL AND admin_user_id IS NULL) OR "
    "(user_id IS NULL AND admin_user_id IS NOT NULL) OR "
    "(user_id IS NULL AND admin_user_id IS NULL)"
)


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _check_constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {constraint["name"] for constraint in inspector.get_check_constraints(table_name)}


def upgrade() -> None:
    if "push_devices" not in _table_names():
        return
    constraint_names = _check_constraint_names("push_devices")
    if "ck_push_devices_has_owner" not in constraint_names:
        return
    with op.batch_alter_table("push_devices") as batch_op:
        batch_op.drop_constraint("ck_push_devices_has_owner", type_="check")
        batch_op.create_check_constraint("ck_push_devices_has_owner", NEW_CONSTRAINT)


def downgrade() -> None:
    if "push_devices" not in _table_names():
        return
    constraint_names = _check_constraint_names("push_devices")
    if "ck_push_devices_has_owner" not in constraint_names:
        return
    with op.batch_alter_table("push_devices") as batch_op:
        batch_op.drop_constraint("ck_push_devices_has_owner", type_="check")
        batch_op.create_check_constraint("ck_push_devices_has_owner", OLD_CONSTRAINT)
