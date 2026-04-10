from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from math import ceil
import re
import uuid
from datetime import datetime, timezone
from time import time
from typing import Any, Callable, Generic, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_token_claims, require_active_subject
from supabase_store import AdminUser, AppUser, ESimProfile, PaymentAttempt, SupabaseStore, utcnow
from users import UserPayload


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


def _normalize_status(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "inactive"
    if raw == "canceled":
        return "cancelled"
    return raw


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


def _augment_package_list_response(provider_payload: dict[str, Any]) -> dict[str, Any]:
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
        included = _extract_included_countries(item)
        if included:
            item["includedCountries"] = included
        enhanced.append(item)
    obj = dict(obj)
    obj["packageList"] = enhanced
    payload["obj"] = obj
    return payload


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


def _serialize_profile(row: ESimProfile, *, now: datetime) -> dict[str, Any]:
    status_value = _normalize_status(row.app_status or row.provider_status)
    days_left: int | None = None
    if row.expires_at is not None:
        expires_at = row.expires_at if row.expires_at.tzinfo is not None else row.expires_at.replace(tzinfo=timezone.utc)
        now_at = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        delta_seconds = (expires_at - now_at).total_seconds()
        days_left = max(int(ceil(delta_seconds / 86400)), 0)
    country_code = row.order_item.country_code if row.order_item is not None else None
    country_name = row.order_item.country_name if row.order_item is not None else None
    return {
        "id": row.id,
        "userId": row.user_id,
        "iccid": row.iccid,
        "countryCode": country_code,
        "countryName": country_name,
        "status": status_value,
        "installed": bool(row.installed),
        "installedAt": _to_utc_z(row.installed_at),
        "activatedAt": _to_utc_z(row.activated_at),
        "expiresAt": _to_utc_z(row.expires_at),
        "totalDataMb": row.total_data_mb,
        "usedDataMb": row.used_data_mb,
        "remainingDataMb": row.remaining_data_mb,
        "daysLeft": days_left,
        "activationCode": row.activation_code,
        "installUrl": row.install_url,
        "esimTranNo": row.esim_tran_no,
        "customFields": row.custom_fields or {},
    }


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
    raise HTTPException(status_code=422, detail="Either iccid or esimTranNo is required.")


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
    trace_id = str(uuid.uuid4())
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "errorCode": "INTERNAL_ERROR",
            "message": "Top-up request failed unexpectedly.",
            "providerMessage": str(exc),
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
            wait_for = self.next_allowed - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = loop.time()
            self.next_allowed = max(self.next_allowed, now) + interval


class ESimAccessAPI:
    def __init__(
        self,
        *,
        access_code: str,
        secret_key: str,
        base_url: str = "https://api.esimaccess.com",
        timeout: float = 30.0,
        rate_limit_per_second: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.access_code = access_code
        self.secret_key = secret_key
        self.rate_limiter = AsyncRateLimiter(rate_limit_per_second)
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_packages(
        self,
        request: PackageQueryRequest,
    ) -> ESimAccessResponse[PackageListResult]:
        return await self._post("/api/v1/open/package/list", request, ESimAccessResponse[PackageListResult])

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
        return await self._post("/api/v1/open/location/list", request or EmptyRequest(), ESimAccessResponse[LocationListResult])

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


def register_esim_access_routes(
    app: FastAPI,
    get_db: Callable[..., Any],
    get_provider: Callable[..., ESimAccessAPI],
) -> None:
    @app.post("/api/v1/esim-access/packages/query")
    async def query_packages(
        payload: PackageQueryRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> dict[str, Any]:
        provider_response = await provider.get_packages(payload)
        raw_payload = provider_response.model_dump(by_alias=True, exclude_none=True)
        return _augment_package_list_response(raw_payload)

    @app.post("/api/v1/esim-access/orders")
    async def create_order(
        payload: OrderProfilesRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[OrderResult]:
        return await provider.order_profiles(payload)

    @app.post("/api/v1/esim-access/orders/managed")
    async def create_managed_order(
        payload: ManagedOrderPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        auth_user = require_active_subject(db, claims=claims, subject_type="user")
        assert isinstance(auth_user, AppUser)
        provider_response = await provider.order_profiles(payload.provider_request)
        store = SupabaseStore(db)
        resolved_payment_method, resolved_payment_provider = _resolve_payment_method_provider(payload)
        provider_request_payload = payload.provider_request.model_dump(by_alias=True, exclude_none=True)
        provider_response_payload = provider_response.model_dump(by_alias=True, exclude_none=True)
        try:
            customer_order, order_item = store.save_managed_order(
                user_data={
                    "phone": auth_user.phone,
                    "name": auth_user.name,
                    "email": auth_user.email,
                    "status": auth_user.status,
                    "is_loyalty": auth_user.is_loyalty,
                    "notes": auth_user.notes,
                },
                platform_code=payload.platform_code,
                platform_name=payload.platform_name,
                order_request=provider_request_payload,
                provider_response=provider_response_payload,
                currency_code=payload.currency_code,
                provider_currency_code=payload.provider_currency_code,
                exchange_rate=payload.exchange_rate,
                sale_price_minor=payload.sale_price_minor,
                provider_price_minor=payload.provider_price_minor,
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
        return {
            "provider": provider_response_payload,
            "database": {
                "customerOrderId": customer_order.id,
                "orderNumber": customer_order.order_number,
                "orderItemId": order_item.id,
                "providerOrderNo": order_item.provider_order_no,
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
            },
        }

    @app.post("/api/v1/esim-access/profiles/query")
    async def query_profiles(
        payload: ProfileQueryRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> dict[str, Any]:
        provider_response = await provider.query_profiles(payload)
        raw_payload = provider_response.model_dump(by_alias=True, exclude_none=True)
        return _augment_profile_usage_units(raw_payload)

    @app.post("/api/v1/esim-access/profiles/sync")
    async def sync_profiles(
        payload: ManagedProfileSyncPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        provider_response = await provider.query_profiles(payload.provider_request)
        store = SupabaseStore(db)
        profiles = store.sync_profiles(
            provider_response.model_dump(by_alias=True, exclude_none=True),
            platform_code=payload.platform_code,
            platform_name=payload.platform_name,
            actor_phone=payload.actor_phone,
        )
        return {
            "provider": provider_response.model_dump(by_alias=True, exclude_none=True),
            "database": {"profilesSynced": len(profiles)},
        }

    @app.post("/api/v1/esim-access/profiles/cancel")
    async def cancel_profile(
        payload: EsimTranNoRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.cancel_profile(payload)

    @app.post("/api/v1/esim-access/profiles/cancel/managed")
    async def cancel_profile_managed(
        payload: ManagedEsimTranActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        provider_response = await provider.cancel_profile(payload.provider_request)
        profile = SupabaseStore(db).apply_profile_action(
            action="cancel",
            identifier_key="esim_tran_no",
            identifier_value=payload.provider_request.esim_tran_no,
            platform_code=payload.context.platform_code,
            actor_phone=payload.context.actor_phone,
            note=payload.context.note,
            payload=provider_response.model_dump(by_alias=True, exclude_none=True),
        )
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/profiles/suspend")
    async def suspend_profile(
        payload: ICCIDRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.suspend_profile(payload)

    @app.post("/api/v1/esim-access/profiles/suspend/managed")
    async def suspend_profile_managed(
        payload: ManagedIccidActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        provider_response = await provider.suspend_profile(payload.provider_request)
        profile = SupabaseStore(db).apply_profile_action(
            action="suspend",
            identifier_key="iccid",
            identifier_value=payload.provider_request.iccid,
            platform_code=payload.context.platform_code,
            actor_phone=payload.context.actor_phone,
            note=payload.context.note,
            payload=provider_response.model_dump(by_alias=True, exclude_none=True),
        )
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/profiles/unsuspend")
    async def unsuspend_profile(
        payload: ICCIDRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.unsuspend_profile(payload)

    @app.post("/api/v1/esim-access/profiles/unsuspend/managed")
    async def unsuspend_profile_managed(
        payload: ManagedIccidActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        provider_response = await provider.unsuspend_profile(payload.provider_request)
        profile = SupabaseStore(db).apply_profile_action(
            action="unsuspend",
            identifier_key="iccid",
            identifier_value=payload.provider_request.iccid,
            platform_code=payload.context.platform_code,
            actor_phone=payload.context.actor_phone,
            note=payload.context.note,
            payload=provider_response.model_dump(by_alias=True, exclude_none=True),
        )
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/profiles/revoke")
    async def revoke_profile(
        payload: ICCIDRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.revoke_profile(payload)

    @app.post("/api/v1/esim-access/profiles/revoke/managed")
    async def revoke_profile_managed(
        payload: ManagedIccidActionPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        provider_response = await provider.revoke_profile(payload.provider_request)
        profile = SupabaseStore(db).apply_profile_action(
            action="revoke",
            identifier_key="iccid",
            identifier_value=payload.provider_request.iccid,
            platform_code=payload.context.platform_code,
            actor_phone=payload.context.actor_phone,
            note=payload.context.note,
            payload=provider_response.model_dump(by_alias=True, exclude_none=True),
        )
        return {"provider": provider_response.model_dump(by_alias=True, exclude_none=True), "database": {"profileId": profile.id if profile else None}}

    @app.post("/api/v1/esim-access/balance/query")
    async def query_balance(
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[BalanceResult]:
        return await provider.balance_query()

    @app.post("/api/v1/esim-access/topups", response_model=None)
    @app.post("/api/v1/esim-access/topup", response_model=None)
    async def top_up(
        payload: TopUpRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> Any:
        try:
            return await provider.top_up(payload)
        except Exception as exc:
            return build_topup_error_response(exc)

    @app.post("/api/v1/esim-access/topups/managed", response_model=None)
    @app.post("/api/v1/esim-access/topup/managed", response_model=None)
    async def top_up_managed(
        payload: ManagedTopUpPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> Any:
        try:
            provider_response = await provider.top_up(payload.provider_request)
        except Exception as exc:
            return build_topup_error_response(exc)
        profiles_synced = 0
        usage_records_synced = 0
        if payload.sync_after_topup:
            store = SupabaseStore(db)
            if payload.provider_request.iccid:
                query_request = ProfileQueryRequest(iccid=payload.provider_request.iccid)
                profile_response = await provider.query_profiles(query_request)
                profiles = store.sync_profiles(
                    profile_response.model_dump(by_alias=True, exclude_none=True),
                    platform_code=payload.platform_code,
                    platform_name=payload.platform_name,
                    actor_phone=payload.actor_phone,
                )
                profiles_synced = len(profiles)
            if payload.provider_request.esim_tran_no:
                usage_request = UsageCheckRequest(esim_tran_no_list=[payload.provider_request.esim_tran_no])
                usage_response = await provider.usage_check(usage_request)
                usage_profiles = store.sync_usage_records(
                    usage_response.model_dump(by_alias=True, exclude_none=True),
                    actor_phone=payload.actor_phone,
                )
                usage_records_synced = len(usage_profiles)
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
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.set_webhook(payload)

    @app.post("/api/v1/esim-access/sms/send")
    async def send_sms(
        payload: SendSmsRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[EmptyResult]:
        return await provider.send_sms(payload)

    @app.post("/api/v1/esim-access/usage/query")
    async def query_usage(
        payload: UsageCheckRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[UsageResult]:
        return await provider.usage_check(payload)

    @app.post("/api/v1/esim-access/usage/sync")
    async def sync_usage(
        payload: ManagedUsageSyncPayload,
        provider: ESimAccessAPI = Depends(get_provider),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        provider_response = await provider.usage_check(payload.provider_request)
        profiles = SupabaseStore(db).sync_usage_records(
            provider_response.model_dump(by_alias=True, exclude_none=True),
            actor_phone=payload.actor_phone,
        )
        return {
            "provider": provider_response.model_dump(by_alias=True, exclude_none=True),
            "database": {"profilesSynced": len(profiles)},
        }

    @app.post("/api/v1/esim-access/locations/query")
    async def query_locations(
        payload: EmptyRequest | None = None,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[LocationListResult]:
        return await provider.locations(payload)

    @app.get("/api/v1/esim-access/exchange-rates/current")
    async def get_current_exchange_rate_settings(
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        _ = require_active_subject(db, claims=claims)
        store = SupabaseStore(db)
        exchange = store.get_current_exchange_rate_settings()
        if exchange is None:
            return {
                "success": True,
                "data": {
                    "enableIQD": False,
                    "exchangeRate": "1320",
                    "markupPercent": "0",
                    "source": "tulip-admin",
                    "updatedAt": _to_utc_z(utcnow()),
                },
            }

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
            "success": True,
            "data": {
                "enableIQD": enable_iqd,
                "exchangeRate": _format_number_as_string(exchange.rate, "1320"),
                "markupPercent": markup_percent,
                "source": source,
                "updatedAt": _to_utc_z(exchange.updated_at),
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
    async def list_my_profiles(
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        status: str | None = Query(default=None),
        installed: bool | None = Query(default=None),
        user_id: str | None = Query(default=None, alias="userId"),
    ) -> dict[str, Any]:
        actor = require_active_subject(db, claims=claims)
        target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=user_id)
        store = SupabaseStore(db)
        rows, total = store.list_profiles_for_user(
            user_id=target_user_id,
            limit=limit,
            offset=offset,
            status=status,
            installed=installed,
        )
        now = utcnow()
        return {
            "success": True,
            "data": {
                "profiles": [_serialize_profile(row, now=now) for row in rows],
                "limit": limit,
                "offset": offset,
                "total": total,
            },
        }

    @app.post(
        "/api/v1/esim-access/profiles/install/my",
        summary="Mark caller-owned profile as installed",
        responses={
            401: {"description": "Missing or invalid bearer token."},
            403: {"description": "Ownership mismatch or forbidden target user scope."},
        },
    )
    async def install_my_profile(
        payload: MyProfileActionPayload,
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        actor = require_active_subject(db, claims=claims)
        target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=payload.user_id)
        identifier_key, identifier_value = _resolve_profile_identifier(payload)
        store = SupabaseStore(db)
        profile = (
            db.scalar(select(ESimProfile).where(ESimProfile.iccid == identifier_value))
            if identifier_key == "iccid"
            else db.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == identifier_value))
        )
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
    async def activate_my_profile(
        payload: MyProfileActionPayload,
        db: Session = Depends(get_db),
        claims: dict[str, Any] = Depends(get_token_claims),
    ) -> dict[str, Any]:
        actor = require_active_subject(db, claims=claims)
        target_user_id = _resolve_target_user_id(actor=actor, claims=claims, requested_user_id=payload.user_id)
        identifier_key, identifier_value = _resolve_profile_identifier(payload)
        store = SupabaseStore(db)
        profile = (
            db.scalar(select(ESimProfile).where(ESimProfile.iccid == identifier_value))
            if identifier_key == "iccid"
            else db.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == identifier_value))
        )
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
    async def receive_webhook(event: WebhookEvent, db: Session = Depends(get_db)) -> dict[str, Any]:
        lifecycle_event = SupabaseStore(db).record_webhook(event.model_dump(by_alias=True, exclude_none=True))
        return {"status": "accepted", "eventId": lifecycle_event.id}
