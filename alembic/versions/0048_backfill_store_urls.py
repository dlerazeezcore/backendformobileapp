"""Backfill app_release_info store URLs (iOS + Android)

Revision ID: 0048_backfill_store_urls
Revises: 0047_app_user_app_version
Create Date: 2026-07-05 00:00:00

The in-app "update available" modal and the app-update push both read the store
URLs from the ``app_release_info`` singleton (id=1). The iOS App Store URL was
never set, so iPhone users saw an "Update now" button that opened nothing and the
admin panel showed "iOS: (not set)". Backfill BOTH URLs where empty/NULL — this
never clobbers a value an admin already set.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0048_backfill_store_urls"
down_revision = "0047_app_user_app_version"
branch_labels = None
depends_on = None

APP_STORE_URL = "https://apps.apple.com/us/app/tulip-booking/id6759516330"
PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.theesim.app&hl=en-US"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE app_release_info SET app_store_url = :url "
            "WHERE id = 1 AND (app_store_url IS NULL OR app_store_url = '')"
        ),
        {"url": APP_STORE_URL},
    )
    bind.execute(
        sa.text(
            "UPDATE app_release_info SET play_store_url = :url "
            "WHERE id = 1 AND (play_store_url IS NULL OR play_store_url = '')"
        ),
        {"url": PLAY_STORE_URL},
    )


def downgrade() -> None:
    # Data-only backfill — nothing structural to revert.
    pass
