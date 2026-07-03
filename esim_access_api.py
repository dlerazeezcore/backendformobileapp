from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from math import ceil
import re
import uuid
from datetime import datetime, timedelta, timezone
from time import monotonic, time
from typing import Any, Callable, Generic, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload, selectinload

from auth import get_token_claims, require_active_subject
from config import get_settings, read_bool_env as _read_bool_env, read_float_env, read_int_env
from supabase_store import AdminUser, AppUser, CustomerOrder, ESimProfile, OrderItem, PaymentAttempt, ProfileInventoryRow, SupabaseStore, utcnow
from users import UserPayload

LOGGER = logging.getLogger("uvicorn.error")


class ESimAccessError(Exception):
    pass


class ESimAccessHTTPError(ESimAccessError):
    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        self.request_id = request_id
        super().__init__(message)


class ESimAccessAPIError(ESimAccessError):
    def __init__(
        self,
        *,
        error_code: str | None,
        error_message: str | None,
        status_code: int | None = None,
        provider_message: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.error_message = error_message
        self.status_code = status_code
        self.provider_message = provider_message or error_message
        self.request_id = request_id
        message = f"eSIM Access API error {error_code or 'unknown'}"
        if error_message:
            message = f"{message}: {error_message}"
        super().__init__(message)


_TOPUP_INVALID_HINTS = (
    "invalid esimtranno",
    "invalid esim tran no",
    "invalid esim tran",
    "invalid iccid",
    "invalid package",
    "package mismatch",
    "does not match",
    "not found",
    "not exist",
)
_TOPUP_CONFLICT_HINTS = (
    "already expired",
    "already revoked",
    "already canceled",
    "already cancelled",
    "already suspended",
    "expired",
    "revoked",
)


def _normalize_provider_business_status(*, upstream_status: int | None, provider_message: str | None) -> int:
    if upstream_status is not None and 400 <= upstream_status < 500:
        return upstream_status
    message = (provider_message or "").strip().lower()
    if any(token in message for token in _TOPUP_INVALID_HINTS):
        return 422
    if any(token in message for token in _TOPUP_CONFLICT_HINTS):
        return 409
    return 502


def _to_utc_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_lifecycle_status(
    *,
    raw_status: str | None,
    installed: bool,
    activated_at: datetime | None,
    bundle_expires_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> str:
    raw = str(raw_status or "").strip().lower()
    now_at = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if bundle_expires_at is not None:
        normalized_bundle_expires_at = (
            bundle_expires_at
            if bundle_expires_at.tzinfo is not None
            else bundle_expires_at.replace(tzinfo=timezone.utc)
        )
        if normalized_bundle_expires_at <= now_at:
            return "expired"
    elif installed and activated_at is not None and expires_at is not None:
        normalized_expires_at = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
        if normalized_expires_at <= now_at:
            return "expired"

    if raw in {"expired", "cancelled", "canceled", "revoked", "refunded", "voided", "closed"}:
        return "expired"
    if raw in {"provider_waiting", "provider-waiting", "provider waiting", "onboard", "onboarded"}:
        return "provider_waiting"
    if raw in {"booked", "got_resource", "released", "pending_install", "pending", "inactive", "created"} or not raw:
        return "provider_waiting" if installed else "inactive"
    if raw in {"active", "installed", "suspended", "enabled", "onboarding", "in_use"}:
        return "active" if installed and activated_at is not None else "provider_waiting"
    return "provider_waiting" if installed else "inactive"


def _format_number_as_string(value: float | int | str | None, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed if trimmed else fallback
    number = float(value)
    if abs(number - int(number)) < 1e-9:
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _dedupe_esim_tran_nos(values: list[str | None]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized:
            continue
        key = normalized.upper()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _chunk_values(values: list[str], *, size: int) -> list[list[str]]:
    if size <= 0:
        return [values]
    return [values[index : index + size] for index in range(0, len(values), size)]


def _collect_esim_tran_nos(rows: list[ESimProfile | ProfileInventoryRow]) -> list[str]:
    candidates: list[str | None] = []
    for row in rows:
        candidates.append(getattr(row, "esim_tran_no", None))
        custom_fields = getattr(row, "custom_fields", None)
        if isinstance(custom_fields, dict):
            candidates.append(custom_fields.get("esimTranNo"))
            candidates.append(custom_fields.get("esim_tran_no"))
    return _dedupe_esim_tran_nos(candidates)


def _bytes_to_mb(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 0:
        return 0
    return int(round(value / (1024 * 1024)))


def _normalize_country_entry(entry: Any) -> dict[str, str] | None:
    if isinstance(entry, dict):
        code = str(
            entry.get("code")
            or entry.get("countryCode")
            or entry.get("isoCode")
            or ""
        ).strip().upper()
        name = str(entry.get("name") or entry.get("countryName") or "").strip()
        if code and name:
            return {"code": code, "name": name}
        if code:
            return {"code": code, "name": code}
        if name:
            return {"code": name[:2].upper(), "name": name}
        return None
    if isinstance(entry, str):
        value = entry.strip()
        if not value:
            return None
        if len(value) == 2 and value.isalpha():
            code = value.upper()
            return {"code": code, "name": code}
        return {"code": value[:2].upper(), "name": value}
    return None


def _extract_included_countries(package_row: dict[str, Any]) -> list[dict[str, str]]:
    candidates = (
        package_row.get("includedCountries"),
        package_row.get("includedCountryList"),
        package_row.get("includeCountries"),
        package_row.get("countryList"),
        package_row.get("countries"),
        package_row.get("regionCountries"),
    )
    countries: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add_entry(entry: Any) -> None:
        normalized = _normalize_country_entry(entry)
        if normalized is None:
            return
        key = f"{normalized['code']}::{normalized['name']}"
        if key in seen:
            return
        seen.add(key)
        countries.append(normalized)

    for candidate in candidates:
        if isinstance(candidate, list):
            for item in candidate:
                _add_entry(item)
        elif isinstance(candidate, dict):
            _add_entry(candidate)
        elif isinstance(candidate, str):
            text = candidate.strip()
            if not text:
                continue
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        for item in parsed:
                            _add_entry(item)
                    else:
                        _add_entry(parsed)
                    continue
                except Exception:
                    pass
            for token in re.split(r"[,\|;/]+", text):
                _add_entry(token)
    return countries


def _is_daily_unlimited_package(item: dict[str, Any]) -> bool:
    """Decide whether a provider package is a 1-day unlimited plan.

    Product rule (set by the owner): the catalog must not surface 1-day
    unlimited plans. Multi-day unlimited and all data-capped plans (including
    1-day capped) stay visible. Top-up packages are exempt — we never filter
    them here.

    eSIM Access can report duration under several field names depending on
    endpoint and package variant; check all the ones we've actually seen.
    A package is "unlimited" when there's no positive totalDataMb/totalVolume.
    """
    days_candidates = (
        item.get("validityDays"),
        item.get("duration"),
        item.get("totalDuration"),
        item.get("periodNum"),
        item.get("days"),
    )
    days: int | None = None
    for candidate in days_candidates:
        if candidate is None:
            continue
        try:
            days = int(candidate)
            break
        except (TypeError, ValueError):
            continue
    if days != 1:
        return False
    total_candidates = (
        item.get("totalDataMb"),
        item.get("totalVolume"),
        item.get("totalData"),
    )
    for candidate in total_candidates:
        try:
            if candidate is not None and int(candidate) > 0:
                return False
        except (TypeError, ValueError):
            continue
    return True


def _augment_package_list_response(
    provider_payload: dict[str, Any],
    *,
    drop_daily_unlimited: bool = False,
) -> dict[str, Any]:
    payload = dict(provider_payload)
    obj = payload.get("obj")
    if not isinstance(obj, dict):
        return payload
    package_list = obj.get("packageList")
    if not isinstance(package_list, list):
        return payload
    enhanced: list[dict[str, Any]] = []
    for raw_item in package_list:
        item = dict(raw_item) if isinstance(raw_item, dict) else {}
        if drop_daily_unlimited and _is_daily_unlimited_package(item):
            # Owner-requested filter: 1-day unlimited plans must not appear in
            # the catalog (top-up endpoints set drop_daily_unlimited=False).
            continue
        included = _extract_included_countries(item)
        if included:
            item["includedCountries"] = included
        enhanced.append(item)
    obj = dict(obj)
    obj["packageList"] = enhanced
    payload["obj"] = obj
    return payload


def _single_country_code(item: dict[str, Any]) -> str | None:
    """A package's country for country-scoped pricing rules — only when it covers
    exactly one country (regional/multi-country bundles → None)."""
    parts = [p.strip().upper() for p in str(item.get("location") or "").split(",") if p.strip()]
    return parts[0] if len(parts) == 1 else None


def _apply_sale_prices(db: Session, payload: dict[str, Any]) -> None:
    """Attach `salePriceMinor` (IQD) to each catalog package via the same pricing
    engine checkout uses, so the displayed price reflects per-country/per-bundle
    rules. Mutates the payload in place. Runs in a worker thread (sync DB)."""
    obj = payload.get("obj")
    if not isinstance(obj, dict):
        return
    package_list = obj.get("packageList")
    if not isinstance(package_list, list):
        return
    items = [
        {"packageCode": it.get("packageCode"), "countryCode": _single_country_code(it), "providerPriceMinor": it.get("price")}
        for it in package_list
        if isinstance(it, dict) and it.get("packageCode")
    ]
    if not items:
        return
    quote = SupabaseStore(db).quote_esim_sale_prices(items)
    for it in package_list:
        if isinstance(it, dict):
            sale = quote.get(it.get("packageCode"))
            if sale is not None:
                it["salePriceMinor"] = sale


def _augment_profile_usage_units(provider_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(provider_payload)
    obj = payload.get("obj")
    if not isinstance(obj, dict):
        return payload
    esim_list = obj.get("esimList")
    if not isinstance(esim_list, list):
        return payload
    enriched: list[dict[str, Any]] = []
    for raw_item in esim_list:
        item = dict(raw_item) if isinstance(raw_item, dict) else {}
        total_mb = _as_int(item.get("totalDataMb"))
        used_mb = _as_int(item.get("usedDataMb"))
        remaining_mb = _as_int(item.get("remainingDataMb"))
        if total_mb is None:
            total_mb = _as_int(item.get("totalVolume"))
        if used_mb is None:
            used_mb = _as_int(item.get("orderUsage"))
        if total_mb is None:
            total_mb = _bytes_to_mb(_as_int(item.get("totalDataBytes")))
        if used_mb is None:
            used_mb = _bytes_to_mb(_as_int(item.get("usedDataBytes") or item.get("dataUsageBytes")))
        if remaining_mb is None and total_mb is not None and used_mb is not None:
            remaining_mb = max(total_mb - used_mb, 0)
        item["totalDataMb"] = total_mb
        item["usedDataMb"] = used_mb
        item["remainingDataMb"] = remaining_mb
        item["dataUsageUnit"] = "MB"
        enriched.append(item)
    obj = dict(obj)
    obj["esimList"] = enriched
    payload["obj"] = obj
    return payload


def _to_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


def _resolve_support_topup_type(custom_fields: dict[str, Any]) -> int:
    for key in ("supportTopUpType", "support_top_up_type", "topupSupportType", "topUpType"):
        raw = custom_fields.get(key)
        parsed = _as_int(raw)
        if parsed is not None:
            return max(int(parsed), 0)
    return 0


def _parse_lpa_activation_code(activation_code: str | None) -> tuple[str | None, str | None]:
    """Parse an LPA string 'LPA:1$<smdp-address>$<matching-id>' into (smdpAddress, matchingId).

    Tolerates a missing 'LPA:' scheme or format-version token, and returns
    (None, None) for empty/unparseable input.
    """
    if not activation_code:
        return None, None
    body = activation_code.strip()
    if not body:
        return None, None
    if body[:4].upper() == "LPA:":
        body = body[4:]
    parts = body.split("$")
    # Drop a leading format-version token ('1') when present.
    if parts and parts[0].strip() in {"1", ""}:
        parts = parts[1:]
    smdp = parts[0].strip() if len(parts) >= 1 and parts[0].strip() else None
    matching_id = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else None
    return smdp, matching_id


def _build_apple_install_url(activation_code: str | None) -> str | None:
    """Apple one-tap eSIM install Universal Link (iOS 17.4+) built from the LPA string."""
    if not activation_code or not activation_code.strip():
        return None
    return (
        "https://esimsetup.apple.com/esim_qrcode_provisioning?carddata="
        + quote(activation_code.strip(), safe=":$")
    )


def _serialize_profile(row: ESimProfile | ProfileInventoryRow, *, now: datetime) -> dict[str, Any]:
    custom_fields = row.custom_fields or {}
    if not isinstance(custom_fields, dict):
        custom_fields = {}
    custom_fields = dict(custom_fields)
    order_item = getattr(row, "order_item", None)
    if "checkoutSnapshot" not in custom_fields:
        custom_fields["checkoutSnapshot"] = (order_item.custom_fields or {}).get("checkoutSnapshot") if order_item is not None else None
    if "packageMetadata" not in custom_fields:
        custom_fields["packageMetadata"] = (
            (order_item.custom_fields or {}).get("packageMetadata")
            if order_item is not None
            else None
        )
    if custom_fields.get("packageMetadata") is None:
        custom_fields["packageMetadata"] = {
            "packageCode": order_item.package_code if order_item is not None else None,
            "packageSlug": order_item.package_slug if order_item is not None else None,
            "packageName": order_item.package_name if order_item is not None else None,
            "countryCode": order_item.country_code if order_item is not None else None,
            "countryName": order_item.country_name if order_item is not None else None,
        }
    usage_unit_hint = str(
        custom_fields.get("dataUnit")
        or custom_fields.get("usageUnit")
        or custom_fields.get("volumeUnit")
        or ""
    ).strip().lower()

    def _normalize_mb(raw_value: int | None) -> int | None:
        if raw_value is None:
            return None
        value = int(raw_value)
        if value < 0:
            return 0
        if "byte" in usage_unit_hint:
            return max(int(round(value / (1024 * 1024))), 0)
        if "kb" in usage_unit_hint or "kib" in usage_unit_hint:
            return max(int(round(value / 1024)), 0)
        if "mb" in usage_unit_hint or "mib" in usage_unit_hint:
            return value
        # Legacy/provider ambiguity fallback:
        # if values are very large they are typically KB in provider payloads.
        if value > 5000:
            return max(int(round(value / 1024)), 0)
        return value

    total_data_mb = _normalize_mb(row.total_data_mb)
    used_data_mb = _normalize_mb(row.used_data_mb)
    remaining_data_mb = _normalize_mb(row.remaining_data_mb)
    package_data_mb = total_data_mb
    if package_data_mb is None:
        package_data_mb = _normalize_mb(_as_int(custom_fields.get("packageDataMb")))
    if package_data_mb is None:
        package_data_mb = _normalize_mb(_as_int(custom_fields.get("packageData")))
    if remaining_data_mb is None and total_data_mb is not None and used_data_mb is not None:
        remaining_data_mb = max(total_data_mb - used_data_mb, 0)

    def _to_gb(value_mb: int | None) -> float | None:
        if value_mb is None:
            return None
        return round(float(value_mb) / 1024.0, 6)

    installed_flag = bool(getattr(row, "installed", False))
    days_left: int | None = None
    now_at = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    activated_at = (
        row.activated_at if row.activated_at is None or row.activated_at.tzinfo is not None else row.activated_at.replace(tzinfo=timezone.utc)
    )
    # User-facing countdown follows bundle validity window:
    # start only after activation and count validity_days from activated_at.
    bundle_expires_at: datetime | None = None
    if installed_flag and activated_at is not None and row.validity_days and row.validity_days > 0:
        bundle_expires_at = activated_at + timedelta(days=row.validity_days)
    effective_expires_at = bundle_expires_at
    # Fallback for legacy rows that do not carry validity_days.
    if installed_flag and effective_expires_at is None and activated_at is not None and row.expires_at is not None:
        effective_expires_at = row.expires_at if row.expires_at.tzinfo is not None else row.expires_at.replace(tzinfo=timezone.utc)
    if effective_expires_at is not None:
        delta_seconds = (effective_expires_at - now_at).total_seconds()
        days_left = max(int(ceil(delta_seconds / 86400)), 0)
        if row.validity_days and row.validity_days > 0:
            days_left = min(days_left, int(row.validity_days))
    status_value = _canonical_lifecycle_status(
        raw_status=row.app_status or row.provider_status,
        installed=installed_flag,
        activated_at=activated_at,
        bundle_expires_at=bundle_expires_at,
        expires_at=row.expires_at,
        now=now_at,
    )
    if status_value == "expired" and days_left is None and activated_at is not None:
        days_left = 0
    package_metadata = custom_fields.get("packageMetadata") if isinstance(custom_fields.get("packageMetadata"), dict) else {}
    country_code = (
        row.order_item.country_code
        if row.order_item is not None
        else custom_fields.get("countryCode") or package_metadata.get("countryCode")
    )
    country_name = (
        row.order_item.country_name
        if row.order_item is not None
        else custom_fields.get("countryName") or package_metadata.get("countryName")
    )
    package_name = (
        row.order_item.package_name
        if row.order_item is not None
        else custom_fields.get("packageName") or package_metadata.get("packageName")
    )
    package_code = (
        row.order_item.package_code
        if row.order_item is not None
        else custom_fields.get("packageCode") or package_metadata.get("packageCode")
    )
    provider_order_no = (
        row.order_item.provider_order_no
        if row.order_item is not None
        else custom_fields.get("providerOrderNo") or custom_fields.get("provider_order_no")
    )
    support_topup_type = _resolve_support_topup_type(custom_fields)
    is_expired = status_value == "expired"
    can_show_qr = bool(row.qr_code_url) and not is_expired
    can_activate = (
        status_value in {"inactive", "provider_waiting"}
        and not installed_flag
        and bool(row.activation_code or row.qr_code_url or row.install_url)
    )
    can_top_up = not is_expired and support_topup_type > 0
    smdp_address, matching_id = _parse_lpa_activation_code(row.activation_code)
    apple_install_url = _build_apple_install_url(row.activation_code) if not is_expired else None
    manual_entry = (
        {"smdpAddress": smdp_address, "activationCode": row.activation_code}
        if row.activation_code and not is_expired
        else None
    )
    return {
        "id": row.id,
        "userId": row.user_id,
        "user_id": row.user_id,
        "providerOrderNo": provider_order_no,
        "provider_order_no": provider_order_no,
        "esimTranNo": row.esim_tran_no,
        "esim_tran_no": row.esim_tran_no,
        "iccid": row.iccid,
        "countryCode": country_code,
        "country_code": country_code,
        "countryName": country_name,
        "country_name": country_name,
        "packageName": package_name,
        "package_name": package_name,
        "packageCode": package_code,
        "package_code": package_code,
        "status": status_value,
        "appStatus": row.app_status,
        "app_status": row.app_status,
        "providerStatus": row.provider_status,
        "provider_status": row.provider_status,
        "isExpired": is_expired,
        "canActivate": can_activate,
        "canTopUp": can_top_up,
        "canShowQr": can_show_qr,
        "installed": installed_flag,
        "installedAt": _to_utc_z(row.installed_at),
        "installed_at": _to_utc_z(row.installed_at),
        "activatedAt": _to_utc_z(activated_at),
        "activated_at": _to_utc_z(activated_at),
        "bundleExpiresAt": _to_utc_z(bundle_expires_at),
        "bundle_expires_at": _to_utc_z(bundle_expires_at),
        "expiresAt": _to_utc_z(row.expires_at),
        "expires_at": _to_utc_z(row.expires_at),
        "totalDataMb": total_data_mb,
        "packageDataMb": package_data_mb,
        "usedDataMb": used_data_mb,
        "remainingDataMb": remaining_data_mb,
        "totalDataGb": _to_gb(total_data_mb),
        "usedDataGb": _to_gb(used_data_mb),
        "remainingDataGb": _to_gb(remaining_data_mb),
        "dataUnit": "MB",
        "usageUnit": "MB",
        "validityDays": int(row.validity_days) if row.validity_days else None,
        "validity_days": int(row.validity_days) if row.validity_days else None,
        "daysLeft": days_left,
        "supportTopUpType": support_topup_type,
        "activationCode": row.activation_code,
        "activation_code": row.activation_code,
        "qrCodeUrl": row.qr_code_url,
        "qr_code_url": row.qr_code_url,
        "installUrl": row.install_url,
        "install_url": row.install_url,
        "appleInstallUrl": apple_install_url,
        "apple_install_url": apple_install_url,
        "smdpAddress": smdp_address,
        "smdp_address": smdp_address,
        "matchingId": matching_id,
        "matching_id": matching_id,
        "manualEntry": manual_entry,
        "manual_entry": manual_entry,
        "customFields": custom_fields,
        "custom_fields": custom_fields,
    }


def _serialize_order(order: CustomerOrder) -> dict[str, Any]:
    items = []
    for it in (order.order_items or []):
        items.append(
            {
                "id": it.id,
                "serviceType": it.service_type,
                "status": it.item_status,
                "providerOrderNo": it.provider_order_no,
                "countryCode": it.country_code,
                "countryName": it.country_name,
                "packageCode": it.package_code,
                "packageName": it.package_name,
                "quantity": it.quantity,
                "salePriceMinor": it.sale_price_minor,
            }
        )
    return {
        "id": order.id,
        "orderNumber": order.order_number,
        "status": order.order_status,
        "currencyCode": order.currency_code,
        "totalMinor": order.total_minor,
        "subtotalMinor": order.subtotal_minor,
        "markupMinor": order.markup_minor,
        "discountMinor": order.discount_minor,
        "paymentMethod": order.payment_method,
        "paymentProvider": order.payment_provider,
        "bookedAt": _to_utc_z(order.booked_at),
        "createdAt": _to_utc_z(order.created_at),
        "items": items,
    }


def _esim_status_bucket(profile_view: dict[str, Any] | None) -> str:
    if profile_view is None:
        return "pending"
    status = str(profile_view.get("status") or "").lower()
    if status == "expired":
        return "expired"
    remaining = profile_view.get("remainingDataMb")
    total = profile_view.get("totalDataMb")
    if remaining == 0 and total:
        return "used"
    if profile_view.get("installed"):
        return "installed"
    return "not_installed"


def _serialize_admin_order(order: CustomerOrder, *, now: datetime) -> dict[str, Any]:
    base = _serialize_order(order)
    user = order.user
    base["user"] = (
        {"id": user.id, "name": user.name, "phone": user.phone} if user is not None else None
    )
    primary: dict[str, Any] | None = None
    # First profile per order item, so each line can show its data + validity.
    views_by_item: dict[int, dict[str, Any]] = {}
    for item in (order.order_items or []):
        for profile in (item.profiles or []):
            view = _serialize_profile(profile, now=now)
            views_by_item[item.id] = view
            if primary is None:
                primary = {
                    "status": view["status"],
                    "installed": view["installed"],
                    "remainingDataMb": view["remainingDataMb"],
                    "totalDataMb": view["totalDataMb"],
                }
            break
    # Annotate each serialized item with the bundle spec (GB + days) so the admin
    # UI can show "5 GB · 7 days" instead of a package code.
    for out in base["items"]:
        view = views_by_item.get(out["id"])
        if view is not None:
            out["dataGb"] = view.get("totalDataGb")
            out["validityDays"] = view.get("validityDays")
            # Positive-evidence only: a missing totalDataMb on a freshly-booked
            # placeholder means "not synced yet", NOT "unlimited". Only call it
            # unlimited once the profile has progressed past the initial sync
            # (active/expired) and the provider still reports no data cap.
            status_value = str(view.get("status") or "").lower()
            out["unlimited"] = (
                not view.get("totalDataMb") and status_value in {"active", "expired"}
            )
        else:
            out["dataGb"] = None
            out["validityDays"] = None
            out["unlimited"] = False
    base["esim"] = primary
    base["esimStatus"] = _esim_status_bucket(primary)
    return base


def _resolve_target_user_id(
    *,
    actor: AppUser | AdminUser,
    claims: dict[str, Any],
    requested_user_id: str | None,
) -> str:
    subject_type = str(claims.get("typ") or "")
    if subject_type == "user":
        if requested_user_id and requested_user_id != actor.id:
            raise HTTPException(
                status_code=403,
                detail="User token cannot access another user's data.",
            )
        return actor.id
    if subject_type == "admin":
        return requested_user_id or actor.id
    raise HTTPException(status_code=403, detail="Token subject is not allowed for this endpoint.")


def _resolve_profile_identifier(payload: MyProfileActionPayload) -> tuple[str, str]:
    if payload.iccid and payload.iccid.strip():
        return "iccid", payload.iccid.strip()
    if payload.esim_tran_no and payload.esim_tran_no.strip():
        return "esim_tran_no", payload.esim_tran_no.strip()
    if payload.provider_order_no and payload.provider_order_no.strip():
        return "provider_order_no", payload.provider_order_no.strip()
    if payload.profile_id is not None:
        return "id", str(payload.profile_id)
    raise HTTPException(status_code=422, detail="Either iccid, esimTranNo, providerOrderNo, or id is required.")


def _lookup_profile_by_identifier(db: Session, *, identifier_key: str, identifier_value: str) -> ESimProfile | None:
    if identifier_key == "iccid":
        return db.scalar(select(ESimProfile).where(ESimProfile.iccid == identifier_value))
    if identifier_key == "esim_tran_no":
        return db.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == identifier_value))
    if identifier_key == "provider_order_no":
        return db.scalar(
            select(ESimProfile)
            .join(OrderItem, ESimProfile.order_item_id == OrderItem.id)
            .where(OrderItem.provider_order_no == identifier_value)
            .order_by(ESimProfile.updated_at.desc(), ESimProfile.id.desc())
            .limit(1)
        )
    if identifier_key == "id":
        parsed_id = _as_int(identifier_value)
        if parsed_id is None:
            return None
        return db.scalar(select(ESimProfile).where(ESimProfile.id == parsed_id))
    return None


def build_topup_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ESimAccessAPIError):
        provider_message = exc.provider_message or exc.error_message or str(exc)
        status_code = _normalize_provider_business_status(
            upstream_status=exc.status_code,
            provider_message=provider_message,
        )
        trace_id = exc.request_id or str(uuid.uuid4())
        if status_code >= 500:
            error_code = exc.error_code or "ESIM_PROVIDER_UPSTREAM_ERROR"
            message = "Top-up provider request failed."
        elif status_code == 409:
            error_code = exc.error_code or "ESIM_TOPUP_STATE_CONFLICT"
            message = "Top-up request conflicts with current eSIM state."
        else:
            error_code = exc.error_code or "ESIM_TOPUP_INVALID_REQUEST"
            message = "Top-up request is invalid for the target eSIM or package."
        return JSONResponse(
            status_code=status_code,
            content={
                "success": False,
                "errorCode": error_code,
                "message": message,
                "providerMessage": provider_message,
                "requestId": trace_id,
                "traceId": trace_id,
            },
        )
    if isinstance(exc, ESimAccessHTTPError):
        trace_id = exc.request_id or str(uuid.uuid4())
        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "errorCode": "ESIM_PROVIDER_UNREACHABLE",
                "message": "Top-up provider request failed.",
                "providerMessage": str(exc),
                "requestId": trace_id,
                "traceId": trace_id,
            },
        )
    # BE-3: never stringify an arbitrary internal exception to the client. Log the
    # detail server-side under a trace id and return a generic message.
    trace_id = str(uuid.uuid4())
    LOGGER.error("Unexpected top-up error (trace=%s): %r", trace_id, exc)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "errorCode": "INTERNAL_ERROR",
            "message": "Top-up request failed unexpectedly.",
            "requestId": trace_id,
            "traceId": trace_id,
        },
    )


def compute_signature(
    *,
    timestamp: str,
    request_id: str,
    access_code: str,
    request_body: str,
    secret_key: str,
) -> str:
    signing_string = f"{timestamp}{request_id}{access_code}{request_body}"
    return hmac.new(
        secret_key.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_auth_headers(
    *,
    access_code: str,
    secret_key: str,
    request_body: str,
    timestamp: str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    resolved_timestamp = timestamp or str(int(time() * 1000))
    resolved_request_id = request_id or str(uuid.uuid4())
    return {
        "RT-Timestamp": resolved_timestamp,
        "RT-RequestID": resolved_request_id,
        "RT-AccessCode": access_code,
        "RT-Signature": compute_signature(
            timestamp=resolved_timestamp,
            request_id=resolved_request_id,
            access_code=access_code,
            request_body=request_body,
            secret_key=secret_key,
        ),
    }


class Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Pager(Model):
    page_num: int = Field(default=1, alias="pageNum")
    page_size: int = Field(default=50, alias="pageSize")


class PackageQueryRequest(Model):
    location_code: str | None = Field(default=None, alias="locationCode")
    type: str | None = None
    slug: str | None = None
    package_code: str | None = Field(default=None, alias="packageCode")
    iccid: str | None = None


class OrderPackageInfo(Model):
    package_code: str = Field(alias="packageCode")
    count: int
    price: int | None = None
    period_num: int | None = Field(default=None, alias="periodNum")


class OrderProfilesRequest(Model):
    transaction_id: str = Field(alias="transactionId")
    amount: int | None = None
    package_info_list: list[OrderPackageInfo] = Field(alias="packageInfoList")


class ProfileQueryRequest(Model):
    order_no: str | None = Field(default=None, alias="orderNo")
    iccid: str | None = None
    pager: Pager = Field(default_factory=Pager)


class EsimTranNoRequest(Model):
    esim_tran_no: str = Field(alias="esimTranNo")


class ICCIDRequest(Model):
    iccid: str


class TopUpRequest(Model):
    esim_tran_no: str | None = Field(default=None, alias="esimTranNo")
    iccid: str | None = None
    package_code: str = Field(alias="packageCode")
    transaction_id: str = Field(alias="transactionId")


class WebhookConfigRequest(Model):
    webhook: str


class SendSmsRequest(Model):
    esim_tran_no: str | None = Field(default=None, alias="esimTranNo")
    iccid: str | None = None
    message: str


class UsageCheckRequest(Model):
    esim_tran_no_list: list[str] = Field(alias="esimTranNoList")


class EmptyRequest(Model):
    pass


class Package(Model):
    package_code: str | None = Field(default=None, alias="packageCode")
    slug: str | None = None
    name: str | None = None
    price: int | None = None
    retail_price: int | None = Field(default=None, alias="retailPrice")
    currency_code: str | None = Field(default=None, alias="currencyCode")
    volume: int | None = None
    duration: int | None = None
    duration_unit: str | None = Field(default=None, alias="durationUnit")
    location: str | None = None
    speed: str | None = None


class PackageListResult(Model):
    package_list: list[Package] = Field(default_factory=list, alias="packageList")


class OrderResult(Model):
    order_no: str | None = Field(default=None, alias="orderNo")
    transaction_id: str | None = Field(default=None, alias="transactionId")


class ESimProfileResult(Model):
    esim_tran_no: str | None = Field(default=None, alias="esimTranNo")
    order_no: str | None = Field(default=None, alias="orderNo")
    transaction_id: str | None = Field(default=None, alias="transactionId")
    iccid: str | None = None
    imsi: str | None = None
    msisdn: str | None = None
    ac: str | None = None
    qr_code_url: str | None = Field(default=None, alias="qrCodeUrl")
    short_url: str | None = Field(default=None, alias="shortUrl")
    smdp_status: str | None = Field(default=None, alias="smdpStatus")
    esim_status: str | None = Field(default=None, alias="esimStatus")
    data_type: int | str | None = Field(default=None, alias="dataType")
    active_type: int | None = Field(default=None, alias="activeType")
    total_volume: int | None = Field(default=None, alias="totalVolume")
    total_duration: int | None = Field(default=None, alias="totalDuration")
    duration_unit: str | None = Field(default=None, alias="durationUnit")
    order_usage: int | None = Field(default=None, alias="orderUsage")
    expired_time: str | None = Field(default=None, alias="expiredTime")


class ProfileListResult(Model):
    esim_list: list[ESimProfileResult] = Field(default_factory=list, alias="esimList")


class EmptyResult(Model):
    pass


class BalanceResult(Model):
    balance: int | None = None
    last_update_time: str | None = Field(default=None, alias="lastUpdateTime")


class TopUpResult(Model):
    transaction_id: str | None = Field(default=None, alias="transactionId")
    iccid: str | None = None
    expired_time: str | None = Field(default=None, alias="expiredTime")
    total_volume: int | None = Field(default=None, alias="totalVolume")
    total_duration: int | None = Field(default=None, alias="totalDuration")
    order_usage: int | None = Field(default=None, alias="orderUsage")


class UsageRecord(Model):
    esim_tran_no: str | None = Field(default=None, alias="esimTranNo")
    data_usage: int | None = Field(default=None, alias="dataUsage")
    total_data: int | None = Field(default=None, alias="totalData")
    last_update_time: str | None = Field(default=None, alias="lastUpdateTime")


class UsageResult(Model):
    esim_usage_list: list[UsageRecord] = Field(default_factory=list, alias="esimUsageList")


class Location(Model):
    code: str | None = None
    name: str | None = None
    type: int | None = None
    sub_location_list: list["Location"] | None = Field(default=None, alias="subLocationList")


class LocationListResult(Model):
    location_list: list[Location] = Field(default_factory=list, alias="locationList")


Location.model_rebuild()


class WebhookEvent(Model):
    notify_type: str = Field(alias="notifyType")
    event_generate_time: str | None = Field(default=None, alias="eventGenerateTime")
    notify_id: str | None = Field(default=None, alias="notifyId")
    content: dict[str, Any]


ResultT = TypeVar("ResultT")


class ESimAccessResponse(Model, Generic[ResultT]):
    success: bool
    error_code: str | None = Field(default=None, alias="errorCode")
    error_msg: str | None = Field(default=None, alias="errorMsg")
    obj: ResultT | None = None


class AsyncRateLimiter:
    """Spread provider calls across a per-second budget.

    The earlier version held ``self.lock`` across ``asyncio.sleep`` which
    serialized ALL concurrent provider calls behind a single mutex. Under
    load (multiple users polling /recover + cron + /usage/sync/my), this
    made the backend appear frozen: every provider call queued up waiting
    for the lock to release, holding their DB sessions during the wait.

    The fixed version computes the next allowed timestamp atomically inside
    the lock, then sleeps OUTSIDE the lock. Concurrent acquires can then
    overlap their sleeps and proceed in parallel up to the per-second cap.
    """

    def __init__(self, per_second: float) -> None:
        self.per_second = per_second
        self.lock = asyncio.Lock()
        self.next_allowed = 0.0

    async def acquire(self) -> None:
        if self.per_second <= 0:
            return
        interval = 1.0 / self.per_second
        loop = asyncio.get_running_loop()
        async with self.lock:
            now = loop.time()
            # Reserve our slot relative to the latest reservation. This means
            # 16 concurrent calls at 8/sec finish in ~2s instead of ~16s
            # (the old "hold-lock-while-sleeping" path).
            scheduled = max(self.next_allowed, now)
            self.next_allowed = scheduled + interval
        wait_for = scheduled - loop.time()
        if wait_for > 0:
            await asyncio.sleep(wait_for)


class ESimAccessAPI:
    def __init__(
        self,
        *,
        access_code: str,
        secret_key: str,
        base_url: str = "https://api.esimaccess.com",
        timeout: float = 15.0,
        rate_limit_per_second: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.access_code = access_code
        self.secret_key = secret_key
        self.rate_limiter = AsyncRateLimiter(rate_limit_per_second)
        # Tight connect timeout so an unreachable provider fails fast instead
        # of pinning a request thread (and its DB session, if any) for 30 s.
        # Read budget stays generous enough for the /package/list endpoint to
        # return its ~200 KB JSON.
        client_timeout = httpx.Timeout(
            connect=5.0,
            read=timeout,
            write=10.0,
            pool=5.0,
        )
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=client_timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self.packages_cache_ttl_seconds = self._read_float_env("ESIM_PACKAGES_CACHE_TTL_SECONDS", 7200.0, minimum=0.0)
        self.packages_cache_max_entries = self._read_int_env("ESIM_PACKAGES_CACHE_MAX_ENTRIES", 128, minimum=1)
        self._packages_cache: dict[str, tuple[float, ESimAccessResponse[PackageListResult]]] = {}
        self._packages_cache_lock = asyncio.Lock()
        self.locations_cache_ttl_seconds = self._read_float_env("ESIM_LOCATIONS_CACHE_TTL_SECONDS", 7200.0, minimum=0.0)
        self._locations_cache: tuple[float, ESimAccessResponse[LocationListResult]] | None = None
        self._locations_cache_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    # Thin delegations to the centralized readers in config.py (kept as static
    # methods so existing self./ESimAccessAPI. call sites stay unchanged).
    _read_float_env = staticmethod(read_float_env)
    _read_int_env = staticmethod(read_int_env)

    @staticmethod
    def _package_cache_key(request: PackageQueryRequest) -> str:
        payload = request.model_dump(by_alias=True, exclude_none=True)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    async def get_packages(
        self,
        request: PackageQueryRequest,
    ) -> ESimAccessResponse[PackageListResult]:
        if self.packages_cache_ttl_seconds <= 0:
            return await self._post("/api/v1/open/package/list", request, ESimAccessResponse[PackageListResult])

        cache_key = self._package_cache_key(request)
        now = monotonic()
        async with self._packages_cache_lock:
            cached = self._packages_cache.get(cache_key)
            if cached is not None:
                expires_at, cached_response = cached
                if expires_at > now:
                    return cached_response
                self._packages_cache.pop(cache_key, None)

        response = await self._post("/api/v1/open/package/list", request, ESimAccessResponse[PackageListResult])

        now = monotonic()
        async with self._packages_cache_lock:
            self._packages_cache[cache_key] = (now + self.packages_cache_ttl_seconds, response)
            if len(self._packages_cache) > self.packages_cache_max_entries:
                expired_keys = [key for key, (expires_at, _) in self._packages_cache.items() if expires_at <= now]
                for key in expired_keys:
                    self._packages_cache.pop(key, None)
            while len(self._packages_cache) > self.packages_cache_max_entries:
                oldest_key = min(self._packages_cache.items(), key=lambda item: item[1][0])[0]
                self._packages_cache.pop(oldest_key, None)

        return response

    async def order_profiles(
        self,
        request: OrderProfilesRequest,
    ) -> ESimAccessResponse[OrderResult]:
        return await self._post("/api/v1/open/esim/order", request, ESimAccessResponse[OrderResult])

    async def query_profiles(
        self,
        request: ProfileQueryRequest,
    ) -> ESimAccessResponse[ProfileListResult]:
        return await self._post("/api/v1/open/esim/query", request, ESimAccessResponse[ProfileListResult])

    async def cancel_profile(
        self,
        request: EsimTranNoRequest,
    ) -> ESimAccessResponse[EmptyResult]:
        return await self._post("/api/v1/open/esim/cancel", request, ESimAccessResponse[EmptyResult])

    async def suspend_profile(
        self,
        request: ICCIDRequest,
    ) -> ESimAccessResponse[EmptyResult]:
        return await self._post("/api/v1/open/esim/suspend", request, ESimAccessResponse[EmptyResult])

    async def unsuspend_profile(
        self,
        request: ICCIDRequest,
    ) -> ESimAccessResponse[EmptyResult]:
        return await self._post("/api/v1/open/esim/unsuspend", request, ESimAccessResponse[EmptyResult])

    async def revoke_profile(
        self,
        request: ICCIDRequest,
    ) -> ESimAccessResponse[EmptyResult]:
        return await self._post("/api/v1/open/esim/revoke", request, ESimAccessResponse[EmptyResult])

    async def balance_query(self) -> ESimAccessResponse[BalanceResult]:
        return await self._post("/api/v1/open/balance/query", None, ESimAccessResponse[BalanceResult])

    async def top_up(
        self,
        request: TopUpRequest,
    ) -> ESimAccessResponse[TopUpResult]:
        return await self._post("/api/v1/open/esim/topup", request, ESimAccessResponse[TopUpResult])

    async def set_webhook(
        self,
        request: WebhookConfigRequest,
    ) -> ESimAccessResponse[EmptyResult]:
        return await self._post("/api/v1/open/webhook/save", request, ESimAccessResponse[EmptyResult])

    async def send_sms(
        self,
        request: SendSmsRequest,
    ) -> ESimAccessResponse[EmptyResult]:
        return await self._post("/api/v1/open/esim/sendSms", request, ESimAccessResponse[EmptyResult])

    async def usage_check(
        self,
        request: UsageCheckRequest,
    ) -> ESimAccessResponse[UsageResult]:
        return await self._post("/api/v1/open/esim/usage/query", request, ESimAccessResponse[UsageResult])

    async def locations(
        self,
        request: EmptyRequest | None = None,
    ) -> ESimAccessResponse[LocationListResult]:
        payload = request or EmptyRequest()
        if self.locations_cache_ttl_seconds <= 0:
            return await self._post("/api/v1/open/location/list", payload, ESimAccessResponse[LocationListResult])

        now = monotonic()
        async with self._locations_cache_lock:
            if self._locations_cache is not None:
                expires_at, cached_response = self._locations_cache
                if expires_at > now:
                    return cached_response
                self._locations_cache = None

        response = await self._post("/api/v1/open/location/list", payload, ESimAccessResponse[LocationListResult])

        async with self._locations_cache_lock:
            self._locations_cache = (monotonic() + self.locations_cache_ttl_seconds, response)

        return response

    async def _post(
        self,
        path: str,
        payload: BaseModel | None,
        response_model: type[BaseModel],
    ) -> Any:
        body = "" if payload is None else json.dumps(
            payload.model_dump(by_alias=True, exclude_none=True),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        request_id = str(uuid.uuid4())
        headers = build_auth_headers(
            access_code=self.access_code,
            secret_key=self.secret_key,
            request_body=body,
            request_id=request_id,
        )
        if body:
            headers["Content-Type"] = "application/json"
        await self.rate_limiter.acquire()
        try:
            response = await self.client.post(path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ESimAccessHTTPError(str(exc), request_id=request_id) from exc
        try:
            parsed = response_model.model_validate(response.json())
        except Exception as exc:
            raise ESimAccessHTTPError(
                f"Invalid provider response: {response.text}",
                request_id=request_id,
            ) from exc
        error_code = getattr(parsed, "error_code", None)
        has_error = error_code not in (None, "", "0", 0)
        if response.status_code >= 400 or not parsed.success or has_error:
            raise ESimAccessAPIError(
                error_code=str(error_code) if error_code is not None else None,
                error_message=parsed.error_msg,
                status_code=response.status_code,
                provider_message=parsed.error_msg,
                request_id=request_id,
            )
        return parsed


class ActionContext(BaseModel):
    actor_phone: str | None = Field(default=None, alias="actorPhone")
    platform_code: str | None = Field(default=None, alias="platformCode")
    platform_name: str | None = Field(default=None, alias="platformName")
    note: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


class MyProfileActionPayload(BaseModel):
    iccid: str | None = None
    esim_tran_no: str | None = Field(default=None, alias="esimTranNo")
    provider_order_no: str | None = Field(default=None, alias="providerOrderNo")
    profile_id: int | None = Field(default=None, alias="id")
    user_id: str | None = Field(default=None, alias="userId")
    platform_code: str | None = Field(default="mobile-app", alias="platformCode")
    note: str | None = None


class ManagedOrderPayload(BaseModel):
    provider_request: OrderProfilesRequest = Field(alias="providerRequest")
    user: UserPayload
    platform_code: str = Field(alias="platformCode")
    platform_name: str | None = Field(default=None, alias="platformName")
    currency_code: str | None = Field(default=None, alias="currencyCode")
    provider_currency_code: str | None = Field(default=None, alias="providerCurrencyCode")
    exchange_rate: float | None = Field(default=None, alias="exchangeRate")
    sale_price_minor: int | None = Field(default=None, alias="salePriceMinor")
    provider_price_minor: int | None = Field(default=None, alias="providerPriceMinor")
    country_code: str | None = Field(default=None, alias="countryCode")
    country_name: str | None = Field(default=None, alias="countryName")
    package_code: str | None = Field(default=None, alias="packageCode")
    package_slug: str | None = Field(default=None, alias="packageSlug")
    package_name: str | None = Field(default=None, alias="packageName")
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")
    payment_attempt_id: str | None = Field(default=None, alias="paymentAttemptId")
    payment_transaction_id: str | None = Field(default=None, alias="paymentTransactionId")
    payment_method: str | None = Field(default=None, alias="paymentMethod")
    payment_provider: str | None = Field(default=None, alias="paymentProvider")
    payment_status: str | None = Field(default=None, alias="paymentStatus")
    payment_amount_minor: int | None = Field(default=None, alias="paymentAmountMinor")
    payment_currency_code: str | None = Field(default=None, alias="paymentCurrencyCode")
    payment_provider_payment_id: str | None = Field(default=None, alias="paymentProviderPaymentId")
    payment_provider_reference: str | None = Field(default=None, alias="paymentProviderReference")
    payment_idempotency_key: str | None = Field(default=None, alias="paymentIdempotencyKey")


class ManagedProfileSyncPayload(BaseModel):
    provider_request: ProfileQueryRequest = Field(alias="providerRequest")
    platform_code: str | None = Field(default=None, alias="platformCode")
    platform_name: str | None = Field(default=None, alias="platformName")
    actor_phone: str | None = Field(default=None, alias="actorPhone")


class ManagedUsageSyncPayload(BaseModel):
    provider_request: UsageCheckRequest = Field(alias="providerRequest")
    actor_phone: str | None = Field(default=None, alias="actorPhone")


class ManagedTopUpPayload(BaseModel):
    provider_request: TopUpRequest = Field(alias="providerRequest")
    platform_code: str | None = Field(default=None, alias="platformCode")
    platform_name: str | None = Field(default=None, alias="platformName")
    actor_phone: str | None = Field(default=None, alias="actorPhone")
    sync_after_topup: bool = Field(default=True, alias="syncAfterTopup")
    # SEC (BE-3): top-ups must be paid like orders — loyalty (comp-gated
    # server-side) or a FIB payment re-verified against the provider before the
    # provider spend. Mirrors the ManagedOrderPayload payment fields.
    payment_method: str | None = Field(default=None, alias="paymentMethod")
    payment_provider_payment_id: str | None = Field(default=None, alias="paymentProviderPaymentId")
    payment_transaction_id: str | None = Field(default=None, alias="paymentTransactionId")


class ManagedEsimTranActionPayload(BaseModel):
    provider_request: EsimTranNoRequest = Field(alias="providerRequest")
    context: ActionContext


class ManagedIccidActionPayload(BaseModel):
    provider_request: ICCIDRequest = Field(alias="providerRequest")
    context: ActionContext


def _normalize_payment_method(
    payment_method: str | None,
    payment_provider: str | None,
) -> tuple[str | None, str | None]:
    normalized_method = payment_method.strip().lower() if isinstance(payment_method, str) and payment_method.strip() else None
    normalized_provider = payment_provider.strip().lower() if isinstance(payment_provider, str) and payment_provider.strip() else None
    if normalized_method == "loyalty" and normalized_provider is None:
        normalized_provider = "internal_loyalty"
    if normalized_method == "fib" and normalized_provider is None:
        normalized_provider = "fib"
    return normalized_method, normalized_provider


def _resolve_payment_method_provider(payload: ManagedOrderPayload) -> tuple[str | None, str | None]:
    custom = payload.custom_fields or {}
    method = payload.payment_method
    provider = payload.payment_provider
    if method is None:
        method = custom.get("paymentMethod") or custom.get("payment_method")
    if provider is None:
        provider = custom.get("paymentProvider") or custom.get("payment_provider")
    normalized_method, normalized_provider = _normalize_payment_method(method, provider)
    if normalized_method and normalized_provider is None:
        normalized_provider = "internal_loyalty" if normalized_method == "loyalty" else normalized_method
    return normalized_method, normalized_provider


def _resolve_or_create_payment_for_managed_order(
    *,
    store: SupabaseStore,
    payload: ManagedOrderPayload,
    customer_order_id: int,
    order_item_id: int,
    user_id: str | None,
    service_type: str,
    amount_minor: int,
    currency_code: str,
    provider_request_payload: dict[str, Any],
    provider_response_payload: dict[str, Any],
) -> PaymentAttempt | None:
    method, provider_code = _resolve_payment_method_provider(payload)
    # SEC-1 hardening: this path comps an order as "paid" straight from client
    # input with NO provider verification. It is reserved for loyalty checkouts
    # (already comp-gated upstream). FIB is verified in
    # _verify_fib_payment_for_managed_order instead — never mint a paid attempt
    # from a client-supplied status for any other method.
    if method != "loyalty":
        return None
    attempt: PaymentAttempt | None = None
    if payload.payment_attempt_id:
        attempt = store.get_payment_attempt_by_id(payload.payment_attempt_id, for_update=True)
    elif payload.payment_transaction_id:
        attempt = store.get_payment_attempt_by_transaction_id(payload.payment_transaction_id, for_update=True)

    # Managed booking can finalize without a pre-created payment attempt.
    # Persist successful methods (loyalty + future methods) for reporting.
    if attempt is None and method:
        transaction_id = payload.payment_transaction_id or f"{method}-{order_item_id}-{uuid.uuid4().hex[:12]}"
        attempt = store.get_payment_attempt_by_transaction_id(transaction_id, for_update=True)
        if attempt is None:
            normalized_status = (
                store._normalize_payment_status(payload.payment_status)
                if payload.payment_status
                else "paid"
            )
            if normalized_status not in {"paid", "refunded"}:
                return None
            attempt = store.create_payment_attempt(
                transaction_id=transaction_id,
                payment_method=method,
                provider=provider_code or ("internal_loyalty" if method == "loyalty" else method),
                customer_order_id=customer_order_id,
                order_item_id=order_item_id,
                user_id=user_id,
                service_type=service_type,
                status=normalized_status,
                amount_minor=payload.payment_amount_minor if payload.payment_amount_minor is not None else amount_minor,
                currency_code=payload.payment_currency_code or currency_code,
                provider_payment_id=payload.payment_provider_payment_id,
                provider_reference=payload.payment_provider_reference,
                idempotency_key=payload.payment_idempotency_key,
                metadata={
                    "source": "orders_managed",
                    "paymentMethod": method,
                    "autoPaidOnBooking": True,
                },
                provider_request={"managedOrderRequest": provider_request_payload},
                provider_response={"managedOrderResponse": provider_response_payload},
                paid_at=utcnow() if normalized_status == "paid" else None,
            )

    if attempt is None:
        return None

    store.update_payment_attempt(
        attempt,
        customer_order_id=customer_order_id,
        order_item_id=order_item_id,
        provider=provider_code,
        provider_payment_id=payload.payment_provider_payment_id,
        provider_reference=payload.payment_provider_reference,
        idempotency_key=payload.payment_idempotency_key,
        metadata=store._merge_json_dict(
            attempt.metadata_payload,
            {
                "source": "orders_managed",
                "requestedMethod": method,
            },
        ),
        provider_request={"managedOrderRequest": provider_request_payload},
        provider_response={"managedOrderResponse": provider_response_payload},
    )
    if payload.payment_status:
        normalized_status = store._normalize_payment_status(payload.payment_status)
        if normalized_status in {"paid", "refunded"}:
            store.apply_payment_status_transition(attempt, new_status=normalized_status)
    elif method is not None:
        store.apply_payment_status_transition(attempt, new_status="paid")
    return attempt


async def _verify_fib_payment_for_managed_order(
    *,
    fib_provider: Any,
    db: Session,
    payload: ManagedOrderPayload,
    auth_user_id: str,
) -> tuple[str, Any]:
    """Re-verify a client-claimed FIB payment against the provider BEFORE the
    managed order is provisioned (SEC-1), and confirm the paid amount matches the
    server-recomputed total (SEC-2).

    Returns ``(attempt_id, provider_status)``. Raises ``HTTPException`` on any
    failure so ``order_profiles`` (real provider spend) is never reached for an
    unpaid, forged, mis-priced, or replayed payment.
    """
    # Imported lazily so the loyalty path never couples to the FIB module and to
    # avoid an import cycle at module load.
    from fib_payment_api import (
        FIBPaymentAPIError,
        _actor_matches_payment_context,
        _as_int,
        _normalize_payment_status,
    )

    provider_payment_id = (
        payload.payment_provider_payment_id or payload.payment_transaction_id or ""
    ).strip()
    if not provider_payment_id:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="A FIB payment reference is required.",
        )

    def _load_attempt_and_quote() -> tuple[str, str, int]:
        store = SupabaseStore(db)
        attempt = store.get_payment_attempt_by_provider_payment_id(
            provider="fib", provider_payment_id=provider_payment_id, for_update=True
        ) or store.get_payment_attempt_by_transaction_id(
            provider_payment_id, for_update=True
        )
        if attempt is None:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="No matching FIB payment was found.",
            )
        if not _actor_matches_payment_context(
            owner_user_id=auth_user_id, owner_admin_user_id=None, row=attempt
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This payment belongs to another account.",
            )
        # Replay guard: a paid FIB payment may finalize exactly ONE eSIM order. An
        # idempotent re-submit (same provider transactionId) is allowed; a
        # different order (or a top-up) reusing the same payment is rejected.
        _ensure_attempt_free_for_order(db, attempt, payload.provider_request.transaction_id)
        # Recompute the authoritative IQD total from the provider-request cost
        # (the same value order_profiles charges), never from client price fields.
        package_info = payload.provider_request.package_info_list[0]
        provider_minor = package_info.price
        if not provider_minor or provider_minor <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order is missing a provider price; cannot verify payment.",
            )
        quote = store.quote_esim_sale_prices(
            [
                {
                    "packageCode": package_info.package_code,
                    "countryCode": payload.country_code,
                    "providerPriceMinor": provider_minor,
                }
            ],
            currency_code="IQD",
        )
        expected_total = quote.get(package_info.package_code)
        if expected_total is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unable to compute the order total for payment verification.",
            )
        resolved_pid = attempt.provider_payment_id or provider_payment_id
        attempt_id = attempt.id
        # Release the pool slot during the provider round-trip below.
        db.close()
        return attempt_id, resolved_pid, expected_total

    attempt_id, resolved_provider_payment_id, expected_total_minor = await asyncio.to_thread(
        _load_attempt_and_quote
    )

    try:
        provider_status = await fib_provider.get_payment_status(resolved_provider_payment_id)
    except FIBPaymentAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not verify the payment with FIB. Please try again.",
        )

    if _normalize_payment_status(provider_status.status) != "paid":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment has not been confirmed by FIB.",
        )
    amount = provider_status.amount
    if amount is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment confirmation did not include an amount.",
        )
    if (amount.currency or "").upper() != "IQD":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment currency does not match the order.",
        )
    if _as_int(amount.amount, default=-1) != expected_total_minor:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Paid amount does not match the order total.",
        )

    # BE-4: claim the payment for THIS order transaction in a locked transaction
    # BEFORE the provider spend, so a concurrent request reusing the same
    # payment short-circuits with 409 instead of double-spending provider
    # credit in the verify→persist window.
    def _claim() -> None:
        attempt = SupabaseStore(db).get_payment_attempt_by_id(attempt_id, for_update=True)
        if attempt is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Verified payment could not be finalized.",
            )
        already_bound = _ensure_attempt_free_for_order(
            db, attempt, payload.provider_request.transaction_id
        )
        if not already_bound:
            meta = dict(attempt.metadata_payload or {})
            meta["orderClaim"] = {
                "transactionId": payload.provider_request.transaction_id,
                "claimedAt": utcnow().isoformat(),
            }
            attempt.metadata_payload = meta
        db.commit()

    await asyncio.to_thread(_claim)
    return attempt_id, provider_status


def _ensure_attempt_free_for_order(db: Session, attempt: Any, order_transaction_id: str) -> bool:
    """Replay/cross-use guard for order payments. Returns True when the attempt
    is already bound to THIS order (idempotent resubmit — no claim needed),
    False when it is free to claim. Raises 409 on any other reuse."""
    meta = attempt.metadata_payload or {}
    if meta.get("topupClaim"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payment has already been used for a top-up.",
        )
    if attempt.customer_order_id is not None:
        bound_item = (
            db.scalar(select(OrderItem).where(OrderItem.id == attempt.order_item_id))
            if attempt.order_item_id
            else None
        )
        if bound_item is None or bound_item.provider_transaction_id != order_transaction_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This payment has already been used for another order.",
            )
        return True
    claim = meta.get("orderClaim")
    if claim and claim.get("transactionId") != order_transaction_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payment has already been used for another order.",
        )
    return False


def _release_order_claim(db: Session, attempt_id: str, order_transaction_id: str) -> None:
    """Best-effort un-claim after a failed provider order so the customer can
    retry with the same (still unspent) payment."""
    try:
        attempt = SupabaseStore(db).get_payment_attempt_by_id(attempt_id, for_update=True)
        if attempt is None:
            return
        claim = (attempt.metadata_payload or {}).get("orderClaim")
        if claim and claim.get("transactionId") == order_transaction_id:
            meta = dict(attempt.metadata_payload)
            meta.pop("orderClaim", None)
            attempt.metadata_payload = meta
            db.commit()
    except Exception:
        LOGGER.warning("order.claim_release_failed attempt=%s", attempt_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            LOGGER.warning("order.claim_release_rollback_failed attempt=%s", attempt_id, exc_info=True)


def _ensure_attempt_free_for_topup(attempt: Any, topup_transaction_id: str) -> None:
    """Replay guard for top-up payments: a FIB payment may fund exactly ONE
    top-up (idempotent re-submits of the same top-up transaction are allowed),
    and never a top-up if it already paid for an order."""
    meta = attempt.metadata_payload or {}
    if (
        attempt.customer_order_id is not None
        or attempt.order_item_id is not None
        or meta.get("orderClaim")
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payment has already been used for another order.",
        )
    claim = meta.get("topupClaim")
    if claim and claim.get("transactionId") != topup_transaction_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payment has already been used for another top-up.",
        )


def _release_topup_claim(db: Session, attempt_id: str, topup_transaction_id: str) -> None:
    """Best-effort un-claim after a failed provider top-up so the customer can
    retry with the same (still unspent) payment."""
    try:
        attempt = SupabaseStore(db).get_payment_attempt_by_id(attempt_id, for_update=True)
        if attempt is None:
            return
        claim = (attempt.metadata_payload or {}).get("topupClaim")
        if claim and claim.get("transactionId") == topup_transaction_id:
            meta = dict(attempt.metadata_payload)
            meta.pop("topupClaim", None)
            attempt.metadata_payload = meta
            db.commit()
    except Exception:
        LOGGER.warning("topup.claim_release_failed attempt=%s", attempt_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            LOGGER.warning("topup.claim_release_rollback_failed attempt=%s", attempt_id, exc_info=True)


async def _verify_fib_payment_for_managed_topup(
    *,
    fib_provider: Any,
    db: Session,
    provider_payment_id: str,
    auth_user_id: str,
    expected_total_minor: int,
    topup_transaction_id: str,
) -> str:
    """Server-side FIB verification for a managed top-up (BE-3), mirroring
    _verify_fib_payment_for_managed_order: the payment must exist, belong to
    the acting user, and be confirmed paid at the server-recomputed IQD total.

    The attempt is then CLAIMED for this top-up transaction id in a locked
    transaction BEFORE the provider spend, so a concurrent request reusing the
    same payment short-circuits with 409 instead of double-spending credit.
    Returns the claimed attempt id.
    """
    from fib_payment_api import (
        FIBPaymentAPIError,
        _actor_matches_payment_context,
        _as_int,
        _normalize_payment_status,
    )

    pid = (provider_payment_id or "").strip()
    if not pid:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="A FIB payment reference is required.",
        )

    def _load_attempt() -> tuple[str, str]:
        store = SupabaseStore(db)
        attempt = store.get_payment_attempt_by_provider_payment_id(
            provider="fib", provider_payment_id=pid, for_update=True
        ) or store.get_payment_attempt_by_transaction_id(pid, for_update=True)
        if attempt is None:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="No matching FIB payment was found.",
            )
        if not _actor_matches_payment_context(
            owner_user_id=auth_user_id, owner_admin_user_id=None, row=attempt
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This payment belongs to another account.",
            )
        _ensure_attempt_free_for_topup(attempt, topup_transaction_id)
        resolved = attempt.provider_payment_id or pid
        attempt_id = attempt.id
        # Release the pool slot during the provider round-trip below.
        db.close()
        return attempt_id, resolved

    attempt_id, resolved_pid = await asyncio.to_thread(_load_attempt)

    try:
        provider_status = await fib_provider.get_payment_status(resolved_pid)
    except FIBPaymentAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not verify the payment with FIB. Please try again.",
        )

    if _normalize_payment_status(provider_status.status) != "paid":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment has not been confirmed by FIB.",
        )
    amount = provider_status.amount
    if amount is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment confirmation did not include an amount.",
        )
    if (amount.currency or "").upper() != "IQD":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment currency does not match the top-up.",
        )
    if _as_int(amount.amount, default=-1) != expected_total_minor:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Paid amount does not match the top-up total.",
        )

    def _claim() -> None:
        attempt = SupabaseStore(db).get_payment_attempt_by_id(attempt_id, for_update=True)
        if attempt is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Verified payment could not be finalized.",
            )
        _ensure_attempt_free_for_topup(attempt, topup_transaction_id)
        meta = dict(attempt.metadata_payload or {})
        meta["topupClaim"] = {
            "transactionId": topup_transaction_id,
            "claimedAt": utcnow().isoformat(),
        }
        attempt.metadata_payload = meta
        db.commit()

    await asyncio.to_thread(_claim)
    return attempt_id


# The eSIM Access webhook-save API accepts a bare URL only (WebhookConfigRequest
# — no custom headers), so the secret necessarily travels in the URL path/query.
# Log that once per process as a reminder to keep the URL treated as a
# credential (scrubbed logs, periodic rotation) — not on every event.
_url_secret_noted = False


def _require_valid_esim_webhook_secret(
    *,
    header_secret: str | None,
    alternate_header_secret: str | None,
    query_secret: str | None,
    path_secret: str | None = None,
) -> None:
    global _url_secret_noted
    configured_secret = str(get_settings().esim_access_webhook_secret or "").strip()
    if not configured_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="eSIM Access webhook secret is not configured on this deployment.",
        )
    candidates = [
        str(header_secret or ""),
        str(alternate_header_secret or ""),
        str(query_secret or ""),
        str(path_secret or ""),
    ]
    if not any(candidate and hmac.compare_digest(candidate, configured_secret) for candidate in candidates):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid eSIM Access webhook secret.")
    if not _url_secret_noted and not (header_secret or alternate_header_secret) and (query_secret or path_secret):
        _url_secret_noted = True
        LOGGER.info(
            "esim_webhook.secret_via_url: provider sends the secret in the URL "
            "(its webhook API takes a bare URL, no headers) — treat the webhook "
            "URL as a credential: scrub URLs from access logs and rotate "
            "ESIM_ACCESS_WEBHOOK_SECRET periodically."
        )


async def stop_periodic_usage_sync_worker(app: FastAPI) -> None:
    """Cancel and await the background eSIM usage-sync task, if running.

    Invoked from app.py's lifespan shutdown (replaces the deprecated
    @app.on_event("shutdown") handler).
    """
    task = getattr(app.state, "esim_usage_sync_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        app.state.esim_usage_sync_task = None


def register_esim_access_routes(
    app: FastAPI,
    get_db: Callable[..., Any],
    get_provider: Callable[..., ESimAccessAPI],
) -> None:
    usage_sync_interval_seconds = ESimAccessAPI._read_float_env(
        "ESIM_USAGE_SYNC_INTERVAL_SECONDS",
        3600.0,  # hourly — matches the GitHub Action cron cadence.
        minimum=300.0,
    )
    usage_sync_batch_size = ESimAccessAPI._read_int_env("ESIM_USAGE_SYNC_BATCH_SIZE", 50, minimum=1)
    usage_sync_enabled = _read_bool_env("ESIM_USAGE_SYNC_ENABLED", default=False)
    usage_sync_skip_if_busy = _read_bool_env("ESIM_USAGE_SYNC_SKIP_IF_BUSY", default=True)
    usage_sync_initial_delay_seconds = ESimAccessAPI._read_float_env(
        "ESIM_USAGE_SYNC_INITIAL_DELAY_SECONDS",
        45.0,
        minimum=0.0,
    )
    usage_sync_lock = asyncio.Lock()
    exchange_rate_settings_cache: dict[str, Any] | None = None
    exchange_rate_settings_retry_after = 0.0
    public_db_failure_backoff_seconds = max(float(os.getenv("PUBLIC_DB_FAILURE_BACKOFF_SECONDS", "15")), 0.0)

    def _default_exchange_rate_settings() -> dict[str, Any]:
        return {
            "enableIQD": False,
            "exchangeRate": "1320",
            "markupPercent": "0",
            "source": "tulip-admin",
            "updatedAt": _to_utc_z(utcnow()),
        }

    def _serialize_exchange_rate_settings(exchange: Any | None) -> dict[str, Any]:
        if exchange is None:
            return _default_exchange_rate_settings()
        custom = exchange.custom_fields or {}
        enable_iqd = _to_bool(
            custom.get("enableIQD", custom.get("enable_iqd")),
            default=True,
        )
        markup_percent = _format_number_as_string(
            custom.get("markupPercent", custom.get("markup_percent", 0)),
            "0",
        )
        source = str(exchange.source or custom.get("source") or "tulip-admin").strip() or "tulip-admin"
        return {
            "enableIQD": enable_iqd,
            "exchangeRate": _format_number_as_string(exchange.rate, "1320"),
            "markupPercent": markup_percent,
            "source": source,
            "updatedAt": _to_utc_z(exchange.updated_at),
        }

    # Plain `def` dependencies: the only work is synchronous DB lookup via
    # require_active_subject (no awaits), so FastAPI runs them in a worker
    # thread instead of blocking the event loop.
    def _require_permission(flag: str) -> Callable[..., AdminUser]:
        """SEC-3: per-route admin permission gate (mirrors admin.py). ``owner``/
        ``super_admin`` bypass the granular flags; every other admin must have
        the specific permission column (e.g. ``can_manage_orders``) set,
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

    def _require_active_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AppUser | AdminUser:
        row = require_active_subject(db, claims=claims)
        assert isinstance(row, (AppUser, AdminUser))
        return row

    def _require_topup_profile_access(db: Session, actor: AppUser | AdminUser, request: TopUpRequest) -> None:
        if isinstance(actor, AdminUser):
            return
        profile = None
        if request.iccid:
            profile = db.scalar(select(ESimProfile).where(ESimProfile.iccid == request.iccid))
        elif request.esim_tran_no:
            profile = db.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == request.esim_tran_no))
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
        if profile.user_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Profile ownership mismatch.")

    def _serialize_profiles_for_user(
        *,
        store: SupabaseStore,
        user_id: str,
        limit: int,
        offset: int,
        status_filter: str | None,
        installed_filter: bool | None,
        include_terminal: bool = True,
    ) -> dict[str, Any]:
        rows = store.list_profiles_for_user(user_id=user_id)
        now = utcnow()
        serialized = [_serialize_profile(row, now=now) for row in rows]
        normalized_status: str | None = None
        if status_filter is not None and status_filter.strip():
            normalized_status = status_filter.strip().lower()
            if normalized_status in {"all", "any", "*"}:
                normalized_status = ""
            if normalized_status in {"booked", "got_resource", "released", "pending_install", "pending"}:
                normalized_status = "inactive"
            if normalized_status in {"provider-waiting", "provider waiting", "waiting", "onboard", "onboarded"}:
                normalized_status = "provider_waiting"
            if normalized_status in {"cancelled", "canceled", "revoked", "refunded", "voided", "closed"}:
                normalized_status = "expired"
            if normalized_status:
                serialized = [row for row in serialized if str(row.get("status") or "").strip().lower() == normalized_status]
        # When the caller didn't ask for a specific lifecycle bucket, hide
        # terminal profiles (CANCELLED / REVOKED / REFUNDED / EXPIRED) by
        # default. The "my eSIMs" list was getting bloated with one-day plans
        # the user already cycled through. Pass `?includeTerminal=true` or
        # `?status=expired` to surface them again.
        if not include_terminal and not normalized_status:
            serialized = [
                row for row in serialized
                if str(row.get("status") or "").strip().lower() != "expired"
            ]
        if installed_filter is not None:
            serialized = [row for row in serialized if bool(row.get("installed")) is installed_filter]
        total = len(serialized)
        paged_profiles = serialized[offset : offset + limit]
        return {
            "profiles": paged_profiles,
            "limit": limit,
            "offset": offset,
            "total": total,
        }

    def _empty_usage_sync_summary() -> dict[str, int]:
        return {
            "esimTranNosRequested": 0,
            "providerCalls": 0,
            "usageRecordsReceived": 0,
            "profilesSynced": 0,
        }

    async def _sync_usage_for_esim_tran_nos(
        *,
        db: Session,
        provider: ESimAccessAPI,
        esim_tran_nos: list[str],
        actor_phone: str | None,
    ) -> dict[str, int]:
        unique_esim_tran_nos = _dedupe_esim_tran_nos(esim_tran_nos)
        if not unique_esim_tran_nos:
            return _empty_usage_sync_summary()

        provider_calls = 0
        usage_records_received = 0
        synced_profile_ids: set[int] = set()
        for batch in _chunk_values(unique_esim_tran_nos, size=usage_sync_batch_size):
            # Avoid holding a checked-out DB connection while waiting on provider IO.
            db.close()
            provider_calls += 1
            provider_response = await provider.usage_check(UsageCheckRequest(esimTranNoList=batch))
            provider_payload = provider_response.model_dump(by_alias=True, exclude_none=True)
            usage_records = ((provider_payload.get("obj") or {}).get("esimUsageList") or [])
            usage_records_received += len(usage_records)
            # Offload the synchronous DB write to a worker thread so it does not
            # block the event loop while inside the batch loop.
            def _sync_usage_batch() -> list[int]:
                synced_profiles = SupabaseStore(db).sync_usage_records(provider_payload, actor_phone=actor_phone)
                return [int(profile.id) for profile in synced_profiles if profile.id is not None]

            for profile_id in await asyncio.to_thread(_sync_usage_batch):
                synced_profile_ids.add(profile_id)
        return {
            "esimTranNosRequested": len(unique_esim_tran_nos),
            "providerCalls": provider_calls,
            "usageRecordsReceived": usage_records_received,
            "profilesSynced": len(synced_profile_ids),
        }

    def _list_all_esim_tran_nos(db: Session) -> list[str]:
        values = db.scalars(
            select(ESimProfile.esim_tran_no).where(ESimProfile.esim_tran_no.is_not(None))
        ).all()
        return _dedupe_esim_tran_nos(list(values))

    async def _run_periodic_usage_sync_once() -> dict[str, Any]:
        session_factory = getattr(app.state, "db_session_factory", None)
        provider = getattr(app.state, "esim_access_api", None)
        if session_factory is None or provider is None:
            return {"enabled": False, "reason": "runtime state unavailable"}
        if usage_sync_lock.locked():
            return {"enabled": True, "skipped": True, "reason": "sync already in progress"}

        db = session_factory()
        try:
            esim_tran_nos = _list_all_esim_tran_nos(db)
            db.close()
            async with usage_sync_lock:
                summary = await _sync_usage_for_esim_tran_nos(
                    db=db,
                    provider=provider,
                    esim_tran_nos=esim_tran_nos,
                    actor_phone="system:scheduled-usage-sync",
                )
            return {"enabled": True, **summary}
        finally:
            db.close()

    async def _periodic_usage_sync_worker() -> None:
        if usage_sync_initial_delay_seconds > 0:
            await asyncio.sleep(usage_sync_initial_delay_seconds)
        while True:
            try:
                summary = await _run_periodic_usage_sync_once()
                LOGGER.info("Scheduled eSIM usage sync summary: %s", summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - background worker protection
                LOGGER.exception("Scheduled eSIM usage sync failed: %s", exc)
            await asyncio.sleep(usage_sync_interval_seconds)

    async def _start_periodic_usage_sync_worker() -> None:
        if not usage_sync_enabled:
            LOGGER.info("Scheduled eSIM usage sync skipped: disabled by ESIM_USAGE_SYNC_ENABLED.")
            return
        session_factory = getattr(app.state, "db_session_factory", None)
        provider = getattr(app.state, "esim_access_api", None)
        if session_factory is None or provider is None:
            LOGGER.info("Scheduled eSIM usage sync skipped: runtime state unavailable.")
            return
        existing_task = getattr(app.state, "esim_usage_sync_task", None)
        if existing_task is not None and not existing_task.done():
            return
        app.state.esim_usage_sync_task = asyncio.create_task(_periodic_usage_sync_worker())
        LOGGER.info(
            "Scheduled eSIM usage sync started: initial_delay=%ss interval=%ss batch_size=%s",
            usage_sync_initial_delay_seconds,
            usage_sync_interval_seconds,
            usage_sync_batch_size,
        )

    # BE-1: started from app.py's lifespan (replaces the deprecated
    # @app.on_event("startup") handler). Exposed on app.state so the lifespan can
    # invoke it once the DB/provider runtime state is initialized.
    app.state.start_esim_usage_sync_worker = _start_periodic_usage_sync_worker


    @app.post("/api/v1/esim-access/packages/query")
    async def query_packages(
        payload: PackageQueryRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        provider_response = await provider.get_packages(payload)
        raw_payload = provider_response.model_dump(by_alias=True, exclude_none=True)
        # Filter 1-day unlimited from the public catalog only. Top-up queries
        # (type=TOPUP + iccid) keep every package the provider returns —
        # otherwise we'd hide top-up SKUs that legitimately have validityDays
        # = 1 unlimited shape on the provider side.
        is_topup_query = (payload.type or "").strip().upper() == "TOPUP" or bool(payload.iccid)
        augmented = _augment_package_list_response(raw_payload, drop_daily_unlimited=not is_topup_query)
        # Attach the rule-applied IQD sale price per package so the displayed
        # catalog price matches checkout (per-country / per-bundle pricing rules).
        # Offloaded to a thread (sync DB) and best-effort — a quote failure must
        # never break the catalog.
        try:
            await asyncio.to_thread(_apply_sale_prices, db, augmented)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("packages.quote_failed detail=%s", exc)
        return augmented

    @app.post("/api/v1/esim-access/orders")
    async def create_order(
        payload: OrderProfilesRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[OrderResult]:
        return await provider.order_profiles(payload)

    @app.post("/api/v1/esim-access/orders/managed")
    async def create_managed_order(
        payload: ManagedOrderPayload,
        request: Request,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        # Synchronous auth + snapshot + session release: offload to a worker
        # thread so the blocking DB read does not run on the event loop.
        def _prepare_managed_order() -> tuple[dict[str, Any], str, str]:
            auth_user = require_active_subject(db, claims=claims, subject_type="user")
            assert isinstance(auth_user, AppUser)
            # Loyalty is a comped payment method reserved for loyalty (VIP/staff)
            # accounts. Enforce server-side — the UI hides it for normal users, but
            # we must not rely on the client. Reject BEFORE the provider order call
            # so a tampered request can never spend provider credit for free.
            requested_method, _ = _resolve_payment_method_provider(payload)
            if requested_method == "loyalty" and not bool(auth_user.is_loyalty):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Loyalty checkout is not available for this account.",
                )
            # Snapshot the user fields we'll need AFTER detaching the session,
            # then release the DB pool slot before the slow provider call. With
            # the default pool size of 8 and 4 overflow, holding sessions across
            # a 2-5 s provider round-trip drains capacity fast — every other
            # request blocks on pool checkout. Reopen lazily for the write.
            user_snapshot = {
                "phone": auth_user.phone,
                "name": auth_user.name,
                "email": auth_user.email,
                "status": auth_user.status,
                "is_loyalty": auth_user.is_loyalty,
                "notes": auth_user.notes,
            }
            auth_user_phone = auth_user.phone
            auth_user_id = auth_user.id
            db.close()
            return user_snapshot, auth_user_phone, auth_user_id

        user_snapshot, auth_user_phone, auth_user_id = await asyncio.to_thread(_prepare_managed_order)
        resolved_payment_method, resolved_payment_provider = _resolve_payment_method_provider(payload)

        # SECURITY (SEC-1/SEC-2): never spend provider credit on client say-so.
        # Verify payment server-side BEFORE provisioning. Loyalty was already
        # comp-gated in _prepare_managed_order; FIB is re-verified against the
        # provider with a server-recomputed amount; any other/missing method is
        # rejected outright so the provider order can never be placed for free.
        verified_fib_status: Any = None
        verified_attempt_id: str | None = None
        if resolved_payment_method == "loyalty":
            pass
        elif resolved_payment_method == "fib":
            fib_provider = getattr(request.app.state, "fib_payment_api", None)
            if fib_provider is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="FIB payments are not configured on this deployment.",
                )
            verified_attempt_id, verified_fib_status = await _verify_fib_payment_for_managed_order(
                fib_provider=fib_provider,
                db=db,
                payload=payload,
                auth_user_id=auth_user_id,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported payment method for managed checkout.",
            )

        try:
            provider_response = await provider.order_profiles(payload.provider_request)
        except Exception:
            # The provider spend failed — free the claimed payment for a retry.
            if verified_attempt_id is not None:
                await asyncio.to_thread(
                    _release_order_claim,
                    db,
                    verified_attempt_id,
                    payload.provider_request.transaction_id,
                )
            raise
        if not bool(getattr(provider_response, "success", True)):
            # Provider rejected the order WITHOUT raising (defense-in-depth —
            # the real client raises on non-success, but the top-up path
            # defends against soft failures and this path must too, or a
            # rejected order would be persisted and the verified payment
            # consumed with no activation data behind it (audit M2). Free the
            # claim and let the global ESimAccessAPIError handler shape the
            # error response.
            if verified_attempt_id is not None:
                await asyncio.to_thread(
                    _release_order_claim,
                    db,
                    verified_attempt_id,
                    payload.provider_request.transaction_id,
                )
            soft_error_code = getattr(provider_response, "error_code", None)
            soft_error_msg = getattr(provider_response, "error_msg", None)
            raise ESimAccessAPIError(
                error_code=(
                    str(soft_error_code)
                    if soft_error_code not in (None, "")
                    else "ESIM_ORDER_PROVIDER_REJECTED"
                ),
                error_message=soft_error_msg,
                status_code=None,
                provider_message=soft_error_msg,
                request_id=None,
            )
        provider_request_payload = payload.provider_request.model_dump(by_alias=True, exclude_none=True)
        provider_response_payload = provider_response.model_dump(by_alias=True, exclude_none=True)

        # Persist the managed order + payment attempt. This is a contiguous block
        # of synchronous SQLAlchemy work (including the commit/rollback) so it is
        # offloaded wholesale to a worker thread to keep the event loop free.
        def _persist_managed_order() -> tuple[Any, Any, Any, Any]:
            store = SupabaseStore(db)
            try:
                customer_order, order_item = store.save_managed_order(
                    user_data=user_snapshot,
                    platform_code=payload.platform_code,
                    platform_name=payload.platform_name,
                    order_request=provider_request_payload,
                    provider_response=provider_response_payload,
                    currency_code=payload.currency_code,
                    provider_currency_code=payload.provider_currency_code,
                    exchange_rate=payload.exchange_rate,
                    sale_price_minor=payload.sale_price_minor,
                    # SEC-2: ignore the client-supplied provider cost. save_managed_order
                    # falls back to the provider-request price (what we actually order),
                    # so a tampered providerPriceMinor cannot shrink the server total.
                    provider_price_minor=None,
                    country_code=payload.country_code,
                    country_name=payload.country_name,
                    package_code=payload.package_code,
                    package_slug=payload.package_slug,
                    package_name=payload.package_name,
                    payment_method=resolved_payment_method,
                    payment_provider=resolved_payment_provider,
                    custom_fields=payload.custom_fields,
                    auto_commit=False,
                )
                if resolved_payment_method == "fib":
                    # Bind the server-verified FIB attempt and persist its verified
                    # status in the SAME commit as the order. Re-check ownership of
                    # the order to close the verify→persist race.
                    from fib_payment_api import _apply_verified_status

                    payment_attempt = store.get_payment_attempt_by_id(
                        verified_attempt_id, for_update=True
                    )
                    if payment_attempt is None:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Verified payment could not be finalized.",
                        )
                    if (
                        payment_attempt.customer_order_id is not None
                        and payment_attempt.customer_order_id != customer_order.id
                    ):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="This payment has already been used for another order.",
                        )
                    _apply_verified_status(
                        store=store,
                        row=payment_attempt,
                        provider_payment_id=payment_attempt.provider_payment_id,
                        provider_status=verified_fib_status,
                    )
                    store.link_payment_attempt_to_order(
                        payment_attempt=payment_attempt,
                        customer_order=customer_order,
                        order_item=order_item,
                    )
                else:
                    payment_attempt = _resolve_or_create_payment_for_managed_order(
                        store=store,
                        payload=payload,
                        customer_order_id=customer_order.id,
                        order_item_id=order_item.id,
                        user_id=customer_order.user_id,
                        service_type=order_item.service_type,
                        amount_minor=order_item.sale_price_minor or customer_order.total_minor or 0,
                        currency_code=customer_order.currency_code or "IQD",
                        provider_request_payload=provider_request_payload,
                        provider_response_payload=provider_response_payload,
                    )
                if payment_attempt is not None:
                    order_item.payment_method = payment_attempt.payment_method
                    order_item.payment_provider = payment_attempt.provider
                    customer_order.payment_method = payment_attempt.payment_method
                    customer_order.payment_provider = payment_attempt.provider
                db.commit()
            except Exception:
                db.rollback()
                raise
            db.refresh(customer_order)
            db.refresh(order_item)
            if payment_attempt is not None:
                db.refresh(payment_attempt)
            return store, customer_order, order_item, payment_attempt

        store, customer_order, order_item, payment_attempt = await asyncio.to_thread(_persist_managed_order)
        profiles_synced = 0
        profile_sync_error: str | None = None
        profile_sync_attempts = 0
        profile_sync_triggered = bool(order_item.provider_order_no)
        # Keep checkout responsive. We create the local order/profile row
        # first, try one immediate provider reconciliation, then let the
        # detail-screen recover poll fill activation data as the provider
        # materializes it.
        if profile_sync_triggered and hasattr(provider, "query_profiles"):
            import asyncio as _asyncio
            from supabase_store import ESimProfile as _ESimProfile
            from sqlalchemy import select as _select
            backoffs = (0.0,)
            order_item_id = order_item.id
            provider_order_no_snapshot = order_item.provider_order_no
            for attempt_index, delay in enumerate(backoffs):
                profile_sync_attempts = attempt_index + 1
                if delay > 0:
                    await _asyncio.sleep(delay)
                try:
                    # Release the pool slot during the provider round-trip,
                    # same pattern as /profiles/{id}/recover.
                    db.close()
                    provider_sync_response = await provider.query_profiles(
                        ProfileQueryRequest(order_no=provider_order_no_snapshot)
                    )

                    # Contiguous synchronous DB section: sync + refetch. Offload
                    # to a worker thread so it does not block the event loop.
                    def _reconcile_synced_profiles() -> tuple[int, Any, Any, str | None, bool]:
                        synced = store.sync_profiles(
                            provider_sync_response.model_dump(by_alias=True, exclude_none=True),
                            platform_code=payload.platform_code,
                            platform_name=payload.platform_name,
                            actor_phone=auth_user_phone,
                        )
                        synced_count = len(synced)
                        # Refetch from a fresh session — the original order_item
                        # is detached after db.close().
                        refetched_order_item = db.scalar(
                            _select(OrderItem).where(OrderItem.id == order_item_id)
                        )
                        refetched_customer_order = (
                            refetched_order_item.customer_order
                            if refetched_order_item is not None
                            else customer_order
                        )
                        # Did we actually get activation data for our profile?
                        refreshed_profile = db.scalar(
                            _select(_ESimProfile).where(_ESimProfile.order_item_id == refetched_order_item.id)
                        )
                        local_error: str | None = profile_sync_error
                        should_break = False
                        if refreshed_profile and refreshed_profile.activation_code:
                            local_error = None
                            should_break = True
                        elif attempt_index == len(backoffs) - 1:
                            # Final attempt and we still have nothing — record it.
                            local_error = (
                                "Provider returned no activation data after "
                                f"{profile_sync_attempts} attempts"
                            )
                        return (
                            synced_count,
                            refetched_order_item,
                            refetched_customer_order,
                            local_error,
                            should_break,
                        )

                    (
                        profiles_synced,
                        order_item,
                        customer_order,
                        profile_sync_error,
                        _should_break,
                    ) = await asyncio.to_thread(_reconcile_synced_profiles)
                    if _should_break:
                        break
                except Exception as exc:  # pragma: no cover - best-effort sync hardening
                    # Synchronous rollback + refetch — offload to a worker thread.
                    def _recover_from_sync_error() -> tuple[Any, Any]:
                        try:
                            db.rollback()
                        except Exception:
                            # A failed rollback can leave the connection poisoned
                            # for the refetch below — surface it in logs.
                            LOGGER.warning("profile_sync.rollback_failed", exc_info=True)
                        # The original ORM objects are detached after db.close().
                        # Refetch by primary key from a fresh session.
                        recovered_order_item = db.scalar(
                            _select(OrderItem).where(OrderItem.id == order_item_id)
                        )
                        recovered_customer_order = customer_order
                        if recovered_order_item is not None:
                            recovered_customer_order = recovered_order_item.customer_order
                        return recovered_order_item, recovered_customer_order

                    profile_sync_error = str(exc)
                    order_item, customer_order = await asyncio.to_thread(_recover_from_sync_error)
                    # Don't retry on hard errors — provider is unhealthy or we
                    # have a code bug. Bail.
                    break

        response_payload = {
            "provider": provider_response_payload,
            "database": {
                "customerOrderId": customer_order.id,
                "orderNumber": customer_order.order_number,
                "orderItemId": order_item.id,
                "providerOrderNo": order_item.provider_order_no,
                "orderNo": order_item.provider_order_no,
                "pricing": {
                    "currencyCode": customer_order.currency_code,
                    "exchangeRate": customer_order.exchange_rate,
                    "subtotalMinor": customer_order.subtotal_minor,
                    "markupMinor": customer_order.markup_minor,
                    "discountMinor": customer_order.discount_minor,
                    "totalMinor": customer_order.total_minor,
                },
                "payment": (
                    {
                        "paymentAttemptId": payment_attempt.id,
                        "paymentMethod": payment_attempt.payment_method,
                        "provider": payment_attempt.provider,
                        "status": payment_attempt.status,
                        "transactionId": payment_attempt.transaction_id,
                        "providerPaymentId": payment_attempt.provider_payment_id,
                    }
                    if payment_attempt is not None
                    else None
                ),
                "profileSync": {
                    "triggered": profile_sync_triggered,
                    "profilesSynced": profiles_synced,
                    "attempts": profile_sync_attempts,
                    "error": profile_sync_error,
                },
            },
        }
        return {
            "success": True,
            "data": response_payload,
            "providerOrderNo": order_item.provider_order_no,
            "orderNo": order_item.provider_order_no,
            **response_payload,
        }

    @app.post("/api/v1/esim-access/profiles/query")
    async def query_profiles(
        payload: ProfileQueryRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        provider_response = await provider.query_profiles(payload)
        raw_payload = provider_response.model_dump(by_alias=True, exclude_none=True)
        return _augment_profile_usage_units(raw_payload)

    @app.post("/api/v1/esim-access/profiles/sync")
    async def sync_profiles(
        payload: ManagedProfileSyncPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        provider_response = await provider.query_profiles(payload.provider_request)

        def _sync_profiles_work() -> int:
            store = SupabaseStore(db)
            profiles = store.sync_profiles(
                provider_response.model_dump(by_alias=True, exclude_none=True),
                platform_code=payload.platform_code,
                platform_name=payload.platform_name,
                actor_phone=payload.actor_phone,
            )
            return len(profiles)

        profiles_synced = await asyncio.to_thread(_sync_profiles_work)
        return {
            "provider": provider_response.model_dump(by_alias=True, exclude_none=True),
            "database": {"profilesSynced": profiles_synced},
        }

    @app.post("/api/v1/esim-access/profiles/cancel")
    async def cancel_profile(
        payload: EsimTranNoRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.cancel_profile(payload)

    @app.post("/api/v1/esim-access/profiles/cancel/managed")
    async def cancel_profile_managed(
        payload: ManagedEsimTranActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        provider_response = await provider.cancel_profile(payload.provider_request)

        def _apply_cancel() -> Any:
            return SupabaseStore(db).apply_profile_action(
                action="cancel",
                identifier_key="esim_tran_no",
                identifier_value=payload.provider_request.esim_tran_no,
                platform_code=payload.context.platform_code,
                actor_phone=payload.context.actor_phone,
                note=payload.context.note,
                payload=provider_response.model_dump(by_alias=True, exclude_none=True),
            )

        profile = await asyncio.to_thread(_apply_cancel)
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/profiles/suspend")
    async def suspend_profile(
        payload: ICCIDRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.suspend_profile(payload)

    @app.post("/api/v1/esim-access/profiles/suspend/managed")
    async def suspend_profile_managed(
        payload: ManagedIccidActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        provider_response = await provider.suspend_profile(payload.provider_request)

        def _apply_suspend() -> Any:
            return SupabaseStore(db).apply_profile_action(
                action="suspend",
                identifier_key="iccid",
                identifier_value=payload.provider_request.iccid,
                platform_code=payload.context.platform_code,
                actor_phone=payload.context.actor_phone,
                note=payload.context.note,
                payload=provider_response.model_dump(by_alias=True, exclude_none=True),
            )

        profile = await asyncio.to_thread(_apply_suspend)
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/profiles/unsuspend")
    async def unsuspend_profile(
        payload: ICCIDRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.unsuspend_profile(payload)

    @app.post("/api/v1/esim-access/profiles/unsuspend/managed")
    async def unsuspend_profile_managed(
        payload: ManagedIccidActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        provider_response = await provider.unsuspend_profile(payload.provider_request)

        def _apply_unsuspend() -> Any:
            return SupabaseStore(db).apply_profile_action(
                action="unsuspend",
                identifier_key="iccid",
                identifier_value=payload.provider_request.iccid,
                platform_code=payload.context.platform_code,
                actor_phone=payload.context.actor_phone,
                note=payload.context.note,
                payload=provider_response.model_dump(by_alias=True, exclude_none=True),
            )

        profile = await asyncio.to_thread(_apply_unsuspend)
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/profiles/revoke")
    async def revoke_profile(
        payload: ICCIDRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.revoke_profile(payload)

    @app.post("/api/v1/esim-access/profiles/revoke/managed")
    async def revoke_profile_managed(
        payload: ManagedIccidActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        provider_response = await provider.revoke_profile(payload.provider_request)

        def _apply_revoke() -> Any:
            return SupabaseStore(db).apply_profile_action(
                action="revoke",
                identifier_key="iccid",
                identifier_value=payload.provider_request.iccid,
                platform_code=payload.context.platform_code,
                actor_phone=payload.context.actor_phone,
                note=payload.context.note,
                payload=provider_response.model_dump(by_alias=True, exclude_none=True),
            )

        profile = await asyncio.to_thread(_apply_revoke)
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/balance/query")
    async def query_balance(
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[BalanceResult]:
        return await provider.balance_query()

    @app.post("/api/v1/esim-access/topups", response_model=None)
    @app.post("/api/v1/esim-access/topup", response_model=None)
    async def top_up(
        payload: TopUpRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> Any:
        try:
            return await provider.top_up(payload)
        except (ESimAccessAPIError, ESimAccessHTTPError) as exc:
            # BE-3: only handle provider errors here; let unexpected internal
            # errors propagate to the app-level handlers (SQLAlchemy->503 etc.)
            # instead of being stringified to the client.
            return build_topup_error_response(exc)

    @app.post("/api/v1/esim-access/topups/managed", response_model=None)
    @app.post("/api/v1/esim-access/topup/managed", response_model=None)
    async def top_up_managed(
        payload: ManagedTopUpPayload,
        request: Request,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        actor: AppUser | AdminUser = Depends(_require_active_actor),
    ) -> Any:
        # Synchronous ownership/access check — offload off the event loop.
        await asyncio.to_thread(_require_topup_profile_access, db, actor, payload.provider_request)

        # SECURITY (BE-3): never spend provider credit on client say-so. Admins
        # may top up as a support action; app users must pay — loyalty is
        # comp-gated server-side, FIB is re-verified against the provider at the
        # server-recomputed IQD price, and the payment is claimed for this
        # top-up transaction BEFORE the spend (replay/double-spend guard).
        topup_transaction_id = payload.provider_request.transaction_id
        verified_attempt_id: str | None = None
        if not isinstance(actor, AdminUser):
            requested_method, _ = _normalize_payment_method(payload.payment_method, None)
            if requested_method == "loyalty":
                if not bool(getattr(actor, "is_loyalty", False)):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Loyalty top-up is not available for this account.",
                    )
            else:
                fib_provider = getattr(request.app.state, "fib_payment_api", None)
                if fib_provider is None:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="FIB payments are not configured on this deployment.",
                    )
                package_code = payload.provider_request.package_code
                # Recompute the authoritative IQD sale price from the provider's
                # own top-up catalog — never from client-supplied numbers. Same
                # quoting recipe the catalog/checkout use (_apply_sale_prices).
                catalog = await provider.get_packages(
                    PackageQueryRequest(
                        type="TOPUP",
                        iccid=payload.provider_request.iccid,
                        package_code=package_code,
                    )
                )
                catalog_payload = catalog.model_dump(by_alias=True, exclude_none=True)
                package_list = (catalog_payload.get("obj") or {}).get("packageList") or []
                match = next(
                    (
                        it
                        for it in package_list
                        if isinstance(it, dict) and it.get("packageCode") == package_code
                    ),
                    None,
                )
                provider_minor = (match or {}).get("price")
                if not provider_minor or provider_minor <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Unable to price the top-up package; cannot verify payment.",
                    )

                def _quote_expected_total() -> int | None:
                    return SupabaseStore(db).quote_esim_sale_prices(
                        [
                            {
                                "packageCode": package_code,
                                "countryCode": _single_country_code(match),
                                "providerPriceMinor": provider_minor,
                            }
                        ],
                        currency_code="IQD",
                    ).get(package_code)

                expected_total_minor = await asyncio.to_thread(_quote_expected_total)
                if expected_total_minor is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Unable to compute the top-up total for payment verification.",
                    )
                verified_attempt_id = await _verify_fib_payment_for_managed_topup(
                    fib_provider=fib_provider,
                    db=db,
                    provider_payment_id=(
                        payload.payment_provider_payment_id or payload.payment_transaction_id or ""
                    ),
                    auth_user_id=actor.id,
                    expected_total_minor=expected_total_minor,
                    topup_transaction_id=topup_transaction_id,
                )

        async def _release_claim_if_any() -> None:
            if verified_attempt_id is not None:
                await asyncio.to_thread(
                    _release_topup_claim, db, verified_attempt_id, topup_transaction_id
                )

        try:
            provider_response = await provider.top_up(payload.provider_request)
        except (ESimAccessAPIError, ESimAccessHTTPError) as exc:
            # BE-3: provider errors only; unexpected internals propagate upward.
            await _release_claim_if_any()
            return build_topup_error_response(exc)
        except Exception:
            await _release_claim_if_any()
            raise
        if not bool(getattr(provider_response, "success", True)):
            # Provider rejected the top-up without raising — the payment was not
            # spent; free it for a retry. Return a real error status as well:
            # falling through to the 200 envelope made apiFetch clients read a
            # failed top-up as success while the claim was already released
            # (audit M1). Reusing build_topup_error_response keeps the error
            # shape identical to the raising path, and the early return keeps a
            # rejected top-up out of the sync_after_topup block (audit L10).
            await _release_claim_if_any()
            soft_error_code = getattr(provider_response, "error_code", None)
            soft_error_msg = getattr(provider_response, "error_msg", None)
            return build_topup_error_response(
                ESimAccessAPIError(
                    error_code=(
                        str(soft_error_code)
                        if soft_error_code not in (None, "")
                        else "ESIM_TOPUP_PROVIDER_REJECTED"
                    ),
                    error_message=soft_error_msg,
                    status_code=None,
                    provider_message=soft_error_msg,
                    request_id=None,
                )
            )
        profiles_synced = 0
        usage_records_synced = 0
        if payload.sync_after_topup:
            store = SupabaseStore(db)
            if payload.provider_request.iccid:
                query_request = ProfileQueryRequest(iccid=payload.provider_request.iccid)
                profile_response = await provider.query_profiles(query_request)

                def _sync_topup_profiles() -> int:
                    profiles = store.sync_profiles(
                        profile_response.model_dump(by_alias=True, exclude_none=True),
                        platform_code=payload.platform_code,
                        platform_name=payload.platform_name,
                        actor_phone=payload.actor_phone,
                    )
                    return len(profiles)

                profiles_synced = await asyncio.to_thread(_sync_topup_profiles)
            if payload.provider_request.esim_tran_no:
                usage_request = UsageCheckRequest(esim_tran_no_list=[payload.provider_request.esim_tran_no])
                async with usage_sync_lock:
                    usage_response = await provider.usage_check(usage_request)

                    def _sync_topup_usage() -> int:
                        usage_profiles = store.sync_usage_records(
                            usage_response.model_dump(by_alias=True, exclude_none=True),
                            actor_phone=payload.actor_phone,
                        )
                        return len(usage_profiles)

                    usage_records_synced = await asyncio.to_thread(_sync_topup_usage)
        return {
            "provider": provider_response.model_dump(by_alias=True, exclude_none=True),
            "database": {
                "profilesSynced": profiles_synced,
                "usageRecordsSynced": usage_records_synced,
                "syncAfterTopup": payload.sync_after_topup,
            },
        }

    @app.post("/api/v1/esim-access/webhooks/configure")
    @app.post("/api/v1/esim-access/webhook/save")
    async def configure_webhook(
        payload: WebhookConfigRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.set_webhook(payload)

    @app.post("/api/v1/esim-access/sms/send")
    async def send_sms(
        payload: SendSmsRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        # SEC-3: sending SMS to a customer's eSIM is messaging, not order ops.
        _: AdminUser = Depends(_require_permission("can_send_push")),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.send_sms(payload)

    @app.post("/api/v1/esim-access/usage/query")
    async def query_usage(
        payload: UsageCheckRequest,
        provider: ESimAccessAPI = Depends(get_provider),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> ESimAccessResponse[UsageResult]:
        return await provider.usage_check(payload)

    @app.post("/api/v1/esim-access/usage/sync")
    async def sync_usage(
        payload: ManagedUsageSyncPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
    ) -> dict[str, Any]:
        # Free the pool slot while waiting on upstream provider network latency.
        db.close()
        async with usage_sync_lock:
            provider_response = await provider.usage_check(payload.provider_request)

            def _sync_usage_work() -> int:
                profiles = SupabaseStore(db).sync_usage_records(
                    provider_response.model_dump(by_alias=True, exclude_none=True),
                    actor_phone=payload.actor_phone,
                )
                return len(profiles)

            profiles_synced = await asyncio.to_thread(_sync_usage_work)
        return {
            "provider": provider_response.model_dump(by_alias=True, exclude_none=True),
            "database": {"profilesSynced": profiles_synced},
        }

    @app.post("/api/v1/esim-access/profiles/{profile_id}/recover")
    async def user_recover_profile(
        profile_id: int,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        """User-facing per-profile sync.

        Looks up the order_item's providerOrderNo, calls query_profiles, applies
        sync_profiles. Returns {ok, hasActivationCode, hasIccid, appStatus}.
        The detail screen polls this after checkout to fill in activation data
        the moment the provider materializes it (faster than waiting for cron).
        """
        from supabase_store import ESimProfile, OrderItem

        # Synchronous lookup + snapshot + session release. This endpoint is polled
        # every 4s, so keep the blocking DB work off the event loop via a thread.
        def _recover_prologue() -> tuple[str | None, str | None, str]:
            actor = require_active_subject(db, claims=claims)
            target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=None)
            profile = db.scalar(
                select(ESimProfile).where(
                    ESimProfile.id == profile_id,
                    ESimProfile.user_id == target_user_id,
                )
            )
            if profile is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
            # Snapshot the identifiers we need from the DB row, then release the
            # connection BEFORE the (potentially slow) provider call. The detail
            # screen polls this every 4s after Activate — if every poll held a
            # pool slot across a 1-3 s provider round-trip, the DATABASE_POOL_SIZE=4
            # pool drains within ~16 s and every other endpoint freezes waiting
            # for a slot. Followed the same pattern used by _sync_usage_for_esim_tran_nos.
            profile_iccid = profile.iccid
            order_no: str | None = None
            if profile.order_item_id is not None:
                oi = db.scalar(select(OrderItem).where(OrderItem.id == profile.order_item_id))
                if oi is not None:
                    order_no = oi.provider_order_no
            actor_phone_value = str(claims.get("phone") or "")
            db.close()
            return profile_iccid, order_no, actor_phone_value

        profile_iccid, order_no, actor_phone_value = await asyncio.to_thread(_recover_prologue)

        if profile_iccid:
            resp = await provider.query_profiles(ProfileQueryRequest(iccid=profile_iccid))
        elif order_no:
            resp = await provider.query_profiles(ProfileQueryRequest(order_no=order_no))
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No iccid or provider_order_no available to recover",
            )

        # Re-acquire a session for the write-back. SQLAlchemy will check out a
        # fresh connection from the pool transparently. Contiguous synchronous
        # DB section (sync + commit + refetch) — offload to a worker thread.
        def _recover_writeback() -> Any:
            SupabaseStore(db).sync_profiles(
                resp.model_dump(by_alias=True, exclude_none=True),
                platform_code="user-recover",
                platform_name="User-initiated recovery",
                actor_phone=actor_phone_value,
            )
            db.commit()
            # Refetch the post-sync profile state via a fresh scalar (the original
            # profile object is detached after db.close()).
            return db.scalar(
                select(ESimProfile).where(ESimProfile.id == profile_id)
            )

        profile = await asyncio.to_thread(_recover_writeback)
        if profile is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile disappeared during recover")
        # Bonus: when the profile already has an esim_tran_no, do a quick
        # follow-up usage_check so this single endpoint also pulls fresh CDR
        # data. The provider's query_profiles "orderUsage" field can lag the
        # cellular session by minutes; usage_check hits the live counter.
        # Best-effort — failures don't abort the response.
        esim_tran_no = profile.esim_tran_no
        if esim_tran_no:
            try:
                db.close()
                usage_resp = await provider.usage_check(
                    UsageCheckRequest(esimTranNoList=[esim_tran_no])
                )

                # Contiguous synchronous DB write + commit + refetch — offload.
                def _recover_usage_writeback() -> Any:
                    SupabaseStore(db).sync_usage_records(
                        usage_resp.model_dump(by_alias=True, exclude_none=True),
                        actor_phone=actor_phone_value,
                    )
                    db.commit()
                    return db.scalar(
                        select(ESimProfile).where(ESimProfile.id == profile_id)
                    )

                profile = await asyncio.to_thread(_recover_usage_writeback)
            except Exception as exc:  # pragma: no cover - best-effort
                LOGGER.warning("recover usage_check failed: %s", str(exc)[:200])
        # Return enough state that the frontend can short-circuit its polling
        # loop without a second round-trip through /usage/sync/my. ACTIVE is
        # only returned after install is confirmed and provider service is
        # active; otherwise provider-active drift remains PROVIDER_WAITING.
        return {
            "ok": True,
            "hasActivationCode": bool(profile.activation_code),
            "hasIccid": bool(profile.iccid),
            "appStatus": profile.app_status,
            "providerStatus": profile.provider_status,
            "installed": bool(profile.installed),
            "activatedAt": (
                profile.activated_at.isoformat() if profile.activated_at is not None else None
            ),
            "expiresAt": (
                profile.expires_at.isoformat() if profile.expires_at is not None else None
            ),
        }

    @app.post(
        "/api/v1/esim-access/usage/sync/my",
        summary="Refresh usage for caller-owned eSIM profiles",
        responses={
            401: {"description": "Missing or invalid bearer token."},
            403: {"description": "Token subject cannot access the requested user scope."},
        },
    )
    @app.post("/api/v1/esim-access/usage/refresh/my")
    async def sync_my_usage(
        request: Request,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        status: str | None = Query(default=None),
        installed: bool | None = Query(default=None),
        user_id: str | None = Query(default=None, alias="userId"),
        include_terminal: bool = Query(default=False, alias="includeTerminal"),
    ) -> dict[str, Any]:
        # Synchronous lookup + snapshot + session release — offload off the loop.
        def _collect_target_esim_tran_nos() -> tuple[str, list[str]]:
            actor = require_active_subject(db, claims=claims)
            target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=user_id)
            store = SupabaseStore(db)
            existing_rows = store.list_profiles_for_user(user_id=target_user_id)
            esim_tran_nos = _collect_esim_tran_nos(existing_rows)
            db.close()
            return target_user_id, esim_tran_nos

        target_user_id, esim_tran_nos = await asyncio.to_thread(_collect_target_esim_tran_nos)

        if esim_tran_nos:
            actor_phone = str(claims.get("phone") or "")
            if usage_sync_skip_if_busy and usage_sync_lock.locked():
                LOGGER.info(
                    "usage.sync.my skipped busy lock user_id=%s path=%s",
                    target_user_id,
                    request.url.path,
                )
                sync_summary = _empty_usage_sync_summary()
            else:
                async with usage_sync_lock:
                    sync_summary = await _sync_usage_for_esim_tran_nos(
                        db=db,
                        provider=provider,
                        esim_tran_nos=esim_tran_nos,
                        actor_phone=actor_phone,
                    )
        else:
            sync_summary = _empty_usage_sync_summary()

        # Re-open session and serialize profiles — synchronous DB work, offloaded.
        def _serialize_after_sync() -> dict[str, Any]:
            db.close()
            store = SupabaseStore(db)
            return _serialize_profiles_for_user(
                store=store,
                user_id=target_user_id,
                limit=limit,
                offset=offset,
                status_filter=status,
                installed_filter=installed,
                include_terminal=include_terminal,
            )

        profile_data = await asyncio.to_thread(_serialize_after_sync)
        return {
            "success": True,
            "data": {
                **profile_data,
                "sync": sync_summary,
            },
        }

    @app.post("/api/v1/esim-access/locations/query")
    async def query_locations(
        payload: EmptyRequest | None = None,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[LocationListResult]:
        return await provider.locations(payload)

    @app.get("/api/v1/esim-access/exchange-rates/current")
    def get_current_exchange_rate_settings(
        response: Response,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        nonlocal exchange_rate_settings_cache, exchange_rate_settings_retry_after
        # The rate + markup change at most a few times a day. Let proxies/CDN
        # and the client reuse the response for 5 min (with a 10 min
        # stale-while-revalidate grace) so app-open pricing renders instantly
        # and we shed repeat traffic off the DB. This is a GET, so HTTP caching
        # actually applies (unlike the POST catalog endpoints, which rely on
        # the in-process provider cache instead).
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        if monotonic() < exchange_rate_settings_retry_after:
            fallback_settings = exchange_rate_settings_cache or _default_exchange_rate_settings()
            return {
                "success": True,
                "data": {
                    **fallback_settings,
                    "cacheStatus": "stale" if exchange_rate_settings_cache is not None else "db_unavailable",
                },
            }
        try:
            store = SupabaseStore(db)
            exchange = store.get_current_exchange_rate_settings()
            exchange_rate_settings_cache = _serialize_exchange_rate_settings(exchange)
            exchange_rate_settings_retry_after = 0.0
            return {
                "success": True,
                "data": {**exchange_rate_settings_cache, "cacheStatus": "fresh"},
            }
        except SQLAlchemyError as exc:
            exchange_rate_settings_retry_after = monotonic() + public_db_failure_backoff_seconds
            if exchange_rate_settings_cache is not None:
                LOGGER.warning("exchange_rates.current_db_unavailable cache=stale detail=%s", exc)
                return {
                    "success": True,
                    "data": {**exchange_rate_settings_cache, "cacheStatus": "stale"},
                }
            LOGGER.warning("exchange_rates.current_db_unavailable cache=default detail=%s", exc)
            return {
                "success": True,
                "data": {**_default_exchange_rate_settings(), "cacheStatus": "db_unavailable"},
            }

    @app.get(
        "/api/v1/esim-access/orders/my",
        summary="List customer orders for the authenticated subject",
    )
    def list_my_orders(
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        user_id: str | None = Query(default=None, alias="userId"),
    ) -> dict[str, Any]:
        # Plain `def`: all work below is synchronous SQLAlchemy/serialization, so
        # FastAPI runs it in a worker thread instead of blocking the event loop.
        actor = require_active_subject(db, claims=claims)
        target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=user_id)
        rows = (
            db.scalars(
                select(CustomerOrder)
                .options(joinedload(CustomerOrder.order_items))
                .where(CustomerOrder.user_id == target_user_id)
                .order_by(
                    func.coalesce(CustomerOrder.booked_at, CustomerOrder.created_at).desc(),
                    CustomerOrder.id.desc(),
                )
            )
            .unique()
            .all()
        )
        total = len(rows)
        paged = rows[offset : offset + limit]
        return {
            "success": True,
            "data": {
                "orders": [_serialize_order(order) for order in paged],
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        }

    @app.get("/api/v1/admin/orders/detailed")
    def list_admin_orders_detailed(
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_permission("can_manage_orders")),
        month: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        # IMPORTANT: this is a *sync* def on purpose. The query + serialization
        # is synchronous SQLAlchemy work over potentially many orders; running it
        # in an `async def` would block the event loop, so /health would time out
        # and Koyeb would kill the instance. As a plain def, FastAPI runs it in a
        # worker thread instead. Filtering, counting and pagination happen in SQL
        # (with selectinload to avoid a joinedload cartesian blow-up) so we never
        # materialise the whole orders table in memory.
        ref = func.coalesce(CustomerOrder.booked_at, CustomerOrder.created_at)
        base = select(CustomerOrder)
        count_query = select(func.count()).select_from(CustomerOrder)

        # Optional month filter (YYYY-MM) applied in SQL as a half-open range.
        if month and re.fullmatch(r"\d{4}-\d{2}", month.strip()):
            year, mon = (int(part) for part in month.strip().split("-"))
            start = datetime(year, mon, 1, tzinfo=timezone.utc)
            end = (
                datetime(year + 1, 1, 1, tzinfo=timezone.utc)
                if mon == 12
                else datetime(year, mon + 1, 1, tzinfo=timezone.utc)
            )
            base = base.where(ref >= start, ref < end)
            count_query = count_query.where(ref >= start, ref < end)

        total = int(db.scalar(count_query) or 0)
        query = (
            base.options(
                selectinload(CustomerOrder.user),
                selectinload(CustomerOrder.order_items).selectinload(OrderItem.profiles),
            )
            .order_by(ref.desc(), CustomerOrder.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = db.scalars(query).unique().all()
        now = utcnow()
        return {
            "success": True,
            "data": {
                "orders": [_serialize_admin_order(order, now=now) for order in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        }

    @app.get(
        "/api/v1/esim-access/profiles/my",
        summary="List eSIM profiles for the authenticated subject",
        responses={
            401: {"description": "Missing or invalid bearer token."},
            403: {"description": "Token subject cannot access the requested user scope."},
        },
    )
    def list_my_profiles(
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        status: str | None = Query(default=None),
        installed: bool | None = Query(default=None),
        user_id: str | None = Query(default=None, alias="userId"),
        include_terminal: bool = Query(default=False, alias="includeTerminal"),
    ) -> dict[str, Any]:
        # Plain `def`: all work below is synchronous SQLAlchemy/serialization, so
        # FastAPI runs it in a worker thread instead of blocking the event loop.
        actor = require_active_subject(db, claims=claims)
        target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=user_id)
        store = SupabaseStore(db)
        profile_data = _serialize_profiles_for_user(
            store=store,
            user_id=target_user_id,
            limit=limit,
            offset=offset,
            status_filter=status,
            installed_filter=installed,
            include_terminal=include_terminal,
        )
        return {
            "success": True,
            "data": profile_data,
        }

    @app.post(
        "/api/v1/esim-access/profiles/install/my",
        summary="Mark caller-owned profile as installed",
        responses={
            401: {"description": "Missing or invalid bearer token."},
            403: {"description": "Ownership mismatch or forbidden target user scope."},
        },
    )
    def install_my_profile(
        payload: MyProfileActionPayload,
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        # Plain `def`: all work below is synchronous SQLAlchemy/serialization, so
        # FastAPI runs it in a worker thread instead of blocking the event loop.
        actor = require_active_subject(db, claims=claims)
        target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=payload.user_id)
        identifier_key, identifier_value = _resolve_profile_identifier(payload)
        store = SupabaseStore(db)
        profile = _lookup_profile_by_identifier(db, identifier_key=identifier_key, identifier_value=identifier_value)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        if profile.user_id != target_user_id:
            raise HTTPException(status_code=403, detail="Profile ownership mismatch.")
        updated = store.apply_profile_action(
            action="install",
            identifier_key=identifier_key,
            identifier_value=identifier_value,
            platform_code=payload.platform_code,
            actor_phone=str(claims.get("phone") or ""),
            note=payload.note,
            payload=payload.model_dump(by_alias=True, exclude_none=True),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        return {"success": True, "data": {"profile": _serialize_profile(updated, now=utcnow())}}

    @app.post(
        "/api/v1/esim-access/profiles/activate/my",
        summary="Mark caller-owned profile as activated",
        responses={
            401: {"description": "Missing or invalid bearer token."},
            403: {"description": "Ownership mismatch or forbidden target user scope."},
        },
    )
    def activate_my_profile(
        payload: MyProfileActionPayload,
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        # Plain `def`: all work below is synchronous SQLAlchemy/serialization, so
        # FastAPI runs it in a worker thread instead of blocking the event loop.
        actor = require_active_subject(db, claims=claims)
        target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=payload.user_id)
        identifier_key, identifier_value = _resolve_profile_identifier(payload)
        store = SupabaseStore(db)
        profile = _lookup_profile_by_identifier(db, identifier_key=identifier_key, identifier_value=identifier_value)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        if profile.user_id != target_user_id:
            raise HTTPException(status_code=403, detail="Profile ownership mismatch.")
        updated = store.apply_profile_action(
            action="activate",
            identifier_key=identifier_key,
            identifier_value=identifier_value,
            platform_code=payload.platform_code,
            actor_phone=str(claims.get("phone") or ""),
            note=payload.note,
            payload=payload.model_dump(by_alias=True, exclude_none=True),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        return {"success": True, "data": {"profile": _serialize_profile(updated, now=utcnow())}}

    @app.post("/api/v1/esim-access/webhooks/events")
    @app.post("/api/v1/esim-access/webhook/events")
    @app.post("/api/v1/esim-access/webhooks/events/{path_secret}")
    @app.post("/api/v1/esim-access/webhook/events/{path_secret}")
    def receive_webhook(
        event: WebhookEvent,
        db: Session = Depends(get_db),
        x_esim_access_webhook_secret: str | None = Header(
            default=None,
            alias="X-ESIM-ACCESS-WEBHOOK-SECRET",
        ),
        x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
        query_secret: str | None = Query(default=None, alias="secret"),
        path_secret: str | None = None,
    ) -> dict[str, Any]:
        # Plain `def`: all work below is synchronous SQLAlchemy/serialization, so
        # FastAPI runs it in a worker thread instead of blocking the event loop.
        _require_valid_esim_webhook_secret(
            header_secret=x_esim_access_webhook_secret,
            alternate_header_secret=x_webhook_secret,
            query_secret=query_secret,
            path_secret=path_secret,
        )
        lifecycle_event = SupabaseStore(db).record_webhook(event.model_dump(by_alias=True, exclude_none=True))
        return {"status": "accepted", "eventId": lifecycle_event.id}
