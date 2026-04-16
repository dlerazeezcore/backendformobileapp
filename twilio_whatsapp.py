from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class TwilioVerifyHTTPError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class TwilioVerifyAPIError(Exception):
    status_code: int
    error_code: str
    error_message: str
    payload: dict[str, Any]

    def __str__(self) -> str:
        return self.error_message


class AsyncRateLimiter:
    def __init__(self, rate_per_second: float) -> None:
        self.rate_per_second = max(rate_per_second, 0.1)
        self.next_allowed = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        interval = 1.0 / self.rate_per_second
        loop = asyncio.get_running_loop()
        async with self.lock:
            now = loop.time()
            wait_for = self.next_allowed - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = loop.time()
            self.next_allowed = max(self.next_allowed, now) + interval


class TwilioWhatsAppVerifyAPI:
    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        verify_service_sid: str,
        base_url: str = "https://verify.twilio.com",
        timeout: float = 20.0,
        rate_limit_per_second: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account_sid = account_sid.strip()
        self.auth_token = auth_token.strip()
        self.verify_service_sid = verify_service_sid.strip()
        self.rate_limiter = AsyncRateLimiter(rate_limit_per_second)
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            auth=(self.account_sid, self.auth_token),
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _post_form(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        await self.rate_limiter.wait()
        try:
            response = await self.client.post(path, data=payload)
        except httpx.HTTPError as exc:
            raise TwilioVerifyHTTPError(f"Twilio Verify HTTP failure: {exc}") from exc

        body: dict[str, Any]
        try:
            body = response.json()
        except Exception:
            body = {}

        if response.status_code >= 400:
            raise TwilioVerifyAPIError(
                status_code=response.status_code,
                error_code=str(body.get("code") or "TWILIO_VERIFY_ERROR"),
                error_message=str(body.get("message") or "Twilio Verify request failed."),
                payload=body,
            )
        return body

    async def start_verification(self, *, phone: str, channel: str = "sms") -> dict[str, Any]:
        normalized_channel = str(channel or "sms").strip().lower()
        if normalized_channel not in {"whatsapp", "sms"}:
            raise TwilioVerifyAPIError(
                status_code=422,
                error_code="UNSUPPORTED_VERIFY_CHANNEL",
                error_message="Unsupported Twilio Verify channel.",
                payload={"channel": normalized_channel},
            )
        return await self._post_form(
            f"/v2/Services/{self.verify_service_sid}/Verifications",
            {
                "To": phone,
                "Channel": normalized_channel,
            },
        )

    async def check_verification(
        self,
        *,
        phone: str,
        code: str,
        verification_sid: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "To": phone,
            "Code": str(code).strip(),
        }
        if verification_sid:
            payload["VerificationSid"] = verification_sid.strip()
        return await self._post_form(
            f"/v2/Services/{self.verify_service_sid}/VerificationCheck",
            payload,
        )
