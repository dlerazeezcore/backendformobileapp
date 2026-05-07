from __future__ import annotations

from datetime import datetime
import logging
import os
from time import monotonic
from typing import Any, Callable

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth import get_token_claims, require_active_subject
from supabase_store import (
    AdminUser,
    AppSetting,
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

WHITELIST_SETTINGS_KEY = "country_whitelist"
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


class WhitelistSettingsPayload(BaseModel):
    enabled: bool | None = None
    is_enabled: bool | None = Field(default=None, alias="isEnabled")
    codes: list[str] | None = None
    country_codes: list[str] | None = Field(default=None, alias="countryCodes")
    whitelist_codes: list[str] | None = Field(default=None, alias="whitelistCodes")
    custom_fields: dict[str, Any] | None = Field(default=None, alias="customFields")

    model_config = {"populate_by_name": True}


def _normalize_country_codes(values: list[str] | None) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        code = str(raw or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _whitelist_response(stored: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(stored.get("enabled"))
    codes = _normalize_country_codes(stored.get("codes") if isinstance(stored.get("codes"), list) else [])
    return {
        "enabled": enabled,
        "isEnabled": enabled,
        "codes": codes,
        "countryCodes": codes,
        "whitelistCodes": codes,
    }


def register_admin_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    async def _require_admin_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser:
        row = require_active_subject(db, claims=claims, subject_type="admin")
        assert isinstance(row, AdminUser)
        return row

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

    @app.get("/api/v1/admin/whitelist-settings")
    @app.get("/api/v1/admin/settings/whitelist")
    @app.get("/api/v1/admin/whitelist-countries")
    @app.get("/api/v1/admin/whitelist")
    async def get_whitelist_settings(
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        stored = SupabaseStore(db).get_app_setting(WHITELIST_SETTINGS_KEY)
        return {"success": True, "data": _whitelist_response(stored)}

    @app.post("/api/v1/admin/whitelist-settings")
    @app.post("/api/v1/admin/settings/whitelist")
    @app.post("/api/v1/admin/whitelist-countries")
    @app.post("/api/v1/admin/whitelist")
    async def update_whitelist_settings(
        payload: WhitelistSettingsPayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        # Accept any of the alias keys the frontend might send.
        enabled_value = payload.enabled
        if enabled_value is None:
            enabled_value = payload.is_enabled
        if enabled_value is None and payload.custom_fields is not None:
            enabled_value = payload.custom_fields.get("enabled") or payload.custom_fields.get("isEnabled")
        enabled = bool(enabled_value)

        codes_value: list[str] | None = None
        for candidate in (payload.codes, payload.country_codes, payload.whitelist_codes):
            if candidate:
                codes_value = candidate
                break
        if codes_value is None and payload.custom_fields is not None:
            cf_codes = payload.custom_fields.get("codes") or payload.custom_fields.get("countryCodes")
            if isinstance(cf_codes, list):
                codes_value = cf_codes

        normalized_codes = _normalize_country_codes(codes_value)
        SupabaseStore(db).set_app_setting(
            WHITELIST_SETTINGS_KEY,
            {"enabled": enabled, "codes": normalized_codes},
        )
        return {"success": True, "data": _whitelist_response({"enabled": enabled, "codes": normalized_codes})}

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
