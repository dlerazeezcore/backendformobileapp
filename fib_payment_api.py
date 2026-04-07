from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from time import time
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_token_claims, require_active_subject
from supabase_store import AdminUser, AppUser, PaymentAttempt, SupabaseStore, parse_provider_datetime


class FIBPaymentError(Exception):
    pass


class FIBPaymentHTTPError(FIBPaymentError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(message)


class FIBPaymentAPIError(FIBPaymentError):
    def __init__(
        self,
        *,
        status_code: int,
        error_code: str | None = None,
        error_message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        self.payload = payload
        message = f"FIB API error {status_code}"
        if error_message:
            message = f"{message}: {error_message}"
        super().__init__(message)


class Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class MonetaryValue(Model):
    amount: str | int | float
    currency: str = "IQD"


class TokenResponse(Model):
    access_token: str = Field(alias="access_token")
    expires_in: int = Field(alias="expires_in")
    refresh_expires_in: int | None = Field(default=None, alias="refresh_expires_in")
    token_type: str = Field(alias="token_type")
    scope: str | None = None


class CreatePaymentRequest(Model):
    monetary_value: MonetaryValue = Field(alias="monetaryValue")
    status_callback_url: str | None = Field(default=None, alias="statusCallbackUrl")
    description: str | None = Field(default=None, max_length=50)
    redirect_uri: str | None = Field(default=None, alias="redirectUri")
    expires_in: str | None = Field(default=None, alias="expiresIn")
    category: str | None = None
    refundable_for: str | None = Field(default=None, alias="refundableFor")


class CreatePaymentResponse(Model):
    payment_id: str = Field(alias="paymentId")
    readable_code: str | None = Field(default=None, alias="readableCode")
    qr_code: str | None = Field(default=None, alias="qrCode")
    valid_until: str | None = Field(default=None, alias="validUntil")
    personal_app_link: str | None = Field(default=None, alias="personalAppLink")
    business_app_link: str | None = Field(default=None, alias="businessAppLink")
    corporate_app_link: str | None = Field(default=None, alias="corporateAppLink")


class PaymentAmount(Model):
    amount: int | float | str | None = None
    currency: str | None = None


class PaymentPayer(Model):
    name: str | None = None
    iban: str | None = None


class PaymentStatusResponse(Model):
    payment_id: str = Field(alias="paymentId")
    status: str
    valid_until: str | None = Field(default=None, alias="validUntil")
    paid_at: str | None = Field(default=None, alias="paidAt")
    amount: PaymentAmount | None = None
    declining_reason: str | None = Field(default=None, alias="decliningReason")
    declined_at: str | None = Field(default=None, alias="declinedAt")
    paid_by: PaymentPayer | None = Field(default=None, alias="paidBy")


class FIBCheckoutRequest(Model):
    amount: str | int | float
    currency: str = "IQD"
    description: str | None = None
    return_url: str | None = Field(default=None, alias="returnUrl")
    success_url: str | None = Field(default=None, alias="successUrl")
    cancel_url: str | None = Field(default=None, alias="cancelUrl")
    metadata: dict[str, Any] = Field(default_factory=dict)


class FIBConfirmRequest(Model):
    payment_id: str | None = Field(default=None, alias="paymentId")
    transaction_id: str | None = Field(default=None, alias="transactionId")


class PaymentWebhookEvent(Model):
    id: str | None = None
    payment_id: str | None = Field(default=None, alias="paymentId")
    transaction_id: str | None = Field(default=None, alias="transactionId")
    status: str | dict[str, Any] | None = None


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


class FIBPaymentAPI:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str = "https://fib.stage.fib.iq",
        timeout: float = 30.0,
        rate_limit_per_second: float = 8.0,
        default_status_callback_url: str | None = None,
        default_redirect_uri: str | None = None,
        webhook_secret: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.default_status_callback_url = default_status_callback_url
        self.default_redirect_uri = default_redirect_uri
        self.webhook_secret = webhook_secret
        self.rate_limiter = AsyncRateLimiter(rate_limit_per_second)
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._access_token_expires_at_epoch: float = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def get_access_token(self, *, force_refresh: bool = False) -> TokenResponse:
        now = time()
        if (
            not force_refresh
            and self._access_token is not None
            and now < self._access_token_expires_at_epoch - 10
        ):
            return TokenResponse(
                access_token=self._access_token,
                expires_in=max(1, int(self._access_token_expires_at_epoch - now)),
                token_type="Bearer",
            )
        async with self._token_lock:
            now = time()
            if (
                not force_refresh
                and self._access_token is not None
                and now < self._access_token_expires_at_epoch - 10
            ):
                return TokenResponse(
                    access_token=self._access_token,
                    expires_in=max(1, int(self._access_token_expires_at_epoch - now)),
                    token_type="Bearer",
                )
            payload = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
            await self.rate_limiter.acquire()
            try:
                response = await self.client.post(
                    "/auth/realms/fib-online-shop/protocol/openid-connect/token",
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise FIBPaymentHTTPError(str(exc)) from exc
            parsed_payload = _safe_json(response)
            if response.status_code >= 400:
                raise FIBPaymentAPIError(
                    status_code=response.status_code,
                    error_code=_extract_error_code(parsed_payload),
                    error_message=_extract_error_message(parsed_payload, response.text),
                    payload=parsed_payload,
                )
            try:
                token_response = TokenResponse.model_validate(parsed_payload)
            except Exception as exc:
                raise FIBPaymentHTTPError(
                    f"Invalid FIB auth response: {response.text}",
                    status_code=response.status_code,
                    payload=parsed_payload,
                ) from exc
            self._access_token = token_response.access_token
            self._access_token_expires_at_epoch = time() + max(1, token_response.expires_in)
            return token_response

    async def create_payment(self, payload: CreatePaymentRequest) -> CreatePaymentResponse:
        request_payload = payload.model_dump(by_alias=True, exclude_none=True)
        if request_payload.get("statusCallbackUrl") is None and self.default_status_callback_url:
            request_payload["statusCallbackUrl"] = self.default_status_callback_url
        if request_payload.get("redirectUri") is None and self.default_redirect_uri:
            request_payload["redirectUri"] = self.default_redirect_uri
        return await self._request_json(
            method="POST",
            path="/protected/v1/payments",
            expected_statuses={201},
            response_model=CreatePaymentResponse,
            json_payload=request_payload,
        )

    async def get_payment_status(self, payment_id: str) -> PaymentStatusResponse:
        return await self._request_json(
            method="GET",
            path=f"/protected/v1/payments/{payment_id}/status",
            expected_statuses={200},
            response_model=PaymentStatusResponse,
        )

    async def cancel_payment(self, payment_id: str) -> None:
        await self._request_json(
            method="POST",
            path=f"/protected/v1/payments/{payment_id}/cancel",
            expected_statuses={204},
            response_model=None,
        )

    async def refund_payment(self, payment_id: str) -> None:
        await self._request_json(
            method="POST",
            path=f"/protected/v1/payments/{payment_id}/refund",
            expected_statuses={202, 204},
            response_model=None,
        )

    async def _request_json(
        self,
        *,
        method: str,
        path: str,
        expected_statuses: set[int],
        response_model: type[Model] | None,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        token_response = await self.get_access_token()
        headers = {"Authorization": f"Bearer {token_response.access_token}", "Accept": "application/json"}
        retry_on_unauthorized = True
        while True:
            await self.rate_limiter.acquire()
            try:
                response = await self.client.request(method, path, json=json_payload, headers=headers)
            except httpx.HTTPError as exc:
                raise FIBPaymentHTTPError(str(exc)) from exc
            if response.status_code == 401 and retry_on_unauthorized:
                retry_on_unauthorized = False
                refreshed_token = await self.get_access_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {refreshed_token.access_token}"
                continue
            parsed_payload = _safe_json(response)
            if response.status_code not in expected_statuses:
                raise FIBPaymentAPIError(
                    status_code=response.status_code,
                    error_code=_extract_error_code(parsed_payload),
                    error_message=_extract_error_message(parsed_payload, response.text),
                    payload=parsed_payload,
                )
            if response_model is None:
                return None
            try:
                return response_model.model_validate(parsed_payload)
            except Exception as exc:
                raise FIBPaymentHTTPError(
                    f"Invalid FIB response: {response.text}",
                    status_code=response.status_code,
                    payload=parsed_payload,
                ) from exc


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        raw = response.json()
        if isinstance(raw, dict):
            return raw
        return {"raw": raw}
    except Exception:
        return {"raw": response.text}


def _extract_error_code(payload: dict[str, Any]) -> str | None:
    for key in ("errorCode", "error_code", "code", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_error_message(payload: dict[str, Any], fallback: str) -> str:
    for key in ("errorMessage", "error_description", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw_value = payload.get("raw")
    if isinstance(raw_value, str) and raw_value.strip():
        return raw_value.strip()
    if fallback.strip():
        return fallback.strip()
    return "FIB request failed"


def _normalize_payment_status(raw_status: str | None) -> str:
    value = (raw_status or "").strip().upper()
    if value in {"PAID", "SUCCESS", "COMPLETED", "SETTLED", "REFUNDED"}:
        return "paid"
    if value in {"FAILED", "DECLINED", "REJECTED", "ERROR"}:
        return "failed"
    if value in {"CANCELLED", "CANCELED", "VOIDED"}:
        return "canceled"
    if value in {"EXPIRED", "TIMEOUT", "TIMED_OUT"}:
        return "expired"
    return "pending"


def _coerce_amount_minor(value: str | int | float) -> int:
    if isinstance(value, bool):
        raise ValueError("amount must be numeric")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, float):
        amount = int(round(value))
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("amount must be provided")
        try:
            amount = int(cleaned)
        except ValueError:
            amount = int(round(float(cleaned)))
    else:
        raise ValueError("amount must be numeric")
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    return amount


def _clean_none(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            cleaned = _clean_none(nested)
            if cleaned is None:
                continue
            result[key] = cleaned
        return result
    if isinstance(value, list):
        return [_clean_none(item) for item in value]
    return value


def _extract_transaction_id(metadata: dict[str, Any]) -> str:
    for key in ("transactionId", "transaction_id", "idempotencyKey", "idempotency_key"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return f"fib-{uuid.uuid4().hex}"


def _extract_provider_payment_id(payload: dict[str, Any]) -> str | None:
    candidates: list[Any] = [
        payload.get("paymentId"),
        payload.get("payment_id"),
        payload.get("id"),
    ]
    nested = payload.get("status")
    if isinstance(nested, dict):
        candidates.extend([nested.get("paymentId"), nested.get("payment_id"), nested.get("id")])
    for raw in candidates:
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _extract_provider_status(payload: dict[str, Any]) -> str | None:
    raw_status = payload.get("status")
    if isinstance(raw_status, str) and raw_status.strip():
        return raw_status.strip()
    if isinstance(raw_status, dict):
        nested = raw_status.get("status")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    for key in ("paymentStatus", "state"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_provider_event_id(payload: dict[str, Any]) -> str | None:
    for key in ("eventId", "event_id", "notificationId", "webhookId", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_webhook_transaction_id(payload: dict[str, Any]) -> str | None:
    for key in ("transactionId", "transaction_id", "merchantTransactionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("transactionId", "transaction_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_paid_at(payload: dict[str, Any]) -> str | None:
    raw_status = payload.get("status")
    if isinstance(raw_status, dict):
        paid_at = raw_status.get("paidAt") or raw_status.get("paid_at")
        if isinstance(paid_at, str) and paid_at.strip():
            return paid_at.strip()
    paid_at = payload.get("paidAt") or payload.get("paid_at")
    if isinstance(paid_at, str) and paid_at.strip():
        return paid_at.strip()
    return None


def _extract_webhook_signature(headers: dict[str, str]) -> str | None:
    for key in ("x-fib-signature", "x-signature", "x-webhook-signature"):
        value = headers.get(key)
        if value:
            return value.strip()
    return None


def _payment_link_from_create(result: CreatePaymentResponse) -> str | None:
    return result.personal_app_link or result.business_app_link or result.corporate_app_link


PERSISTED_PAYMENT_STATUSES = {"paid", "refunded"}


def _checkout_event_id_by_transaction(transaction_id: str) -> str:
    return f"checkout_tx:{transaction_id}"


def _checkout_event_id_by_payment(payment_id: str) -> str:
    return f"checkout_payment:{payment_id}"


def _as_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return default
        try:
            return int(cleaned)
        except ValueError:
            try:
                return int(float(cleaned))
            except ValueError:
                return default
    return default


def _to_checkout_response_from_context(context: dict[str, Any]) -> dict[str, Any]:
    provider_payment_id = _metadata_string(context, ("providerPaymentId", "paymentId"))
    transaction_id = _metadata_string(context, ("transactionId", "transaction_id")) or f"fib-{uuid.uuid4().hex}"
    raw_provider_status = _metadata_string(context, ("providerStatus",)) or "UNPAID"
    normalized_status = _metadata_string(context, ("normalizedStatus",)) or _normalize_payment_status(raw_provider_status)
    response = {
        "paymentAttemptId": _metadata_string(context, ("paymentAttemptId", "payment_attempt_id")),
        "paymentId": provider_payment_id or transaction_id,
        "providerPaymentId": provider_payment_id,
        "transactionId": transaction_id,
        "paymentMethod": "fib",
        "provider": "fib",
        "userId": _metadata_string(context, ("userId", "user_id")),
        "adminUserId": _metadata_string(context, ("adminUserId", "admin_user_id")),
        "externalUserRef": _metadata_string(context, ("externalUserRef", "external_user_ref")),
        "status": normalized_status,
        "amountMinor": _as_int(context.get("amountMinor"), default=0),
        "currencyCode": (_metadata_string(context, ("currencyCode", "currency_code")) or "IQD").upper(),
        "customerOrderId": _metadata_int(context, ("customerOrderId", "customer_order_id")),
        "orderItemId": _metadata_int(context, ("orderItemId", "order_item_id")),
        "paymentLink": _metadata_string(context, ("paymentLink",)),
        "qrCodeUrl": _metadata_string(context, ("qrCodeUrl",)),
        "expiresAt": _metadata_string(context, ("expiresAt",)),
        "providerInfo": {
            "name": "fib",
            "paymentId": provider_payment_id,
            "reference": _metadata_string(context, ("providerReference", "readableCode")),
            "status": raw_provider_status,
            "refs": {
                "providerPaymentId": provider_payment_id,
                "readableCode": _metadata_string(context, ("providerReference", "readableCode")),
            },
        },
    }
    return _clean_none(response)


def _resolve_checkout_context(
    *,
    store: SupabaseStore,
    transaction_id: str | None = None,
    provider_payment_id: str | None = None,
) -> dict[str, Any] | None:
    event = None
    if transaction_id:
        event = store.get_payment_provider_event(provider="fib", provider_event_id=_checkout_event_id_by_transaction(transaction_id))
    if event is None and provider_payment_id:
        event = store.get_payment_provider_event(provider="fib", provider_event_id=_checkout_event_id_by_payment(provider_payment_id))
    if event is None:
        return None
    payload = event.raw_payload if isinstance(event.raw_payload, dict) else None
    return payload


def _build_checkout_context_payload(
    *,
    transaction_id: str,
    metadata: dict[str, Any],
    provider_payload_dict: dict[str, Any],
    provider_response: CreatePaymentResponse,
    provider_response_dict: dict[str, Any],
    amount_minor: int,
    currency_code: str,
    user_id: str | None,
    admin_user_id: str | None,
    external_user_ref: str | None,
) -> dict[str, Any]:
    return {
        "paymentAttemptId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"fib:{transaction_id}")),
        "transactionId": transaction_id,
        "providerPaymentId": provider_response.payment_id,
        "providerReference": provider_response.readable_code,
        "paymentMethod": "fib",
        "provider": "fib",
        "providerStatus": "UNPAID",
        "normalizedStatus": "pending",
        "amountMinor": amount_minor,
        "currencyCode": currency_code,
        "paymentLink": _payment_link_from_create(provider_response),
        "qrCodeUrl": provider_response.qr_code,
        "expiresAt": provider_response.valid_until,
        "serviceType": _metadata_string(metadata, ("serviceType", "service_type")) or "esim",
        "customerOrderId": _metadata_int(metadata, ("customerOrderId", "customer_order_id")),
        "orderItemId": _metadata_int(metadata, ("orderItemId", "order_item_id")),
        "idempotencyKey": _metadata_string(metadata, ("idempotencyKey", "idempotency_key")),
        "userId": user_id,
        "adminUserId": admin_user_id,
        "externalUserRef": external_user_ref,
        "metadata": metadata,
        "providerRequest": provider_payload_dict,
        "providerCreatePayload": provider_response_dict,
    }


def _actor_matches_payment_context(
    *,
    owner_user_id: str | None,
    owner_admin_user_id: str | None,
    row: PaymentAttempt | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    if row is not None:
        subject_user_id = row.user_id
        subject_admin_id = row.admin_user_id
    else:
        context_payload = context or {}
        subject_user_id = _metadata_string(context_payload, ("userId", "user_id"))
        subject_admin_id = _metadata_string(context_payload, ("adminUserId", "admin_user_id"))
    if owner_user_id:
        return owner_user_id == subject_user_id
    if owner_admin_user_id:
        return owner_admin_user_id == subject_admin_id
    return False


def _upsert_successful_attempt_from_provider_status(
    *,
    db: Session,
    store: SupabaseStore,
    provider_payment_id: str,
    provider_status: PaymentStatusResponse | dict[str, Any],
    transaction_id_hint: str | None = None,
) -> PaymentAttempt | None:
    row = store.get_payment_attempt_by_provider_payment_id(
        provider="fib",
        provider_payment_id=provider_payment_id,
        for_update=True,
    )
    payload = (
        provider_status.model_dump(by_alias=True, exclude_none=True)
        if isinstance(provider_status, PaymentStatusResponse)
        else provider_status
    )
    raw_status = provider_status.status if isinstance(provider_status, PaymentStatusResponse) else _extract_provider_status(payload)
    normalized_status = _normalize_payment_status(raw_status)
    if row is not None:
        _apply_verified_status(store=store, row=row, provider_payment_id=provider_payment_id, provider_status=provider_status)
        return row

    if normalized_status not in PERSISTED_PAYMENT_STATUSES:
        return None

    context_payload = _resolve_checkout_context(
        store=store,
        transaction_id=transaction_id_hint,
        provider_payment_id=provider_payment_id,
    ) or {}
    user_id = _metadata_string(context_payload, ("userId", "user_id"))
    admin_user_id = _metadata_string(context_payload, ("adminUserId", "admin_user_id"))
    if not user_id and not admin_user_id:
        return None

    transaction_id = (
        _metadata_string(context_payload, ("transactionId", "transaction_id"))
        or transaction_id_hint
        or f"fib-{provider_payment_id}"
    )
    amount_minor = _as_int(context_payload.get("amountMinor"), default=0)
    currency_code = (_metadata_string(context_payload, ("currencyCode", "currency_code")) or "IQD").upper()
    if isinstance(provider_status, PaymentStatusResponse) and provider_status.amount:
        amount_minor = _as_int(provider_status.amount.amount, default=amount_minor)
        currency_code = (provider_status.amount.currency or currency_code).upper()
    elif isinstance(payload, dict):
        amount_payload = payload.get("amount")
        if isinstance(amount_payload, dict):
            amount_minor = _as_int(amount_payload.get("amount"), default=amount_minor)
            currency_code = (str(amount_payload.get("currency") or currency_code)).upper()

    row = store.create_payment_attempt(
        transaction_id=transaction_id,
        payment_method="fib",
        provider="fib",
        provider_payment_id=provider_payment_id,
        provider_reference=_extract_provider_event_id(payload) or _metadata_string(context_payload, ("providerReference",)),
        external_user_ref=_metadata_string(context_payload, ("externalUserRef", "external_user_ref")),
        status=normalized_status,
        amount_minor=amount_minor,
        currency_code=currency_code,
        customer_order_id=_metadata_int(context_payload, ("customerOrderId", "customer_order_id")),
        user_id=user_id,
        admin_user_id=admin_user_id,
        service_type=_metadata_string(context_payload, ("serviceType", "service_type")) or "esim",
        order_item_id=_metadata_int(context_payload, ("orderItemId", "order_item_id")),
        idempotency_key=_metadata_string(context_payload, ("idempotencyKey", "idempotency_key")),
        metadata=dict(context_payload.get("metadata") or {}),
        provider_request=dict(context_payload.get("providerRequest") or {}),
        provider_response={
            "providerStatus": raw_status,
            "providerStatusPayload": payload,
            "paymentLink": _metadata_string(context_payload, ("paymentLink",)),
            "qrCodeUrl": _metadata_string(context_payload, ("qrCodeUrl",)),
            "expiresAt": _metadata_string(context_payload, ("expiresAt",)),
            "providerRefs": {
                "providerPaymentId": provider_payment_id,
                "providerReference": _extract_provider_event_id(payload),
            },
        },
    )
    _apply_verified_status(
        store=store,
        row=row,
        provider_payment_id=provider_payment_id,
        provider_status=provider_status,
    )
    db.flush()
    return row


def _error_response(
    *,
    status_code: int,
    error_code: str,
    message: str,
    request_id: str | None = None,
    provider_message: str | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    trace_id = request_id or str(uuid.uuid4())
    payload: dict[str, Any] = {
        "success": False,
        "errorCode": error_code,
        "message": message,
        "requestId": trace_id,
        "traceId": trace_id,
    }
    if provider_message:
        payload["providerMessage"] = provider_message
    if details:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def _map_fib_exception(exc: Exception) -> JSONResponse:
    if isinstance(exc, FIBPaymentAPIError):
        status_code = exc.status_code if 400 <= exc.status_code < 600 else 502
        if status_code >= 500:
            error_code = exc.error_code or "FIB_UPSTREAM_ERROR"
            message = "Payment provider is temporarily unavailable."
        elif status_code == 404:
            error_code = exc.error_code or "FIB_PAYMENT_NOT_FOUND"
            message = "Payment was not found at provider."
        else:
            error_code = exc.error_code or "FIB_PROVIDER_REJECTED"
            message = "Payment request was rejected by provider."
        return _error_response(
            status_code=status_code,
            error_code=error_code,
            message=message,
            provider_message=exc.error_message,
            details={"providerPayload": exc.payload or {}},
        )
    if isinstance(exc, FIBPaymentHTTPError):
        return _error_response(
            status_code=502,
            error_code="FIB_UPSTREAM_UNAVAILABLE",
            message="Unable to reach payment provider.",
            provider_message=str(exc),
        )
    return _error_response(
        status_code=500,
        error_code="INTERNAL_ERROR",
        message="Payment request failed unexpectedly.",
        provider_message=str(exc),
    )


def _to_checkout_response(row: PaymentAttempt) -> dict[str, Any]:
    provider_response = row.provider_response or {}
    response = {
        "paymentAttemptId": row.id,
        "paymentId": row.provider_payment_id or row.transaction_id,
        "providerPaymentId": row.provider_payment_id,
        "transactionId": row.transaction_id,
        "paymentMethod": row.payment_method,
        "provider": row.provider,
        "userId": row.user_id,
        "adminUserId": row.admin_user_id,
        "externalUserRef": row.external_user_ref,
        "status": row.status,
        "amountMinor": row.amount_minor,
        "currencyCode": row.currency_code,
        "customerOrderId": row.customer_order_id,
        "orderItemId": row.order_item_id,
        "paymentLink": provider_response.get("paymentLink"),
        "qrCodeUrl": provider_response.get("qrCodeUrl"),
        "expiresAt": provider_response.get("expiresAt"),
        "paidAt": row.paid_at.isoformat() if row.paid_at else None,
        "failedAt": row.failed_at.isoformat() if row.failed_at else None,
        "canceledAt": row.canceled_at.isoformat() if row.canceled_at else None,
        "failureReason": row.failure_reason,
        "providerInfo": {
            "name": row.provider,
            "paymentId": row.provider_payment_id,
            "reference": row.provider_reference,
            "status": provider_response.get("providerStatus"),
            "refs": provider_response.get("providerRefs"),
        },
    }
    return _clean_none(response)


def _metadata_string(metadata: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _metadata_int(metadata: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                continue
            try:
                return int(cleaned)
            except ValueError:
                continue
    return None


def _extract_checkout_user_ref(metadata: dict[str, Any]) -> tuple[str | None, str | None]:
    ordered_keys = ("customerUserId", "customer_user_id", "userId", "user_id")
    for key in ordered_keys:
        raw_value = metadata.get(key)
        if raw_value is None:
            continue
        if isinstance(raw_value, str):
            cleaned = raw_value.strip()
            if cleaned:
                return key, cleaned
            continue
        return key, str(raw_value)
    return None, None


def _apply_verified_status(
    *,
    store: SupabaseStore,
    row: PaymentAttempt,
    provider_payment_id: str,
    provider_status: PaymentStatusResponse | dict[str, Any],
) -> None:
    if isinstance(provider_status, PaymentStatusResponse):
        raw_status = provider_status.status
        payload = provider_status.model_dump(by_alias=True, exclude_none=True)
        paid_at = parse_provider_datetime(provider_status.paid_at)
    else:
        raw_status = _extract_provider_status(provider_status)
        payload = provider_status
        paid_at = parse_provider_datetime(_extract_paid_at(provider_status))

    normalized_status = _normalize_payment_status(raw_status)
    store.update_payment_attempt(
        row,
        provider_payment_id=provider_payment_id,
        provider_reference=_extract_provider_event_id(payload),
        provider_response={
            "providerStatus": raw_status,
            "providerStatusPayload": payload,
            "providerRefs": {
                "providerPaymentId": provider_payment_id,
            },
        },
    )
    if normalized_status in PERSISTED_PAYMENT_STATUSES:
        store.apply_payment_status_transition(
            row,
            new_status=normalized_status,
            paid_at=paid_at,
        )


async def _create_checkout_attempt(
    *,
    payload: FIBCheckoutRequest,
    provider: FIBPaymentAPI,
    db: Session,
    owner_user_id: str | None,
    owner_admin_user_id: str | None,
    owner_external_user_ref: str | None,
) -> PaymentAttempt:
    store = SupabaseStore(db)
    metadata = dict(payload.metadata or {})
    transaction_id = _extract_transaction_id(metadata)

    existing_success = store.get_payment_attempt_by_transaction_id(transaction_id)
    if existing_success is not None:
        return existing_success
    existing_context = _resolve_checkout_context(store=store, transaction_id=transaction_id)
    if existing_context is not None:
        if not _actor_matches_payment_context(
            owner_user_id=owner_user_id,
            owner_admin_user_id=owner_admin_user_id,
            context=existing_context,
        ):
            raise ValueError("Transaction ID already exists for another account.")
        return PaymentAttempt(
            id=_metadata_string(existing_context, ("paymentAttemptId", "payment_attempt_id")) or str(uuid.uuid4()),
            transaction_id=transaction_id,
            payment_method="fib",
            provider="fib",
            status=_metadata_string(existing_context, ("normalizedStatus", "status")) or "pending",
            amount_minor=_as_int(existing_context.get("amountMinor"), default=0),
            currency_code=(_metadata_string(existing_context, ("currencyCode",)) or "IQD").upper(),
            provider_payment_id=_metadata_string(existing_context, ("providerPaymentId", "paymentId")),
            provider_reference=_metadata_string(existing_context, ("providerReference",)),
            external_user_ref=_metadata_string(existing_context, ("externalUserRef", "external_user_ref")),
            user_id=_metadata_string(existing_context, ("userId", "user_id")),
            admin_user_id=_metadata_string(existing_context, ("adminUserId", "admin_user_id")),
            service_type=_metadata_string(existing_context, ("serviceType", "service_type")) or "esim",
            customer_order_id=_metadata_int(existing_context, ("customerOrderId", "customer_order_id")),
            order_item_id=_metadata_int(existing_context, ("orderItemId", "order_item_id")),
            idempotency_key=_metadata_string(existing_context, ("idempotencyKey", "idempotency_key")),
            metadata_payload=dict(existing_context.get("metadata") or {}),
            provider_request=dict(existing_context.get("providerRequest") or {}),
            provider_response={
                "providerStatus": _metadata_string(existing_context, ("providerStatus",)) or "UNPAID",
                "providerCreatePayload": existing_context.get("providerCreatePayload"),
                "paymentLink": _metadata_string(existing_context, ("paymentLink",)),
                "qrCodeUrl": _metadata_string(existing_context, ("qrCodeUrl",)),
                "expiresAt": _metadata_string(existing_context, ("expiresAt",)),
                "providerRefs": {
                    "providerPaymentId": _metadata_string(existing_context, ("providerPaymentId", "paymentId")),
                    "readableCode": _metadata_string(existing_context, ("providerReference",)),
                },
            },
        )

    amount_minor = _coerce_amount_minor(payload.amount)
    currency_code = payload.currency.strip().upper() if payload.currency else "IQD"
    redirect_uri = payload.success_url or payload.return_url or payload.cancel_url

    provider_payload = CreatePaymentRequest(
        monetaryValue={"amount": str(amount_minor), "currency": currency_code},
        description=payload.description,
        redirectUri=redirect_uri,
        statusCallbackUrl=_metadata_string(metadata, ("statusCallbackUrl", "status_callback_url")),
    )

    provider_response = await provider.create_payment(provider_payload)
    provider_payload_dict = provider_payload.model_dump(by_alias=True, exclude_none=True)
    provider_response_dict = provider_response.model_dump(by_alias=True, exclude_none=True)
    user_ref_source, metadata_user_ref = _extract_checkout_user_ref(metadata)
    external_user_ref = metadata_user_ref or owner_external_user_ref
    linked_user_id = owner_user_id
    linked_admin_user_id = owner_admin_user_id
    if external_user_ref:
        metadata["externalUserRef"] = external_user_ref
    if user_ref_source:
        metadata["userRefSource"] = user_ref_source
    if linked_user_id:
        metadata["linkedUserId"] = linked_user_id
    if linked_admin_user_id:
        metadata["linkedAdminUserId"] = linked_admin_user_id

    checkout_context = _build_checkout_context_payload(
        transaction_id=transaction_id,
        metadata=metadata,
        provider_payload_dict=provider_payload_dict,
        provider_response=provider_response,
        provider_response_dict=provider_response_dict,
        amount_minor=amount_minor,
        currency_code=currency_code,
        user_id=linked_user_id,
        admin_user_id=linked_admin_user_id,
        external_user_ref=external_user_ref,
    )
    marker_tx = _checkout_event_id_by_transaction(transaction_id)
    marker_payment = _checkout_event_id_by_payment(provider_response.payment_id)
    if store.get_payment_provider_event(provider="fib", provider_event_id=marker_tx) is None:
        store.create_payment_provider_event(
            provider="fib",
            event_type="fib.checkout_created",
            provider_event_id=marker_tx,
            signature_valid=None,
            raw_payload=checkout_context,
            processed=True,
        )
    if store.get_payment_provider_event(provider="fib", provider_event_id=marker_payment) is None:
        store.create_payment_provider_event(
            provider="fib",
            event_type="fib.checkout_created",
            provider_event_id=marker_payment,
            signature_valid=None,
            raw_payload=checkout_context,
            processed=True,
        )
    db.commit()
    return PaymentAttempt(
        id=checkout_context["paymentAttemptId"],
        transaction_id=transaction_id,
        payment_method="fib",
        provider="fib",
        status="pending",
        amount_minor=amount_minor,
        currency_code=currency_code,
        provider_payment_id=provider_response.payment_id,
        provider_reference=provider_response.readable_code,
        external_user_ref=external_user_ref,
        user_id=linked_user_id,
        admin_user_id=linked_admin_user_id,
        service_type=_metadata_string(metadata, ("serviceType", "service_type")) or "esim",
        customer_order_id=_metadata_int(metadata, ("customerOrderId", "customer_order_id")),
        order_item_id=_metadata_int(metadata, ("orderItemId", "order_item_id")),
        idempotency_key=_metadata_string(metadata, ("idempotencyKey", "idempotency_key")),
        metadata_payload=metadata,
        provider_request=provider_payload_dict,
        provider_response={
            "providerStatus": "UNPAID",
            "providerCreatePayload": provider_response_dict,
            "paymentLink": _payment_link_from_create(provider_response),
            "qrCodeUrl": provider_response.qr_code,
            "expiresAt": provider_response.valid_until,
            "providerRefs": {
                "providerPaymentId": provider_response.payment_id,
                "readableCode": provider_response.readable_code,
            },
        },
    )


def register_fib_payment_routes(
    app: FastAPI,
    get_fib_provider: Callable[..., FIBPaymentAPI],
    get_db: Callable[..., Any],
) -> None:
    async def _require_payment_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> tuple[str | None, str | None, str]:
        row = require_active_subject(db, claims=claims)
        if isinstance(row, AppUser):
            return row.id, None, row.phone
        assert isinstance(row, AdminUser)
        return None, row.id, row.phone

    @app.post("/api/v1/payments/fib/checkout")
    @app.post("/api/v1/payments/fib/create")
    @app.post("/api/v1/payments/fib/intent")
    @app.post("/api/v1/payments/fib/initiate")
    async def checkout_payment(
        payload: FIBCheckoutRequest,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        db: Session = Depends(get_db),
        actor: tuple[str | None, str | None, str] = Depends(_require_payment_actor),
    ) -> Any:
        owner_user_id, owner_admin_user_id, owner_phone = actor
        try:
            row = await _create_checkout_attempt(
                payload=payload,
                provider=provider,
                db=db,
                owner_user_id=owner_user_id,
                owner_admin_user_id=owner_admin_user_id,
                owner_external_user_ref=owner_phone,
            )
            return _to_checkout_response(row)
        except ValueError as exc:
            return _error_response(
                status_code=422,
                error_code="INVALID_PAYMENT_REQUEST",
                message="Invalid checkout payload.",
                provider_message=str(exc),
            )
        except Exception as exc:
            return _map_fib_exception(exc)

    @app.get("/api/v1/payments/fib/{payment_id}")
    async def get_payment(
        payment_id: str,
        refresh: bool = False,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        db: Session = Depends(get_db),
        actor: tuple[str | None, str | None, str] = Depends(_require_payment_actor),
    ) -> Any:
        owner_user_id, owner_admin_user_id, _ = actor
        store = SupabaseStore(db)
        row = store.get_payment_attempt_by_any_reference(payment_id)
        if row is None and not refresh:
            checkout_context = _resolve_checkout_context(store=store, provider_payment_id=payment_id)
            if checkout_context is not None:
                if not _actor_matches_payment_context(
                    owner_user_id=owner_user_id,
                    owner_admin_user_id=owner_admin_user_id,
                    context=checkout_context,
                ):
                    return _error_response(
                        status_code=403,
                        error_code="PAYMENT_FORBIDDEN",
                        message="You do not have access to this payment.",
                    )
                return _to_checkout_response_from_context(checkout_context)
        if row is None and not refresh:
            return _error_response(
                status_code=404,
                error_code="FIB_PAYMENT_NOT_FOUND",
                message="Payment was not found.",
                details={"paymentId": payment_id},
            )
        if row is not None and not refresh:
            if not _actor_matches_payment_context(
                owner_user_id=owner_user_id,
                owner_admin_user_id=owner_admin_user_id,
                row=row,
            ):
                return _error_response(
                    status_code=403,
                    error_code="PAYMENT_FORBIDDEN",
                    message="You do not have access to this payment.",
                )
            return _to_checkout_response(row)

        provider_payment_id = row.provider_payment_id if row is not None else payment_id
        if not provider_payment_id:
            if row is not None:
                return _to_checkout_response(row)
            return _error_response(
                status_code=422,
                error_code="MISSING_PROVIDER_PAYMENT_ID",
                message="Unable to verify payment without provider payment reference.",
            )
        try:
            provider_status = await provider.get_payment_status(provider_payment_id)
            resolved_row = _upsert_successful_attempt_from_provider_status(
                db=db,
                store=store,
                provider_payment_id=provider_payment_id,
                provider_status=provider_status,
                transaction_id_hint=row.transaction_id if row is not None else None,
            )
            db.commit()
            if resolved_row is not None:
                if not _actor_matches_payment_context(
                    owner_user_id=owner_user_id,
                    owner_admin_user_id=owner_admin_user_id,
                    row=resolved_row,
                ):
                    return _error_response(
                        status_code=403,
                        error_code="PAYMENT_FORBIDDEN",
                        message="You do not have access to this payment.",
                    )
                db.refresh(resolved_row)
                return _to_checkout_response(resolved_row)
            checkout_context = _resolve_checkout_context(store=store, provider_payment_id=provider_payment_id) or {}
            if not _actor_matches_payment_context(
                owner_user_id=owner_user_id,
                owner_admin_user_id=owner_admin_user_id,
                context=checkout_context,
            ):
                return _error_response(
                    status_code=403,
                    error_code="PAYMENT_FORBIDDEN",
                    message="You do not have access to this payment.",
                )
            status_payload = provider_status.model_dump(by_alias=True, exclude_none=True)
            checkout_context = {
                **checkout_context,
                "providerPaymentId": provider_payment_id,
                "paymentId": provider_payment_id,
                "providerStatus": provider_status.status,
                "normalizedStatus": _normalize_payment_status(provider_status.status),
                "amountMinor": _as_int(provider_status.amount.amount if provider_status.amount else None, default=0),
                "currencyCode": (provider_status.amount.currency if provider_status.amount else "IQD"),
                "providerStatusPayload": status_payload,
                "paidAt": provider_status.paid_at,
                "expiresAt": provider_status.valid_until,
            }
            return _to_checkout_response_from_context(checkout_context)
        except Exception as exc:
            return _map_fib_exception(exc)

    @app.post("/api/v1/payments/fib/confirm")
    async def confirm_payment(
        payload: FIBConfirmRequest,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        db: Session = Depends(get_db),
        actor: tuple[str | None, str | None, str] = Depends(_require_payment_actor),
    ) -> Any:
        owner_user_id, owner_admin_user_id, _ = actor
        if not payload.payment_id and not payload.transaction_id:
            return _error_response(
                status_code=422,
                error_code="INVALID_CONFIRM_REQUEST",
                message="Either paymentId or transactionId is required.",
            )

        store = SupabaseStore(db)
        row: PaymentAttempt | None = None
        if payload.payment_id:
            row = store.get_payment_attempt_by_provider_payment_id(
                provider="fib",
                provider_payment_id=payload.payment_id,
            )
            if row is None:
                row = store.get_payment_attempt_by_any_reference(payload.payment_id)
        if row is None and payload.transaction_id:
            row = store.get_payment_attempt_by_transaction_id(payload.transaction_id)
        if row is not None and not _actor_matches_payment_context(
            owner_user_id=owner_user_id,
            owner_admin_user_id=owner_admin_user_id,
            row=row,
        ):
            return _error_response(
                status_code=403,
                error_code="PAYMENT_FORBIDDEN",
                message="You do not have access to this payment.",
            )

        provider_payment_id = payload.payment_id or (row.provider_payment_id if row is not None else None)
        if provider_payment_id is None and payload.transaction_id:
            checkout_context = _resolve_checkout_context(store=store, transaction_id=payload.transaction_id)
            provider_payment_id = _metadata_string(checkout_context or {}, ("providerPaymentId", "paymentId"))
        if not provider_payment_id:
            return _error_response(
                status_code=422,
                error_code="MISSING_PROVIDER_PAYMENT_ID",
                message="Unable to verify payment without provider payment reference.",
            )

        try:
            provider_status = await provider.get_payment_status(provider_payment_id)
            resolved_row = _upsert_successful_attempt_from_provider_status(
                db=db,
                store=store,
                provider_payment_id=provider_payment_id,
                provider_status=provider_status,
                transaction_id_hint=payload.transaction_id or (row.transaction_id if row is not None else None),
            )
            db.commit()
            if resolved_row is not None:
                if not _actor_matches_payment_context(
                    owner_user_id=owner_user_id,
                    owner_admin_user_id=owner_admin_user_id,
                    row=resolved_row,
                ):
                    return _error_response(
                        status_code=403,
                        error_code="PAYMENT_FORBIDDEN",
                        message="You do not have access to this payment.",
                    )
                db.refresh(resolved_row)
                return _to_checkout_response(resolved_row)
            checkout_context = _resolve_checkout_context(
                store=store,
                transaction_id=payload.transaction_id,
                provider_payment_id=provider_payment_id,
            ) or {}
            if not _actor_matches_payment_context(
                owner_user_id=owner_user_id,
                owner_admin_user_id=owner_admin_user_id,
                context=checkout_context,
            ):
                return _error_response(
                    status_code=403,
                    error_code="PAYMENT_FORBIDDEN",
                    message="You do not have access to this payment.",
                )
            status_payload = provider_status.model_dump(by_alias=True, exclude_none=True)
            checkout_context = {
                **checkout_context,
                "providerPaymentId": provider_payment_id,
                "paymentId": provider_payment_id,
                "providerStatus": provider_status.status,
                "normalizedStatus": _normalize_payment_status(provider_status.status),
                "amountMinor": _as_int(provider_status.amount.amount if provider_status.amount else None, default=0),
                "currencyCode": (provider_status.amount.currency if provider_status.amount else "IQD"),
                "providerStatusPayload": status_payload,
                "paidAt": provider_status.paid_at,
                "expiresAt": provider_status.valid_until,
            }
            return _to_checkout_response_from_context(checkout_context)
        except Exception as exc:
            return _map_fib_exception(exc)

    @app.post("/api/v1/payments/fib/webhook")
    async def receive_webhook(
        request: Request,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        db: Session = Depends(get_db),
        x_fib_webhook_secret: str | None = Header(default=None, alias="X-FIB-WEBHOOK-SECRET"),
    ) -> JSONResponse:
        raw_body = await request.body()
        signature_valid: bool | None = None
        if provider.webhook_secret:
            expected = hmac.new(provider.webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            signature = _extract_webhook_signature({k.lower(): v for k, v in request.headers.items()})
            secret_matches = bool(
                x_fib_webhook_secret and hmac.compare_digest(x_fib_webhook_secret, provider.webhook_secret)
            )
            signature_matches = bool(signature and hmac.compare_digest(signature, expected))
            signature_valid = secret_matches or signature_matches
            if not signature_valid:
                return _error_response(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    error_code="INVALID_WEBHOOK_SIGNATURE",
                    message="Webhook signature validation failed.",
                )

        try:
            payload_raw = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return _error_response(
                status_code=400,
                error_code="INVALID_WEBHOOK_PAYLOAD",
                message="Webhook payload is not valid JSON.",
            )
        payload = payload_raw if isinstance(payload_raw, dict) else {"raw": payload_raw}

        provider_event_id = _extract_provider_event_id(payload)
        provider_payment_id = _extract_provider_payment_id(payload)
        webhook_transaction_id = _extract_webhook_transaction_id(payload)
        provider_status = _extract_provider_status(payload)
        normalized_status = _normalize_payment_status(provider_status)
        paid_at = parse_provider_datetime(_extract_paid_at(payload))
        event_type = f"fib.{(provider_status or 'unknown').strip().lower()}"

        store = SupabaseStore(db)
        event = None
        if provider_event_id:
            existing_event = store.get_payment_provider_event(provider="fib", provider_event_id=provider_event_id)
            if existing_event is not None and existing_event.processed:
                return JSONResponse(
                    status_code=202,
                    content={
                        "success": True,
                        "status": "accepted",
                        "duplicateEvent": True,
                        "eventId": existing_event.id,
                        "paymentAttemptId": existing_event.payment_attempt_id,
                    },
                )
            if existing_event is not None:
                event = existing_event
            else:
                event = store.create_payment_provider_event(
                    provider="fib",
                    event_type=event_type,
                    provider_event_id=provider_event_id,
                    signature_valid=signature_valid,
                    raw_payload=payload,
                    processed=False,
                )
        if event is None:
            event = store.create_payment_provider_event(
                provider="fib",
                event_type=event_type,
                provider_event_id=provider_event_id,
                signature_valid=signature_valid,
                raw_payload=payload,
                processed=False,
            )

        row: PaymentAttempt | None = None
        if provider_payment_id:
            row = store.get_payment_attempt_by_provider_payment_id(
                provider="fib",
                provider_payment_id=provider_payment_id,
                for_update=True,
            )
        if row is None and webhook_transaction_id:
            row = store.get_payment_attempt_by_transaction_id(webhook_transaction_id, for_update=True)

        transition_applied = False
        if row is not None and provider_payment_id:
            store.update_payment_attempt(
                row,
                provider="fib",
                provider_payment_id=provider_payment_id,
                provider_reference=provider_event_id,
                provider_response={
                    "providerStatus": provider_status,
                    "webhookPayload": payload,
                    "providerRefs": {
                        "providerPaymentId": provider_payment_id,
                        "providerEventId": provider_event_id,
                    },
                },
            )
            if normalized_status in PERSISTED_PAYMENT_STATUSES:
                transition_applied = store.apply_payment_status_transition(
                    row,
                    new_status=normalized_status,
                    paid_at=paid_at,
                )
                processing_error = None
            else:
                transition_applied = False
                processing_error = "Ignored non-success payment status for success-only payment_attempts policy."
            store.mark_payment_provider_event_processed(
                event,
                processed=True,
                payment_attempt_id=row.id,
                processing_error=processing_error,
            )
        elif provider_payment_id:
            resolved_row = _upsert_successful_attempt_from_provider_status(
                db=db,
                store=store,
                provider_payment_id=provider_payment_id,
                provider_status=payload,
                transaction_id_hint=webhook_transaction_id,
            )
            if resolved_row is not None:
                row = resolved_row
                transition_applied = True
                store.mark_payment_provider_event_processed(
                    event,
                    processed=True,
                    payment_attempt_id=resolved_row.id,
                )
            else:
                store.mark_payment_provider_event_processed(
                    event,
                    processed=True,
                    processing_error=(
                        "Payment attempt not persisted because status is non-successful "
                        "or owner context could not be resolved."
                    ),
                )
        else:
            store.mark_payment_provider_event_processed(
                event,
                processed=False,
                processing_error="Webhook payload is missing provider payment identifier.",
            )

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if provider_payment_id:
                row = store.get_payment_attempt_by_provider_payment_id(
                    provider="fib",
                    provider_payment_id=provider_payment_id,
                )
            if row is None:
                return _error_response(
                    status_code=409,
                    error_code="PAYMENT_UPDATE_CONFLICT",
                    message="Payment update conflicted with concurrent operation.",
                )

        return JSONResponse(
            status_code=202,
            content={
                "success": True,
                "status": "accepted",
                "eventId": event.id if event is not None else None,
                "paymentAttemptId": row.id if row is not None else None,
                "paymentId": row.provider_payment_id if row else provider_payment_id,
                "transactionId": row.transaction_id if row else None,
                "normalizedStatus": row.status if row else normalized_status,
                "transitionApplied": transition_applied,
            },
        )

    # Backward-compatible aliases for already deployed frontend/mobile builds.
    @app.post("/api/v1/fib-payments/payments", status_code=201)
    async def create_payment_legacy(
        payload: CreatePaymentRequest,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> dict[str, Any]:
        result = await provider.create_payment(payload)
        return {"provider": result.model_dump(by_alias=True, exclude_none=True)}

    @app.get("/api/v1/fib-payments/payments/{payment_id}/status")
    async def get_payment_status_legacy(
        payment_id: str,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> dict[str, Any]:
        result = await provider.get_payment_status(payment_id)
        return {"provider": result.model_dump(by_alias=True, exclude_none=True)}

    @app.post("/api/v1/fib-payments/payments/{payment_id}/cancel")
    async def cancel_payment_legacy(
        payment_id: str,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> Response:
        await provider.cancel_payment(payment_id)
        return Response(status_code=204)

    @app.post("/api/v1/fib-payments/payments/{payment_id}/refund")
    async def refund_payment_legacy(
        payment_id: str,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> Response:
        await provider.refund_payment(payment_id)
        return Response(status_code=202)

    @app.post("/api/v1/fib-payments/webhooks/events")
    async def receive_webhook_legacy(
        payload: PaymentWebhookEvent,
        request: Request,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        db: Session = Depends(get_db),
        x_fib_webhook_secret: str | None = Header(default=None, alias="X-FIB-WEBHOOK-SECRET"),
    ) -> JSONResponse:
        # Re-route to canonical webhook by preserving request body shape.
        _ = payload
        return await receive_webhook(
            request=request,
            provider=provider,
            db=db,
            x_fib_webhook_secret=x_fib_webhook_secret,
        )
