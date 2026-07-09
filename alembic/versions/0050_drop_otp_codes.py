"""drop otp_codes — the VerifyWay OTP flow went stateless

Revision ID: 0050_drop_otp_codes
Revises: 0049_create_otp_codes
Create Date: 2026-07-09 00:00:00

verifyway.py no longer stores OTP state anywhere: the code is mixed into the
HMAC signature of a client-held challenge token, so verification re-derives
validity instead of reading a row. The table created by 0049 (which briefly
ran in production) is removed.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0050_drop_otp_codes"
down_revision = "0049_create_otp_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "otp_codes" not in set(inspector.get_table_names()):
        return
    op.drop_table("otp_codes")


def downgrade() -> None:
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
