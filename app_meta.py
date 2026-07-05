"""App release / version info endpoints.

- GET  /api/v1/app/version-info        — public, called by the mobile app on boot
                                         and resume to decide whether to show an
                                         in-app update modal.
- PUT  /api/v1/admin/app/version-info  — admin-only, updates the singleton row.

The data lives in the `app_release_info` table (single row, id=1). Created and
seeded by Alembic migration 0040.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_token_claims, require_active_subject
from supabase_store import AdminUser, AppReleaseInfo, SupabaseStore, utcnow


# Canonical production store listings. Used as the seed defaults for the
# app_release_info singleton and backfilled onto existing rows by migration 0048,
# so the in-app update modal and the app-update push always have a link on BOTH
# stores (a missing iOS URL left iPhone users with a dead "Update now" button).
DEFAULT_APP_STORE_URL = "https://apps.apple.com/us/app/tulip-booking/id6759516330"
DEFAULT_PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.theesim.app&hl=en-US"


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AppVersionInfoUpdatePayload(_Model):
    latest_version: str | None = Field(default=None, max_length=32, alias="latestVersion")
    min_supported_version: str | None = Field(default=None, max_length=32, alias="minSupportedVersion")
    app_store_url: str | None = Field(default=None, max_length=512, alias="appStoreUrl")
    play_store_url: str | None = Field(default=None, max_length=512, alias="playStoreUrl")
    release_notes_en: str | None = Field(default=None, alias="releaseNotesEn")
    release_notes_ar: str | None = Field(default=None, alias="releaseNotesAr")
    release_notes_ku: str | None = Field(default=None, alias="releaseNotesKu")


def _serialize(row: AppReleaseInfo) -> dict[str, Any]:
    return {
        "latestVersion": row.latest_version,
        "minSupportedVersion": row.min_supported_version,
        "appStoreUrl": row.app_store_url or None,
        "playStoreUrl": row.play_store_url or None,
        "releaseNotes": {
            "en": row.release_notes_en or None,
            "ar": row.release_notes_ar or None,
            "ku": row.release_notes_ku or None,
        },
        "updatedAt": row.updated_at,
    }


def _get_or_create(db: Session) -> AppReleaseInfo:
    row = db.scalar(select(AppReleaseInfo).where(AppReleaseInfo.id == 1))
    if row is None:
        row = AppReleaseInfo(
            id=1,
            latest_version="1.0.0",
            min_supported_version="1.0.0",
            app_store_url=DEFAULT_APP_STORE_URL,
            play_store_url=DEFAULT_PLAY_STORE_URL,
        )
        db.add(row)
        db.flush()
    return row


def register_app_meta_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    def _require_permission(flag: str) -> Callable[..., AdminUser]:
        """SEC-3: per-route admin permission gate (mirrors admin.py). ``owner``/
        ``super_admin`` bypass the granular flags; every other admin must have
        the specific permission column (e.g. ``can_manage_content``) set,
        enforced server-side rather than relying on the client to hide UI."""

        def _dep(
            claims: dict[str, Any] = Depends(get_token_claims),
            db: Session = Depends(get_db),
        ) -> AdminUser:
            row = require_active_subject(db, claims=claims, subject_type="admin")
            assert isinstance(row, AdminUser)
            if (row.role or "").strip().lower() in {"super_admin", "owner"}:
                return row
            if not getattr(row, flag, False):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Admin permission '{flag}' is required.",
                )
            return row

        return _dep

    @app.get("/api/v1/app/version-info")
    def get_version_info(db: Session = Depends(get_db)) -> dict[str, Any]:
        row = _get_or_create(db)
        db.commit()
        return _serialize(row)

    @app.get("/api/v1/currencies")
    def get_currencies(response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
        """Universal currency set for the app: pure FX rates (IQD per 1 unit) for
        every enabled display currency + the IQD settlement base. `esimPricing`
        carries the (service-scoped) global eSIM markup the client uses to compute
        eSIM display prices. FX is universal; markup is eSIM-only (and future
        per-country/class via pricing_rules)."""
        store = SupabaseStore(db)
        currencies = store.get_display_currencies()
        markup_percent = store.get_global_esim_markup_percent()
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        return {
            "baseCurrency": "IQD",
            "currencies": currencies,
            "esimPricing": {"markupPercent": markup_percent},
        }

    @app.put("/api/v1/admin/app/version-info")
    def update_version_info(
        payload: AppVersionInfoUpdatePayload,
        db: Session = Depends(get_db),
        # SEC-3: publishing latestVersion drives the mandatory-update modal —
        # content publishing, so it needs the content grant.
        _: AdminUser = Depends(_require_permission("can_manage_content")),
    ) -> dict[str, Any]:
        row = _get_or_create(db)
        provided = payload.model_fields_set
        changed = False

        def _set(field: str, value: Any, *, max_len: int | None = None) -> None:
            nonlocal changed
            cleaned = str(value or "").strip()
            if max_len is not None and len(cleaned) > max_len:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{field} is too long (max {max_len}).",
                )
            current = getattr(row, field)
            if cleaned != (current or ""):
                setattr(row, field, cleaned or "")
                changed = True

        def _set_optional(field: str, value: Any) -> None:
            nonlocal changed
            cleaned = str(value or "").strip() or None
            current = getattr(row, field)
            if cleaned != current:
                setattr(row, field, cleaned)
                changed = True

        if "latest_version" in provided:
            _set("latest_version", payload.latest_version or "1.0.0", max_len=32)
        if "min_supported_version" in provided:
            _set("min_supported_version", payload.min_supported_version or "1.0.0", max_len=32)
        if "app_store_url" in provided:
            _set("app_store_url", payload.app_store_url or "", max_len=512)
        if "play_store_url" in provided:
            _set("play_store_url", payload.play_store_url or "", max_len=512)
        if "release_notes_en" in provided:
            _set_optional("release_notes_en", payload.release_notes_en)
        if "release_notes_ar" in provided:
            _set_optional("release_notes_ar", payload.release_notes_ar)
        if "release_notes_ku" in provided:
            _set_optional("release_notes_ku", payload.release_notes_ku)

        if changed:
            row.updated_at = utcnow()
        db.commit()
        db.refresh(row)
        return _serialize(row)
