"""backfill admin_users permission flags so SEC-3 enforcement is additive

Revision ID: 0046_backfill_admin_permission_flags
Revises: 0045_esim_webhook_notify_id_index
Create Date: 2026-06-29 16:30:00

Until now the granular admin permission flags (``can_manage_users`` / ``orders``
/ ``pricing`` / ``content`` / ``can_send_push``) were stored and returned to the
client but never enforced server-side — every authenticated admin could call
every admin route. SEC-3 adds per-route ``_require_permission(...)`` gates in
``admin.py``.

Those columns default to ``False``, so turning on enforcement would instantly
lock every existing non-owner admin out of every gated route. To make the change
ADDITIVE (existing admins keep exactly the access they had; granular control
applies to newly-created/edited admins going forward), this grants all five
flags to existing admins that currently have NONE set. ``owner`` / ``super_admin``
bypass the granular flags in code, so they are left untouched.

Idempotent: the ``all-flags-false`` predicate excludes any admin already granted,
so re-running is a no-op. Postgres in practice — the SQLite test DB builds its
schema from the models and never runs migrations.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0046_backfill_admin_permission_flags"
down_revision = "0045_esim_webhook_notify_id_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE admin_users
            SET can_manage_users = true,
                can_manage_orders = true,
                can_manage_pricing = true,
                can_manage_content = true,
                can_send_push = true
            WHERE lower(coalesce(role, '')) NOT IN ('owner', 'super_admin')
              AND can_manage_users = false
              AND can_manage_orders = false
              AND can_manage_pricing = false
              AND can_manage_content = false
              AND can_send_push = false
            """
        )
    )


def downgrade() -> None:
    # Reverting would re-lock admins this migration intentionally grandfathered
    # in; there is no safe way to tell granted-by-backfill from granted-by-admin.
    pass
