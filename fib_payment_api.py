from __future__ import annotations

import asyncio
import uuid
from time import time
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


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


class PaymentWebhookEvent(Model):
    id: str | None = None
    payment_id: str | None = Field(default=None, alias="paymentId")
    status: PaymentStatusResponse | dict[str, Any] | str | None = None


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


def register_fib_payment_routes(
    app: FastAPI,
    get_fib_provider: Callable[..., FIBPaymentAPI],
) -> None:
    @app.post("/api/v1/fib-payments/payments", status_code=201)
    async def create_payment(
        payload: CreatePaymentRequest,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> dict[str, Any]:
        result = await provider.create_payment(payload)
        return {"provider": result.model_dump(by_alias=True, exclude_none=True)}

    @app.get("/api/v1/fib-payments/payments/{payment_id}/status")
    async def get_payment_status(
        payment_id: str,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> dict[str, Any]:
        result = await provider.get_payment_status(payment_id)
        return {"provider": result.model_dump(by_alias=True, exclude_none=True)}

    @app.post("/api/v1/fib-payments/payments/{payment_id}/cancel")
    async def cancel_payment(
        payment_id: str,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> Response:
        await provider.cancel_payment(payment_id)
        return Response(status_code=204)

    @app.post("/api/v1/fib-payments/payments/{payment_id}/refund")
    async def refund_payment(
        payment_id: str,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
    ) -> Response:
        await provider.refund_payment(payment_id)
        return Response(status_code=202)

    @app.post("/api/v1/fib-payments/webhooks/events")
    async def receive_webhook(
        payload: PaymentWebhookEvent,
        provider: FIBPaymentAPI = Depends(get_fib_provider),
        x_fib_webhook_secret: str | None = Header(default=None, alias="X-FIB-WEBHOOK-SECRET"),
    ) -> JSONResponse:
        # Optional static secret validation for deployments that can set a shared header.
        if provider.webhook_secret and x_fib_webhook_secret != provider.webhook_secret:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid FIB webhook secret")
        payment_status = payload.status
        if isinstance(payment_status, dict):
            payment_status = payment_status.get("status")
        elif isinstance(payment_status, PaymentStatusResponse):
            payment_status = payment_status.status
        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "paymentId": payload.id or payload.payment_id,
                "paymentStatus": payment_status,
            },
        )
