from __future__ import annotations

import asyncio
from datetime import datetime
import hmac
import logging
import os
from time import monotonic
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status

from dependencies import get_provider as get_esim_provider
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth import get_token_claims, hash_password, require_active_subject
from phone_utils import phone_lookup_candidates
from users import _check_account_status
from supabase_store import (
    AdminUser,
    CustomerOrder,
    DiscountRule,
    ESimLifecycleEvent,
    ESimProfile,
    ExchangeRate,
    FeaturedLocation,
    OrderItem,
    PricingRule,
    PaymentAttempt,
    PaymentProviderEvent,
    SupabaseStore,
    normalize_esim_status,
    utcnow,
)

from esim_access_api import ActionContext

LOGGER = logging.getLogger("uvicorn.error")
_PUBLIC_FEATURED_LOCATIONS_CACHE: dict[str, list[dict[str, Any]]] = {}
_PUBLIC_FEATURED_LOCATIONS_RETRY_AFTER: dict[str, float] = {}
_PUBLIC_DB_FAILURE_BACKOFF_SECONDS = max(float(os.getenv("PUBLIC_DB_FAILURE_BACKOFF_SECONDS", "15")), 0.0)


def _featured_locations_cache_key(service_type: str) -> str:
    return str(service_type or "esim").strip().lower() or "esim"


def _clone_featured_locations(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(location) for location in locations]


class RefundPayload(BaseModel):
    iccid: str
    refund_amount_minor: int | None = Field(default=None, alias="refundAmountMinor")
    context: ActionContext


class ProfileStatePayload(BaseModel):
    iccid: str
    context: ActionContext


class PricingRulePayload(BaseModel):
    service_type: str = Field(default="esim", alias="serviceType")
    rule_scope: str = Field(default="global", alias="ruleScope")
    country_code: str | None = Field(default=None, alias="countryCode")
    package_code: str | None = Field(default=None, alias="packageCode")
    provider_code: str | None = Field(default=None, alias="providerCode")
    adjustment_type: str = Field(default="percent", alias="adjustmentType")
    adjustment_value: float = Field(alias="adjustmentValue")
    applies_to: str = Field(default="provider_cost", alias="appliesTo")
    currency_code: str | None = Field(default=None, alias="currencyCode")
    priority: int = 100
    active: bool = True
    starts_at: datetime | None = Field(default=None, alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    notes: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


class DiscountRulePayload(BaseModel):
    service_type: str = Field(default="esim", alias="serviceType")
    rule_scope: str = Field(default="global", alias="ruleScope")
    country_code: str | None = Field(default=None, alias="countryCode")
    package_code: str | None = Field(default=None, alias="packageCode")
    provider_code: str | None = Field(default=None, alias="providerCode")
    discount_type: str = Field(default="percent", alias="discountType")
    discount_value: float = Field(alias="discountValue")
    applies_to: str = Field(default="provider_cost", alias="appliesTo")
    currency_code: str | None = Field(default=None, alias="currencyCode")
    priority: int = 100
    active: bool = True
    starts_at: datetime | None = Field(default=None, alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    reason: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


class ExchangeRatePayload(BaseModel):
    base_currency: str = Field(alias="baseCurrency")
    quote_currency: str = Field(alias="quoteCurrency")
    rate: float
    source: str | None = None
    effective_at: datetime | None = Field(default=None, alias="effectiveAt")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    active: bool = True
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


# Persist only known admin roles — free-text roles would silently bypass the
# owner/super_admin gates in register_admin_routes.
_ALLOWED_ADMIN_ROLES = {"admin", "super_admin", "owner"}


def _check_admin_role(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in _ALLOWED_ADMIN_ROLES:
        raise ValueError(f"role must be one of {sorted(_ALLOWED_ADMIN_ROLES)}")
    return normalized


class AdminUserPayload(BaseModel):
    phone: str
    name: str
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)
    status: str = "active"
    role: str = "admin"
    can_manage_users: bool = Field(default=False, alias="canManageUsers")
    can_manage_orders: bool = Field(default=False, alias="canManageOrders")
    can_manage_pricing: bool = Field(default=False, alias="canManagePricing")
    can_manage_content: bool = Field(default=False, alias="canManageContent")
    can_send_push: bool = Field(default=False, alias="canSendPush")
    notes: str | None = None
    blocked_at: datetime | None = Field(default=None, alias="blockedAt")
    deleted_at: datetime | None = Field(default=None, alias="deletedAt")
    last_login_at: datetime | None = Field(default=None, alias="lastLoginAt")
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        return _check_account_status(v)

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        return _check_admin_role(v)


class FeaturedLocationPayload(BaseModel):
    code: str
    name: str
    service_type: str = Field(default="esim", alias="serviceType")
    location_type: str = Field(default="country", alias="locationType")
    sort_order: int = Field(default=0, alias="sortOrder")
    is_popular: bool = Field(default=True, alias="isPopular")
    enabled: bool = True
    starts_at: datetime | None = Field(default=None, alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


def register_admin_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    def _require_admin_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser:
        row = require_active_subject(db, claims=claims, subject_type="admin")
        assert isinstance(row, AdminUser)
        return row

    def _require_owner_or_super(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser:
        row = require_active_subject(db, claims=claims, subject_type="admin")
        assert isinstance(row, AdminUser)
        if (row.role or "").strip().lower() not in {"super_admin", "owner"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super_admin or owner can manage admin users",
            )
        return row

    def _require_permission(flag: str) -> Callable[..., AdminUser]:
        """SEC-3: per-route admin permission gate. ``owner``/``super_admin`` bypass
        the granular flags; every other admin must have the specific permission
        column (e.g. ``can_manage_pricing``) set, enforced server-side rather than
        relying on the client to hide UI."""

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

    def _serialize_admin_user(row: AdminUser) -> dict[str, Any]:
        return {
            "id": row.id,
            "phone": row.phone,
            "name": row.name,
            "email": row.email,
            "role": row.role,
            "status": row.status,
            "canSendPush": row.can_send_push,
            "canManageUsers": row.can_manage_users,
            "canManageOrders": row.can_manage_orders,
            "canManagePricing": row.can_manage_pricing,
            "canManageContent": row.can_manage_content,
            "createdAt": row.created_at,
            "updatedAt": row.updated_at,
        }

    @app.get("/api/v1/admin/admin-users")
    def list_admin_users(
        phone: str | None = None,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_owner_or_super),
    ) -> dict[str, Any]:
        from sqlalchemy import select as _select
        query = _select(AdminUser).order_by(AdminUser.created_at.desc()).limit(500)
        if phone:
            query = query.where(AdminUser.phone == phone.strip())
        rows = db.scalars(query).all()
        return {"admins": [_serialize_admin_user(r) for r in rows]}

    @app.post("/api/v1/admin/admin-users")
    def save_admin_user(
        payload: AdminUserPayload,
        db: Session = Depends(get_db),
        actor: AdminUser = Depends(_require_owner_or_super),
    ) -> dict[str, Any]:
        # BE-1: same owner/super gate as the GET/DELETE siblings. On top of it,
        # only an owner may assign the owner role or touch an owner account —
        # ensure_admin_user upserts by phone, so a super_admin could otherwise
        # replace/demote the owner.
        from sqlalchemy import select as _select

        actor_role = (actor.role or "").strip().lower()
        if actor_role != "owner":
            if payload.role == "owner":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only an owner can assign the owner role",
                )
            existing = db.scalar(
                _select(AdminUser).where(AdminUser.phone.in_(phone_lookup_candidates(payload.phone)))
            )
            if existing is not None and (existing.role or "").strip().lower() == "owner":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only an owner can modify an owner account",
                )
        data = payload.model_dump(by_alias=False)
        password = data.pop("password", None)
        if password is not None:
            data["password_hash"] = hash_password(password)
        admin_user = SupabaseStore(db).ensure_admin_user(**data)
        db.commit()
        db.refresh(admin_user)
        return {
            "adminUser": {
                "id": admin_user.id,
                "phone": admin_user.phone,
                "name": admin_user.name,
                "status": admin_user.status,
                "role": admin_user.role,
            }
        }

    @app.get("/api/v1/admin/users/{user_id}/push-devices")
    def list_user_push_devices(
        user_id: str,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_send_push")),
    ) -> dict[str, Any]:
        """Admin diagnostics: dump every push device row for one app user.

        Returns full token (for FCM/APNs distinction) + length + platform +
        app_version + last_seen so we can tell whether tokens are stale,
        which build they came from, and whether to deactivate them.
        """
        from supabase_store import PushDevice
        from sqlalchemy import select as _select
        rows = db.scalars(
            _select(PushDevice).where(PushDevice.user_id == user_id).order_by(PushDevice.updated_at.desc())
        ).all()
        return {
            "userId": user_id,
            "count": len(rows),
            "devices": [
                {
                    "id": r.id,
                    "token": r.token,
                    "tokenLen": len(r.token or ""),
                    "tokenLooksLikeApns": bool(r.token and len(r.token) <= 80 and ":" not in r.token),
                    "platform": r.platform,
                    "deviceId": r.device_id,
                    "appVersion": r.app_version,
                    "locale": r.locale,
                    "active": r.active,
                    "lastSeenAt": r.last_seen_at,
                    "createdAt": r.created_at,
                    "updatedAt": r.updated_at,
                    "customFields": r.custom_fields,
                }
                for r in rows
            ],
        }

    @app.post("/api/v1/admin/users/{user_id}/push-devices/purge-stale")
    def purge_stale_user_devices(
        user_id: str,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_send_push")),
    ) -> dict[str, Any]:
        """Deactivate any push device for the user that LOOKS like an APNs token
        (length <= 80 and no colon). Production FCM tokens are ~140-200 chars
        with a colon; APNs tokens are ~64-char hex. If a row is left over from
        a previous (non-rn-firebase) build, FCM accepts it but APNs drops the
        push silently — visible to the user as 'success but no banner'.
        """
        from supabase_store import PushDevice
        from sqlalchemy import select as _select
        rows = db.scalars(
            _select(PushDevice).where(PushDevice.user_id == user_id, PushDevice.active.is_(True))
        ).all()
        deactivated = []
        for r in rows:
            tok = r.token or ""
            if len(tok) <= 80 and ":" not in tok:
                r.active = False
                deactivated.append({"id": r.id, "tokenLen": len(tok), "platform": r.platform})
        db.commit()
        return {"userId": user_id, "deactivated": deactivated, "count": len(deactivated)}

    @app.post("/api/v1/admin/profiles/normalize-statuses")
    def normalize_existing_statuses(
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        """One-shot backfill: rewrite app_status on existing rows through
        normalize_esim_status() so legacy values (CANCEL, GOT_RESOURCE, etc.)
        become canonical (CANCELLED, INACTIVE). Idempotent — running it again
        is a no-op on already-normalized rows.
        """
        from supabase_store import ESimProfile
        from sqlalchemy import select as _select
        rows = db.scalars(_select(ESimProfile)).all()
        changes: list[dict[str, Any]] = []
        for r in rows:
            before = r.app_status
            after = normalize_esim_status(before)
            if after and after != before:
                r.app_status = after
                changes.append({"id": r.id, "from": before, "to": after})
        db.commit()
        return {"profilesScanned": len(rows), "rowsChanged": len(changes), "changes": changes}

    @app.post("/api/v1/internal/cron/refresh-active-usage")
    async def cron_refresh_active_usage(
        request: Request,
        db: Session = Depends(get_db),
        provider: Any = Depends(get_esim_provider),
    ) -> dict[str, Any]:
        """Background job: refresh provider usage for every ACTIVE eSIM profile.

        Auth: shared CRON_TOKEN env var. Caller passes `Authorization: Bearer <token>`
        OR `X-Cron-Token: <token>`. Falls back to admin auth if a bearer is
        present but doesn't match CRON_TOKEN.

        For each ACTIVE profile, calls provider.query_profiles by ICCID (the
        most reliable lookup), then sync_profiles to apply. Returns summary.
        Designed to be called hourly by the GitHub Action cron scheduler.
        """
        from supabase_store import ESimProfile
        from esim_access_api import ProfileQueryRequest
        from sqlalchemy import select as _select, func

        # Auth: shared CRON_TOKEN passed as X-Cron-Token header (or as
        # `Authorization: Bearer <CRON_TOKEN>`). Set CRON_TOKEN in Koyeb env.
        # If you also want admin-token access for manual smoke tests, hit
        # /admin/profiles/normalize-statuses-and-refresh or call this with the
        # CRON_TOKEN value as Bearer.
        expected = (os.environ.get("CRON_TOKEN") or "").strip()
        provided = (request.headers.get("X-Cron-Token") or "").strip()
        if not provided:
            auth_header = request.headers.get("Authorization") or ""
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CRON_TOKEN env var is not configured on this deployment",
            )
        # SEC-5: constant-time compare to avoid leaking the token via timing.
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Provide a valid X-Cron-Token header (or Bearer <CRON_TOKEN>)",
            )

        return await _perform_active_usage_refresh(db=db, provider=provider, source="cron")

    async def _perform_active_usage_refresh(
        *, db: Session, provider: Any, source: str
    ) -> dict[str, Any]:
        """Shared core for the cron endpoint AND the admin refresh button.

        Two passes:
        1) ACTIVE profiles with ICCID — refresh usage from the provider.
        2) BROKEN placeholders (no iccid + no activation_code) created in the
           last 7 days — try to resync via their order's providerOrderNo.
           Recovers orders whose post-buy retry-with-backoff window expired
           before the provider materialized their activation code.
        """
        from supabase_store import ESimProfile
        from esim_access_api import ProfileQueryRequest
        from sqlalchemy import or_, select as _select, func
        from datetime import timedelta

        # Cron mode: only refresh profiles that are already ACTIVE (cheap,
        # for GB usage updates). Admin mode: also include INACTIVE+iccid'd
        # profiles, so the "Refresh from provider" button picks up profiles
        # the user just installed (they're still INACTIVE in our DB until
        # the provider reports IN_USE).
        def _load_refresh_targets() -> tuple[list[Any], list[Any]]:
            if source == "cron":
                # ACTIVE profiles get a usage refresh. Plus a defensive sweep for
                # any profile whose provider_status says ONBOARDING/IN_USE (the
                # first-connection states that should auto-promote to ACTIVE).
                # The ONBOARDING alias + _apply_active_side_effects already fires
                # on every webhook/recover, so this is just belt-and-suspenders for
                # drift / missed webhooks.
                active_iccids = db.scalars(
                    _select(ESimProfile.iccid)
                    .where(ESimProfile.iccid.is_not(None))
                    .where(
                        or_(
                            func.upper(ESimProfile.app_status) == "ACTIVE",
                            func.upper(ESimProfile.provider_status).in_(("ONBOARDING", "IN_USE")),
                        )
                    )
                ).all()
            else:
                # admin/manual: include anything with an ICCID that isn't a hard-dead status
                active_iccids = db.scalars(
                    _select(ESimProfile.iccid)
                    .where(ESimProfile.iccid.is_not(None))
                    .where(~func.upper(ESimProfile.app_status).in_(("CANCELLED", "REVOKED", "REFUNDED", "EXPIRED")))
                ).all()
            # Snapshot the broken-placeholder (profile_id, order_no) pairs up front
            # so we don't keep a row reference alive across provider IO.
            broken_pairs = db.execute(
                _select(ESimProfile.id, OrderItem.provider_order_no)
                .join(OrderItem, ESimProfile.order_item_id == OrderItem.id)
                .where(ESimProfile.iccid.is_(None))
                .where(or_(ESimProfile.activation_code.is_(None), ESimProfile.activation_code == ""))
                .where(ESimProfile.created_at >= (utcnow() - timedelta(days=7)))
                .where(OrderItem.provider_order_no.is_not(None))
            ).all()

            # CRITICAL (freeze fix): release the pooled DB connection BEFORE the
            # provider IO loop. Previously this function held one connection for
            # the entire duration of N sequential provider round-trips (each
            # 1-3 s). With N active profiles that pinned a slot for N*2s+, and
            # when the hourly cron overlapped a user's pull-to-refresh the pool
            # starved and the whole backend appeared frozen. Now each iteration
            # closes the session before calling the provider; sync_profiles
            # transparently checks a fresh connection back out for the short write.
            db.close()
            return active_iccids, broken_pairs

        active_iccids, broken_pairs = await asyncio.to_thread(_load_refresh_targets)

        active_iccids = [i for i in active_iccids if i]
        store = SupabaseStore(db)
        attempted = 0
        synced = 0
        recovered = 0
        errors: list[dict[str, Any]] = []

        for iccid in active_iccids:
            attempted += 1
            try:
                db.close()  # drop the slot during the provider round-trip
                resp = await provider.query_profiles(ProfileQueryRequest(iccid=iccid))

                def _apply_active() -> Any:
                    return store.sync_profiles(
                        resp.model_dump(by_alias=True, exclude_none=True),
                        platform_code=f"{source}-refresh",
                        platform_name=f"Active usage refresh ({source})",
                        actor_phone=None,
                    )

                applied = await asyncio.to_thread(_apply_active)
                if applied:
                    synced += 1
            except Exception as exc:  # pragma: no cover - resilience
                # Keep the loop alive but preserve the full traceback in logs —
                # the truncated string in `errors` is for the response only.
                LOGGER.exception("admin.usage_refresh active-profile sync failed iccid=%s", iccid)
                errors.append({"iccid": iccid, "error": str(exc)[:200]})

        for profile_id, order_no in broken_pairs:
            attempted += 1
            if not order_no:
                continue
            try:
                db.close()
                resp = await provider.query_profiles(ProfileQueryRequest(order_no=order_no))

                def _apply_recover() -> tuple[Any, Any]:
                    applied = store.sync_profiles(
                        resp.model_dump(by_alias=True, exclude_none=True),
                        platform_code=f"{source}-recover",
                        platform_name=f"Broken-placeholder recovery ({source})",
                        actor_phone=None,
                    )
                    refreshed = db.scalar(_select(ESimProfile).where(ESimProfile.id == profile_id))
                    return applied, refreshed

                applied, refreshed = await asyncio.to_thread(_apply_recover)
                if refreshed is not None and refreshed.activation_code:
                    recovered += 1
                elif applied:
                    synced += 1
            except Exception as exc:  # pragma: no cover - resilience
                LOGGER.exception(
                    "admin.usage_refresh placeholder recovery failed profileId=%s orderNo=%s",
                    profile_id, order_no,
                )
                errors.append({"profileId": profile_id, "orderNo": order_no, "error": str(exc)[:200]})

        return {
            "attempted": attempted,
            "activeRefreshed": synced,
            "placeholdersRecovered": recovered,
            "errors": errors[:20],
            "errorCount": len(errors),
            "ranAt": utcnow(),
        }

    @app.post("/api/v1/admin/orders/refresh-from-provider")
    async def admin_refresh_orders_from_provider(
        db: Session = Depends(get_db),
        provider: Any = Depends(get_esim_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        """Admin-triggered: same provider sync as the cron endpoint, but auth'd
        by admin JWT (no CRON_TOKEN). Use the "Refresh from provider" button on
        the admin Orders page. Takes ~5-30s depending on how many ACTIVE
        profiles + recent broken placeholders the system has.
        """
        return await _perform_active_usage_refresh(db=db, provider=provider, source="admin")

    @app.get("/api/v1/admin/profiles/audit")
    def audit_esim_profiles(
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        """Aggregate health-check over the esim_profiles table.

        Surfaces data-quality drift the engineering team should fix:
          - status histograms (look for casing inconsistencies like CANCEL vs CANCELLED)
          - profiles with empty activation_code that ARE marked installed (broken)
          - profiles with NULL country (data we should be carrying from the order)
          - profiles whose last_provider_sync_at is stale (usage data outdated)
          - orphans: profiles with no order_item, recent orders with no profile
          - lifecycle event counts
        """
        from supabase_store import ESimProfile, OrderItem, CustomerOrder, ESimLifecycleEvent
        from sqlalchemy import select as _select, func, and_, or_
        from datetime import timedelta

        now = utcnow()
        stale_threshold = now - timedelta(days=1)

        total_profiles = db.scalar(_select(func.count(ESimProfile.id))) or 0

        # Status histograms
        app_status_rows = db.execute(
            _select(ESimProfile.app_status, func.count(ESimProfile.id))
            .group_by(ESimProfile.app_status)
        ).all()
        app_status_counts = {(r[0] or "(null)"): int(r[1]) for r in app_status_rows}

        provider_status_rows = db.execute(
            _select(ESimProfile.provider_status, func.count(ESimProfile.id))
            .group_by(ESimProfile.provider_status)
        ).all()
        provider_status_counts = {(r[0] or "(null)"): int(r[1]) for r in provider_status_rows}

        # Profiles marked installed but with NO activation_code (broken)
        installed_no_activation = db.scalar(
            _select(func.count(ESimProfile.id))
            .where(ESimProfile.installed.is_(True))
            .where(or_(ESimProfile.activation_code.is_(None), ESimProfile.activation_code == ""))
        ) or 0

        # Profiles with NULL country
        no_country = db.scalar(
            _select(func.count(ESimProfile.id))
            # country lives on OrderItem, not ESimProfile — count via JOIN
            .select_from(ESimProfile)
            .outerjoin(OrderItem, OrderItem.id == ESimProfile.order_item_id)
            .where(or_(OrderItem.country_code.is_(None), OrderItem.country_code == ""))
        ) or 0

        # ACTIVE profiles with stale last_provider_sync_at (usage data is old)
        stale_active = db.scalar(
            _select(func.count(ESimProfile.id))
            .where(func.upper(ESimProfile.app_status) == "ACTIVE")
            .where(or_(
                ESimProfile.last_provider_sync_at.is_(None),
                ESimProfile.last_provider_sync_at < stale_threshold,
            ))
        ) or 0

        # ACTIVE profiles with NULL usage fields (never synced)
        active_no_usage = db.scalar(
            _select(func.count(ESimProfile.id))
            .where(func.upper(ESimProfile.app_status) == "ACTIVE")
            .where(or_(ESimProfile.total_data_mb.is_(None), ESimProfile.used_data_mb.is_(None)))
        ) or 0

        # Profiles past expiry but app_status not EXPIRED
        past_expiry_wrong_status = db.scalar(
            _select(func.count(ESimProfile.id))
            .where(ESimProfile.expires_at.is_not(None))
            .where(ESimProfile.expires_at < now)
            .where(func.upper(ESimProfile.app_status) != "EXPIRED")
            .where(func.upper(ESimProfile.app_status) != "CANCELLED")
            .where(func.upper(ESimProfile.app_status) != "CANCEL")
            .where(func.upper(ESimProfile.app_status) != "REFUNDED")
            .where(func.upper(ESimProfile.app_status) != "REVOKED")
        ) or 0

        # Orphans
        orphan_profiles_no_order = db.scalar(
            _select(func.count(ESimProfile.id))
            .where(ESimProfile.order_item_id.is_(None))
        ) or 0

        recent_order_items = db.scalars(
            _select(OrderItem)
            .where(OrderItem.service_type == "esim")
            .where(OrderItem.created_at >= now - timedelta(days=30))
        ).all()
        orders_without_profile = 0
        for oi in recent_order_items:
            has_profile = db.scalar(_select(func.count(ESimProfile.id)).where(ESimProfile.order_item_id == oi.id)) or 0
            if has_profile == 0:
                orders_without_profile += 1

        # Lifecycle event totals
        total_events = db.scalar(_select(func.count(ESimLifecycleEvent.id))) or 0
        event_types_rows = db.execute(
            _select(ESimLifecycleEvent.event_type, func.count(ESimLifecycleEvent.id))
            .group_by(ESimLifecycleEvent.event_type)
            .order_by(func.count(ESimLifecycleEvent.id).desc())
        ).all()
        event_type_counts = {r[0]: int(r[1]) for r in event_types_rows[:20]}

        return {
            "totalProfiles": total_profiles,
            "appStatusCounts": app_status_counts,
            "providerStatusCounts": provider_status_counts,
            "issues": {
                "installedWithNoActivationCode": installed_no_activation,
                "profilesMissingCountry": no_country,
                "activeStaleSync_olderThan1d": stale_active,
                "activeMissingUsageFields": active_no_usage,
                "pastExpiryButStatusNotExpired": past_expiry_wrong_status,
                "orphanProfilesNoOrderItem": orphan_profiles_no_order,
                "recent30dEsimOrdersWithoutProfile": orders_without_profile,
            },
            "lifecycleEvents": {
                "total": total_events,
                "byTypeTop20": event_type_counts,
            },
            "auditedAt": now,
        }

    @app.post("/api/v1/admin/profiles/{profile_id}/resync")
    async def resync_profile_from_provider(
        profile_id: int,
        db: Session = Depends(get_db),
        provider: Any = Depends(get_esim_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        """Re-query the eSIM provider for this profile and store the fresh
        activation_code / iccid / qr_url etc. Used to recover orders where the
        initial provisioning call returned no data (placeholder profile).

        Looks up the parent order_item's provider_order_no, calls the provider's
        query_profiles, then sync_profiles to apply the result.
        """
        from supabase_store import CustomerOrder, ESimProfile, OrderItem
        from esim_access_api import ProfileQueryRequest
        from sqlalchemy import select as _select

        def _resolve_profile_and_order_no() -> tuple[ESimProfile, Any]:
            profile = db.scalar(_select(ESimProfile).where(ESimProfile.id == profile_id))
            if profile is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
            # Resolve providerOrderNo from the profile first, then fall back to the
            # most recent OrderItem for this user. OrderItem links to user via the
            # parent CustomerOrder (no direct OrderItem.user_id column).
            order_no = getattr(profile, "provider_order_no", None)
            if not order_no:
                order_item = db.scalar(
                    _select(OrderItem)
                    .join(CustomerOrder, CustomerOrder.id == OrderItem.customer_order_id)
                    .where(CustomerOrder.user_id == profile.user_id)
                    .where(OrderItem.provider_order_no.is_not(None))
                    .order_by(OrderItem.created_at.desc())
                    .limit(1)
                )
                if order_item is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No provider_order_no available to query the provider with",
                    )
                order_no = order_item.provider_order_no
            return profile, order_no

        profile, order_no = await asyncio.to_thread(_resolve_profile_and_order_no)
        response = await provider.query_profiles(ProfileQueryRequest(order_no=order_no))

        def _apply_resync() -> Any:
            synced = SupabaseStore(db).sync_profiles(
                response.model_dump(by_alias=True, exclude_none=True),
                platform_code="admin-resync",
                platform_name="Admin manual resync",
                actor_phone=None,
            )
            db.commit()
            db.refresh(profile)
            return synced

        synced = await asyncio.to_thread(_apply_resync)
        return {
            "profileId": profile.id,
            "orderNo": order_no,
            "profilesSynced": len(synced),
            "after": {
                "activationCodeLen": len(profile.activation_code or ""),
                "qrCodeUrl": profile.qr_code_url,
                "iccid": profile.iccid,
                "appStatus": getattr(profile, "app_status", None),
            },
        }

    @app.get("/api/v1/admin/users/{user_id}/profiles-raw")
    def list_user_profiles_raw(
        user_id: str,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        """Admin diagnostic: dump raw ESimProfile rows for one user.

        Returns activation_code, smdp, qr_code_url, install_url, app_status,
        installed flag, expiration — the fields the install screen depends on.
        Useful for debugging "I tapped Activate and nothing happened" when the
        backend hasn't filled in the Apple universal URL or QR.
        """
        from supabase_store import ESimProfile
        from sqlalchemy import select as _select
        rows = db.scalars(
            _select(ESimProfile).where(ESimProfile.user_id == user_id).order_by(ESimProfile.created_at.desc()).limit(20)
        ).all()
        return {
            "userId": user_id,
            "count": len(rows),
            "profiles": [
                {
                    "id": r.id,
                    "iccid": r.iccid,
                    "esimTranNo": getattr(r, "esim_tran_no", None),
                    "providerOrderNo": getattr(r, "provider_order_no", None),
                    "countryCode": getattr(r, "country_code", None),
                    "countryName": getattr(r, "country_name", None),
                    "appStatus": getattr(r, "app_status", None),
                    "providerStatus": getattr(r, "provider_status", None),
                    "installed": getattr(r, "installed", None),
                    "installedAt": getattr(r, "installed_at", None),
                    "activatedAt": getattr(r, "activated_at", None),
                    "expiresAt": getattr(r, "expires_at", None),
                    "totalDataMb": getattr(r, "total_data_mb", None),
                    "usedDataMb": getattr(r, "used_data_mb", None),
                    "activationCode": r.activation_code,
                    "activationCodeLen": len(r.activation_code or ""),
                    "activationCodeStartsWithLPA": bool(r.activation_code and r.activation_code.startswith("LPA:")),
                    "qrCodeUrl": r.qr_code_url,
                    "installUrl": getattr(r, "install_url", None),
                    "createdAt": r.created_at,
                    "updatedAt": r.updated_at,
                }
                for r in rows
            ],
        }

    @app.delete("/api/v1/admin/admin-users/{admin_id}")
    def delete_admin_user(
        admin_id: str,
        db: Session = Depends(get_db),
        actor: AdminUser = Depends(_require_owner_or_super),
    ) -> dict[str, Any]:
        if str(admin_id).strip() == str(actor.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete your own admin account",
            )
        from sqlalchemy import select as _select
        row = db.scalar(_select(AdminUser).where(AdminUser.id == admin_id))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin user not found")
        db.delete(row)
        db.commit()
        return {"deleted": admin_id}

    @app.post("/api/v1/admin/profiles/refund")
    def refund_profile(
        payload: RefundPayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        profile = SupabaseStore(db).apply_profile_action(
            action="refund",
            identifier_key="iccid",
            identifier_value=payload.iccid,
            platform_code=payload.context.platform_code,
            actor_phone=payload.context.actor_phone,
            note=payload.context.note,
            payload={"iccid": payload.iccid, "refundAmountMinor": payload.refund_amount_minor},
            refund_amount_minor=payload.refund_amount_minor,
        )
        return {"database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/admin/profiles/install")
    def install_profile(
        payload: ProfileStatePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        profile = SupabaseStore(db).apply_profile_action(
            action="install",
            identifier_key="iccid",
            identifier_value=payload.iccid,
            platform_code=payload.context.platform_code,
            actor_phone=payload.context.actor_phone,
            note=payload.context.note,
            payload={"iccid": payload.iccid},
        )
        return {"database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/admin/profiles/activate")
    def activate_profile(
        payload: ProfileStatePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        profile = SupabaseStore(db).apply_profile_action(
            action="activate",
            identifier_key="iccid",
            identifier_value=payload.iccid,
            platform_code=payload.context.platform_code,
            actor_phone=payload.context.actor_phone,
            note=payload.context.note,
            payload={"iccid": payload.iccid},
        )
        return {"database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/admin/pricing-rules")
    @app.post("/api/v1/admin/prices")
    def save_pricing_rule(
        payload: PricingRulePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_pricing")),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_pricing_rule(payload.model_dump(by_alias=False))
        return {"pricingRule": {"id": row.id}}

    @app.get("/api/v1/admin/pricing-rules")
    @app.get("/api/v1/admin/prices")
    def list_pricing_rules(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_pricing")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(PricingRule, limit=limit, offset=offset)
        return {"pricingRules": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.post("/api/v1/admin/discount-rules")
    def save_discount_rule(
        payload: DiscountRulePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_pricing")),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_discount_rule(payload.model_dump(by_alias=False))
        return {"discountRule": {"id": row.id}}

    @app.get("/api/v1/admin/discount-rules")
    def list_discount_rules(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_pricing")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(DiscountRule, limit=limit, offset=offset)
        return {"discountRules": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.post("/api/v1/admin/featured-locations")
    def save_featured_location(
        payload: FeaturedLocationPayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_content")),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_featured_location(payload.model_dump(by_alias=False))
        cache_key = _featured_locations_cache_key(payload.service_type)
        _PUBLIC_FEATURED_LOCATIONS_CACHE.pop(cache_key, None)
        _PUBLIC_FEATURED_LOCATIONS_RETRY_AFTER.pop(cache_key, None)
        return {"location": {"id": row.id}}

    @app.get("/api/v1/admin/featured-locations")
    def list_featured_locations(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_content")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(FeaturedLocation, limit=limit, offset=offset)
        return {"locations": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.delete("/api/v1/admin/featured-locations/{location_id}")
    def delete_featured_location(
        location_id: int,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_content")),
    ) -> dict[str, Any]:
        deleted = SupabaseStore(db).delete_featured_location(location_id)
        # Bust public caches across all service types so the change shows immediately.
        _PUBLIC_FEATURED_LOCATIONS_CACHE.clear()
        _PUBLIC_FEATURED_LOCATIONS_RETRY_AFTER.clear()
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Featured location not found")
        return {"deleted": True, "id": location_id}

    @app.get("/api/v1/featured-locations/public")
    @app.get("/api/v1/esim-access/featured-locations")
    def list_public_featured_locations(
        db: Session = Depends(get_db),
        service_type: str = Query(default="esim", alias="serviceType"),
    ) -> dict[str, Any]:
        cache_key = _featured_locations_cache_key(service_type)
        cached_locations = _PUBLIC_FEATURED_LOCATIONS_CACHE.get(cache_key)
        if monotonic() < _PUBLIC_FEATURED_LOCATIONS_RETRY_AFTER.get(cache_key, 0.0):
            return {
                "success": True,
                "data": {
                    "locations": _clone_featured_locations(cached_locations or []),
                    "cacheStatus": "stale" if cached_locations is not None else "db_unavailable",
                },
            }
        try:
            locations = SupabaseStore(db).list_public_featured_locations(service_type=service_type)
        except SQLAlchemyError as exc:
            _PUBLIC_FEATURED_LOCATIONS_RETRY_AFTER[cache_key] = monotonic() + _PUBLIC_DB_FAILURE_BACKOFF_SECONDS
            if cached_locations is not None:
                LOGGER.warning(
                    "featured_locations.public_db_unavailable cache=stale service_type=%s detail=%s",
                    cache_key,
                    exc,
                )
                return {
                    "success": True,
                    "data": {
                        "locations": _clone_featured_locations(cached_locations),
                        "cacheStatus": "stale",
                    },
                }
            LOGGER.warning(
                "featured_locations.public_db_unavailable cache=empty service_type=%s detail=%s",
                cache_key,
                exc,
            )
            return {"success": True, "data": {"locations": [], "cacheStatus": "db_unavailable"}}

        _PUBLIC_FEATURED_LOCATIONS_CACHE[cache_key] = _clone_featured_locations(locations)
        _PUBLIC_FEATURED_LOCATIONS_RETRY_AFTER.pop(cache_key, None)
        return {"success": True, "data": {"locations": locations, "cacheStatus": "fresh"}}

    @app.post("/api/v1/admin/exchange-rates")
    def save_exchange_rate(
        payload: ExchangeRatePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_pricing")),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_exchange_rate(payload.model_dump(by_alias=False))
        return {"exchangeRate": {"id": row.id}}

    @app.get("/api/v1/admin/exchange-rates")
    def list_exchange_rates(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_pricing")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(ExchangeRate, limit=limit, offset=offset)
        return {"exchangeRates": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/orders")
    def list_orders(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(CustomerOrder, limit=limit, offset=offset)
        return {"orders": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/order-items")
    def list_order_items(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(OrderItem, limit=limit, offset=offset)
        return {"orderItems": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/profiles")
    def list_profiles(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(ESimProfile, limit=limit, offset=offset)
        return {"profiles": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/lifecycle-events")
    def list_lifecycle_events(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(ESimLifecycleEvent, limit=limit, offset=offset)
        return {"events": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/payment-attempts")
    def list_payment_attempts(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(PaymentAttempt, limit=limit, offset=offset)
        return {"paymentAttempts": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/payment-provider-events")
    def list_payment_provider_events(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(PaymentProviderEvent, limit=limit, offset=offset)
        return {"paymentProviderEvents": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}
