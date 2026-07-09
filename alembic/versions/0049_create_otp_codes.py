"""create otp_codes (VerifyWay OTP state, one row per phone)

Revision ID: 0049_create_otp_codes
Revises: 0048_backfill_store_urls
Create Date: 2026-07-09 00:00:00

Production runs uvicorn with multiple workers (Dockerfile WEB_CONCURRENCY=2),
so verifyway.py's OTP send/verify state must be shared across processes — an
in-memory store fails whenever verify lands on a different worker than send.
Stores only the HMAC digest of the code, never plaintext.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0049_create_otp_codes"
down_revision = "0048_backfill_store_urls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "otp_codes" in set(inspector.get_table_names()):
        return
    op.create_table(
        "otp_codes",
        sa.Column("phone", sa.String(length=64), primary_key=True),
        sa.Column("code_digest", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts_left", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "otp_codes" not in set(inspector.get_table_names()):
        return
    op.drop_table("otp_codes")
