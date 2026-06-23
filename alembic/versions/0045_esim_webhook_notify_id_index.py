"""partial functional index on esim_lifecycle_events (payload->>'notifyId')

Revision ID: 0045_esim_webhook_notify_id_index
Revises: 0044_backfill_terminal_timestamps
Create Date: 2026-06-24 00:00:00

`record_webhook` dedups eSIM Access webhook replays on the stable ``notifyId``
carried in the stored event payload (see SupabaseStore.record_webhook). That
lookup filters ``source = 'provider_webhook'`` and ``payload ->> 'notifyId'``.
Without an index it is a sequential scan of the append-only lifecycle-event
table, which is fine at today's volume but degrades as the table grows.

This adds a partial functional index covering exactly that predicate so the
idempotency lookup stays O(log n). Postgres-only — the expression/partial index
syntax is not portable, and the SQLite test database builds its schema from the
models via ``Base.metadata.create_all`` (it never runs this migration).

Idempotent-friendly: guarded on the dialect so a non-Postgres bind is a no-op.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0045_esim_webhook_notify_id_index"
down_revision = "0044_backfill_terminal_timestamps"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_esim_lifecycle_events_webhook_notify_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.create_index(
        INDEX_NAME,
        "esim_lifecycle_events",
        [sa.text("(payload ->> 'notifyId')")],
        unique=False,
        postgresql_where=sa.text("source = 'provider_webhook'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_index(INDEX_NAME, table_name="esim_lifecycle_events")
