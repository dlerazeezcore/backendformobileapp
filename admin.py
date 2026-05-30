from __future__ import annotations

from datetime import datetime
import logging
import os
from time import monotonic
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth import get_token_claims, require_active_subject
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
    async def _require_admin_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser:
        row = require_active_subject(db, claims=claims, subject_type="admin")
        assert isinstance(row, AdminUser)
        return row

    async def _require_owner_or_super(
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
    async def list_admin_users(
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

    @app.get("/api/v1/admin/users/{user_id}/push-devices")
    async def list_user_push_devices(
        user_id: str,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
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
    async def purge_stale_user_devices(
        user_id: str,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
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

    @app.post("/api/v1/admin/profiles/{profile_id}/resync")
    async def resync_profile_from_provider(
        profile_id: int,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        """Re-query the eSIM provider for this profile and store the fresh
        activation_code / iccid / qr_url etc. Used to recover orders where the
        initial provisioning call returned no data (placeholder profile).

        Looks up the parent order_item's provider_order_no, calls the provider's
        query_profiles, then sync_profiles to apply the result.
        """
        from supabase_store import ESimProfile, OrderItem
        from esim_access_api import ProfileQueryRequest
        from dependencies import get_provider
        from sqlalchemy import select as _select
        profile = db.scalar(_select(ESimProfile).where(ESimProfile.id == profile_id))
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
        # Resolve providerOrderNo from the profile first, then fall back to the
        # most recent OrderItem for this user.
        order_no = getattr(profile, "provider_order_no", None)
        if not order_no:
            order_item = db.scalar(
                _select(OrderItem)
                .where(OrderItem.user_id == profile.user_id)
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
        provider = get_provider()
        response = await provider.query_profiles(ProfileQueryRequest(order_no=order_no))
        synced = SupabaseStore(db).sync_profiles(
            response.model_dump(by_alias=True, exclude_none=True),
            platform_code="admin-resync",
            platform_name="Admin manual resync",
            actor_phone=None,
        )
        db.commit()
        db.refresh(profile)
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
    async def list_user_profiles_raw(
        user_id: str,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
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
    async def delete_admin_user(
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
    async def refund_profile(
        payload: RefundPayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
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
    async def install_profile(
        payload: ProfileStatePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
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
    async def activate_profile(
        payload: ProfileStatePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
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
    async def save_pricing_rule(
        payload: PricingRulePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_pricing_rule(payload.model_dump(by_alias=False))
        return {"pricingRule": {"id": row.id}}

    @app.get("/api/v1/admin/pricing-rules")
    @app.get("/api/v1/admin/prices")
    async def list_pricing_rules(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(PricingRule, limit=limit, offset=offset)
        return {"pricingRules": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.post("/api/v1/admin/discount-rules")
    async def save_discount_rule(
        payload: DiscountRulePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_discount_rule(payload.model_dump(by_alias=False))
        return {"discountRule": {"id": row.id}}

    @app.get("/api/v1/admin/discount-rules")
    async def list_discount_rules(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(DiscountRule, limit=limit, offset=offset)
        return {"discountRules": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.post("/api/v1/admin/featured-locations")
    async def save_featured_location(
        payload: FeaturedLocationPayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_featured_location(payload.model_dump(by_alias=False))
        cache_key = _featured_locations_cache_key(payload.service_type)
        _PUBLIC_FEATURED_LOCATIONS_CACHE.pop(cache_key, None)
        _PUBLIC_FEATURED_LOCATIONS_RETRY_AFTER.pop(cache_key, None)
        return {"location": {"id": row.id}}

    @app.get("/api/v1/admin/featured-locations")
    async def list_featured_locations(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(FeaturedLocation, limit=limit, offset=offset)
        return {"locations": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.delete("/api/v1/admin/featured-locations/{location_id}")
    async def delete_featured_location(
        location_id: int,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
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
    async def save_exchange_rate(
        payload: ExchangeRatePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).save_exchange_rate(payload.model_dump(by_alias=False))
        return {"exchangeRate": {"id": row.id}}

    @app.get("/api/v1/admin/exchange-rates")
    async def list_exchange_rates(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(ExchangeRate, limit=limit, offset=offset)
        return {"exchangeRates": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/orders")
    async def list_orders(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(CustomerOrder, limit=limit, offset=offset)
        return {"orders": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/order-items")
    async def list_order_items(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(OrderItem, limit=limit, offset=offset)
        return {"orderItems": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/profiles")
    async def list_profiles(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(ESimProfile, limit=limit, offset=offset)
        return {"profiles": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/lifecycle-events")
    async def list_lifecycle_events(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(ESimLifecycleEvent, limit=limit, offset=offset)
        return {"events": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/payment-attempts")
    async def list_payment_attempts(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(PaymentAttempt, limit=limit, offset=offset)
        return {"paymentAttempts": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.get("/api/v1/admin/payment-provider-events")
    async def list_payment_provider_events(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(PaymentProviderEvent, limit=limit, offset=offset)
        return {"paymentProviderEvents": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}
