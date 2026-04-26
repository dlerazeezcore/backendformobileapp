from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable

from fastapi import Depends, FastAPI, Query
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
    ProviderFieldRule,
    SupabaseStore,
)
from esim_access_api import ActionContext

LOGGER = logging.getLogger("uvicorn.error")
_PUBLIC_FEATURED_LOCATIONS_CACHE: dict[str, list[dict[str, Any]]] = {}


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


class ProviderFieldRulePayload(BaseModel):
    entity_type: str = Field(alias="entityType")
    field_paths: list[str] = Field(default_factory=list, alias="fieldPaths")
    enabled: bool = True
    provider: str = "esim_access"


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
    badge_text: str | None = Field(default=None, alias="badgeText")
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

    @app.post("/api/v1/admin/provider-field-rules")
    async def save_field_rule(
        payload: ProviderFieldRulePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rule = SupabaseStore(db).save_field_rule(payload.entity_type, payload.field_paths, payload.provider, payload.enabled)
        return {"rule": {"id": rule.id, "entityType": rule.entity_type, "fieldPaths": rule.field_paths}}

    @app.get("/api/v1/admin/provider-field-rules")
    async def list_field_rules(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(ProviderFieldRule, limit=limit, offset=offset)
        return {"rules": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

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

    @app.get("/api/v1/featured-locations/public")
    @app.get("/api/v1/esim-access/featured-locations")
    def list_public_featured_locations(
        db: Session = Depends(get_db),
        service_type: str = Query(default="esim", alias="serviceType"),
    ) -> dict[str, Any]:
        cache_key = _featured_locations_cache_key(service_type)
        try:
            locations = SupabaseStore(db).list_public_featured_locations(service_type=service_type)
        except SQLAlchemyError as exc:
            cached_locations = _PUBLIC_FEATURED_LOCATIONS_CACHE.get(cache_key)
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
