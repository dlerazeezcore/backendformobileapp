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

from supabase_store import PaymentAttempt, SupabaseStore, parse_provider_datetime


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
) -> PaymentAttempt:
    store = SupabaseStore(db)
    metadata = dict(payload.metadata or {})
    transaction_id = _extract_transaction_id(metadata)

    existing = store.get_payment_attempt_by_transaction_id(transaction_id)
    if existing is not None:
        return existing

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

    row = store.create_payment_attempt(
        transaction_id=transaction_id,
        payment_method="fib",
        provider="fib",
        provider_payment_id=provider_response.payment_id,
        provider_reference=provider_response.readable_code,
        status="pending",
        amount_minor=amount_minor,
        currency_code=currency_code,
        customer_order_id=_metadata_int(metadata, ("customerOrderId", "customer_order_id")),
        user_id=_metadata_string(metadata, ("userId", "user_id")),
        service_type=_metadata_string(metadata, ("serviceType", "service_type")) or "esim",
        order_item_id=_metadata_int(metadata, ("orderItemId", "order_item_id")),
        idempotency_key=_metadata_string(metadata, ("idempotencyKey", "idempotency_key")),
        metadata=metadata,
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
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate = store.get_payment_attempt_by_transaction_id(transaction_id)
        if duplicate is not None:
            return duplicate
        duplicate = store.get_payment_attempt_by_provider_payment_id(
            provider=None,
            provider_payment_id=provider_response.payment_id,
        )
        if duplicate is not None:
            return duplicate
        raise
    db.refresh(row)
    return row


def register_fib_payment_routes(
    app: FastAPI,
    get_fib_provider: Callable[..., FIBPaymentAPI],
    get_db: Callable[..., Any],
) -> None:
    @app.post("/api/v1/payments/fib/checkout")
    @app.post("/api/v1/payments/fib/create")
    @app.post("/api/v1/payments/fib/intent")
    @app.post("/api/v1/payments/fib/initiate")
    async def checkout_payment(
        payload: FIBCheckoutRequest,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        db: Session = Depends(get_db),
    ) -> Any:
        try:
            row = await _create_checkout_attempt(payload=payload, provider=provider, db=db)
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
    ) -> Any:
        store = SupabaseStore(db)
        row = store.get_payment_attempt_by_any_reference(payment_id)
        if row is None:
            return _error_response(
                status_code=404,
                error_code="FIB_PAYMENT_NOT_FOUND",
                message="Payment was not found.",
                details={"paymentId": payment_id},
            )
        if not refresh:
            return _to_checkout_response(row)

        provider_payment_id = row.provider_payment_id or payment_id
        if not provider_payment_id:
            return _to_checkout_response(row)
        try:
            provider_status = await provider.get_payment_status(provider_payment_id)
            _apply_verified_status(
                store=store,
                row=row,
                provider_payment_id=provider_payment_id,
                provider_status=provider_status,
            )
            db.commit()
            db.refresh(row)
            return _to_checkout_response(row)
        except Exception as exc:
            return _map_fib_exception(exc)

    @app.post("/api/v1/payments/fib/confirm")
    async def confirm_payment(
        payload: FIBConfirmRequest,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        db: Session = Depends(get_db),
    ) -> Any:
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
                provider=None,
                provider_payment_id=payload.payment_id,
            )
            if row is None:
                row = store.get_payment_attempt_by_any_reference(payload.payment_id)
        if row is None and payload.transaction_id:
            row = store.get_payment_attempt_by_transaction_id(payload.transaction_id)
        if row is None:
            return _error_response(
                status_code=404,
                error_code="FIB_PAYMENT_NOT_FOUND",
                message="Payment was not found.",
            )

        provider_payment_id = payload.payment_id or row.provider_payment_id
        if not provider_payment_id:
            return _error_response(
                status_code=422,
                error_code="MISSING_PROVIDER_PAYMENT_ID",
                message="Unable to verify payment without provider payment reference.",
            )

        try:
            provider_status = await provider.get_payment_status(provider_payment_id)
            _apply_verified_status(
                store=store,
                row=row,
                provider_payment_id=provider_payment_id,
                provider_status=provider_status,
            )
            db.commit()
            db.refresh(row)
            return _to_checkout_response(row)
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
                provider=None,
                provider_payment_id=provider_payment_id,
                for_update=True,
            )
        if row is None and webhook_transaction_id:
            row = store.get_payment_attempt_by_transaction_id(webhook_transaction_id, for_update=True)

        if row is None:
            metadata = {
                "source": "fib_webhook",
                "providerPaymentId": provider_payment_id,
                "providerEventId": provider_event_id,
            }
            row = store.create_payment_attempt(
                transaction_id=webhook_transaction_id or _extract_transaction_id(metadata),
                payment_method="fib",
                provider="fib",
                provider_payment_id=provider_payment_id,
                provider_reference=provider_event_id,
                status="pending",
                amount_minor=0,
                currency_code="IQD",
                metadata=metadata,
                provider_request={},
                provider_response={
                    "providerStatus": provider_status,
                    "webhookPayload": payload,
                    "providerRefs": {"providerPaymentId": provider_payment_id},
                },
            )
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
        transition_applied = store.apply_payment_status_transition(
            row,
            new_status=normalized_status,
            paid_at=paid_at,
        )
        store.mark_payment_provider_event_processed(
            event,
            processed=True,
            payment_attempt_id=row.id,
        )

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if provider_payment_id:
                row = store.get_payment_attempt_by_provider_payment_id(
                    provider=None,
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
