from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime
from time import time
from typing import Any, Callable, Generic, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field
from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from supabase_store import SupabaseStore
from users import UserPayload


class ESimAccessError(Exception):
    pass


class ESimAccessHTTPError(ESimAccessError):
    pass


class ESimAccessAPIError(ESimAccessError):
    def __init__(
        self,
        *,
        error_code: str | None,
        error_message: str | None,
        status_code: int | None = None,
    ) -> None:
        self.error_code = error_code
        self.error_message = error_message
        self.status_code = status_code
        message = f"eSIM Access API error {error_code or 'unknown'}"
        if error_message:
            message = f"{message}: {error_message}"
        super().__init__(message)


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
        headers = build_auth_headers(
            access_code=self.access_code,
            secret_key=self.secret_key,
            request_body=body,
        )
        if body:
            headers["Content-Type"] = "application/json"
        await self.rate_limiter.acquire()
        try:
            response = await self.client.post(path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ESimAccessHTTPError(str(exc)) from exc
        try:
            parsed = response_model.model_validate(response.json())
        except Exception as exc:
            raise ESimAccessHTTPError(f"Invalid provider response: {response.text}") from exc
        error_code = getattr(parsed, "error_code", None)
        has_error = error_code not in (None, "", "0", 0)
        if response.status_code >= 400 or not parsed.success or has_error:
            raise ESimAccessAPIError(
                error_code=str(error_code) if error_code is not None else None,
                error_message=parsed.error_msg,
                status_code=response.status_code,
            )
        return parsed


class ActionContext(BaseModel):
    actor_phone: str | None = Field(default=None, alias="actorPhone")
    platform_code: str | None = Field(default=None, alias="platformCode")
    platform_name: str | None = Field(default=None, alias="platformName")
    note: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


class ManagedOrderPayload(BaseModel):
    provider_request: OrderProfilesRequest = Field(alias="providerRequest")
    user: UserPayload
    platform_code: str = Field(alias="platformCode")
    platform_name: str | None = Field(default=None, alias="platformName")
    purchase_channel: str | None = Field(default=None, alias="purchaseChannel")
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


class ManagedProfileSyncPayload(BaseModel):
    provider_request: ProfileQueryRequest = Field(alias="providerRequest")
    platform_code: str | None = Field(default=None, alias="platformCode")
    platform_name: str | None = Field(default=None, alias="platformName")
    actor_phone: str | None = Field(default=None, alias="actorPhone")


class ManagedUsageSyncPayload(BaseModel):
    provider_request: UsageCheckRequest = Field(alias="providerRequest")
    actor_phone: str | None = Field(default=None, alias="actorPhone")


class ManagedEsimTranActionPayload(BaseModel):
    provider_request: EsimTranNoRequest = Field(alias="providerRequest")
    context: ActionContext


class ManagedIccidActionPayload(BaseModel):
    provider_request: ICCIDRequest = Field(alias="providerRequest")
    context: ActionContext


def register_esim_access_routes(
    app: FastAPI,
    get_db: Callable[..., Any],
    get_provider: Callable[..., ESimAccessAPI],
) -> None:
    @app.post("/api/v1/esim-access/packages/query")
    async def query_packages(
        payload: PackageQueryRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[PackageListResult]:
        return await provider.get_packages(payload)

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
    ) -> dict[str, Any]:
        provider_response = await provider.order_profiles(payload.provider_request)
        store = SupabaseStore(db)
        customer_order, order_item = store.save_managed_order(
            user_data=payload.user.model_dump(by_alias=False),
            platform_code=payload.platform_code,
            platform_name=payload.platform_name,
            order_request=payload.provider_request.model_dump(by_alias=True, exclude_none=True),
            provider_response=provider_response.model_dump(by_alias=True, exclude_none=True),
            purchase_channel=payload.purchase_channel,
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
            custom_fields=payload.custom_fields,
        )
        return {
            "provider": provider_response.model_dump(by_alias=True, exclude_none=True),
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
            },
        }

    @app.post("/api/v1/esim-access/profiles/query")
    async def query_profiles(
        payload: ProfileQueryRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[ProfileListResult]:
        return await provider.query_profiles(payload)

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

    @app.post("/api/v1/esim-access/topups")
    async def top_up(
        payload: TopUpRequest,
        provider: ESimAccessAPI = Depends(get_provider),
    ) -> ESimAccessResponse[TopUpResult]:
        return await provider.top_up(payload)

    @app.post("/api/v1/esim-access/webhooks/configure")
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

    @app.post("/api/v1/esim-access/webhooks/events")
    async def receive_webhook(event: WebhookEvent, db: Session = Depends(get_db)) -> dict[str, Any]:
        lifecycle_event = SupabaseStore(db).record_webhook(event.model_dump(by_alias=True, exclude_none=True))
        return {"status": "accepted", "eventId": lifecycle_event.id}
