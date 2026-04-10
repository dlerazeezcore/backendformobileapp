from __future__ import annotations

import hmac
import re
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_token_claims, require_active_subject
from config import get_settings
from push_notification import PushNotificationService
from phone_utils import phone_lookup_candidates
from supabase_store import AdminUser, AppUser, PushDevice, TelegramSupportMessage, utcnow

TELEGRAM_SUPPORT_CHAT_ID = -5169340336
USER_ID_PATTERN = re.compile(r"User ID:\s*([0-9a-fA-F-]{36})")
PHONE_PATTERN = re.compile(r"Phone:\s*(\+?[0-9][0-9\s\-]{6,})")


class SupportMessagePayload(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str | None = Field(default=None, alias="userId")


async def _telegram_send_message(*, bot_token: str, chat_id: int, text: str, reply_to: int | None = None) -> dict[str, Any]:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_parameters"] = {"message_id": reply_to}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(api_url, json=payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Telegram provider rejected sendMessage request")
    body = response.json()
    if not bool(body.get("ok")):
        raise HTTPException(status_code=502, detail="Telegram provider returned an unsuccessful response")
    return body


def _render_user_message_for_telegram(*, actor: AppUser, text: str) -> str:
    return (
        "📩 Support message\n"
        f"Phone: {actor.phone}\n"
        f"Name: {actor.name}\n"
        "\n"
        f"{text.strip()}"
    )


def _extract_user_id_from_text(text: str) -> str | None:
    match = USER_ID_PATTERN.search(text)
    if match is None:
        return None
    return match.group(1)


def _extract_phone_from_text(text: str) -> str | None:
    match = PHONE_PATTERN.search(text)
    if match is None:
        return None
    return match.group(1).strip()


def _find_user_by_phone(db: Session, phone: str) -> AppUser | None:
    candidates = phone_lookup_candidates(phone)
    if not candidates:
        return None
    return db.scalar(select(AppUser).where(AppUser.phone.in_(candidates)))


def register_telegram_support_routes(
    app: FastAPI,
    get_db: Callable[..., Any],
    get_push_provider: Callable[..., PushNotificationService],
) -> None:
    async def _require_active_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AppUser | AdminUser:
        return require_active_subject(db, claims=claims)

    @app.post("/api/v1/support/telegram/messages")
    async def send_support_message(
        payload: SupportMessagePayload,
        db: Session = Depends(get_db),
        actor: AppUser | AdminUser = Depends(_require_active_actor),
        push_provider: PushNotificationService = Depends(get_push_provider),
    ) -> dict[str, Any]:
        if isinstance(actor, AppUser):
            settings = get_settings()
            bot_token = str(settings.telegram_support_bot_token or "").strip()
            if not bot_token:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Telegram support is not configured")

            row = TelegramSupportMessage(
                user_id=actor.id,
                direction="user_to_admin",
                status="pending",
                message_text=payload.message.strip(),
                telegram_chat_id=TELEGRAM_SUPPORT_CHAT_ID,
            )
            db.add(row)
            db.flush()

            outbound_text = _render_user_message_for_telegram(actor=actor, text=payload.message)
            try:
                sent = await _telegram_send_message(bot_token=bot_token, chat_id=TELEGRAM_SUPPORT_CHAT_ID, text=outbound_text)
                result = sent.get("result") or {}
                row.telegram_message_id = int(result.get("message_id")) if result.get("message_id") is not None else None
                row.provider_payload = sent
                row.status = "sent"
            except HTTPException as exc:
                row.status = "failed"
                row.error_message = str(exc.detail)
                row.updated_at = utcnow()
                db.commit()
                raise

            row.updated_at = utcnow()
            db.commit()
            db.refresh(row)
            return {
                "message": {
                    "id": row.id,
                    "userId": row.user_id,
                    "direction": row.direction,
                    "status": row.status,
                    "telegramMessageId": row.telegram_message_id,
                    "createdAt": row.created_at,
                }
            }

        target_user_id = payload.user_id
        if not target_user_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="userId is required for admin messages")

        row = TelegramSupportMessage(
            user_id=target_user_id,
            admin_user_id=actor.id,
            direction="admin_to_user",
            status="pending",
            message_text=payload.message.strip(),
            push_delivery_status="pending",
        )
        db.add(row)
        db.flush()

        tokens = db.scalars(select(PushDevice.token).where(PushDevice.user_id == target_user_id, PushDevice.active.is_(True))).all()
        if tokens:
            try:
                send_result = push_provider.send_push_notification(
                    tokens=tokens,
                    title="Support reply",
                    body=payload.message.strip()[:2000],
                    data={"type": "support_reply", "supportMessageId": row.id},
                    channel_id="support",
                )
                if int(send_result.get("successCount") or 0) > 0:
                    row.push_delivery_status = "sent"
                    row.status = "sent"
                else:
                    row.push_delivery_status = "failed"
                    row.status = "failed"
            except Exception as exc:  # noqa: BLE001
                row.push_delivery_status = "failed"
                row.status = "failed"
                row.error_message = str(exc)
        else:
            row.push_delivery_status = "no_devices"
            row.status = "sent"

        row.updated_at = utcnow()
        db.commit()
        db.refresh(row)
        return {
            "message": {
                "id": row.id,
                "userId": row.user_id,
                "direction": row.direction,
                "status": row.status,
                "pushDeliveryStatus": row.push_delivery_status,
                "createdAt": row.created_at,
            }
        }

    @app.get("/api/v1/support/telegram/messages")
    async def list_my_support_messages(
        db: Session = Depends(get_db),
        actor: AppUser | AdminUser = Depends(_require_active_actor),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        user_id: str | None = Query(default=None, alias="userId"),
    ) -> dict[str, Any]:
        query = select(TelegramSupportMessage).order_by(TelegramSupportMessage.created_at.desc())
        if isinstance(actor, AppUser):
            query = query.where(TelegramSupportMessage.user_id == actor.id)
        elif user_id is not None:
            query = query.where(TelegramSupportMessage.user_id == user_id)

        rows = db.scalars(query.offset(offset).limit(limit)).all()
        return {
            "messages": [
                {
                    "id": row.id,
                    "userId": row.user_id,
                    "direction": row.direction,
                    "status": row.status,
                    "message": row.message_text,
                    "createdAt": row.created_at,
                    "pushDeliveryStatus": row.push_delivery_status,
                }
                for row in rows
            ],
            "pagination": {"limit": limit, "offset": offset, "count": len(rows)},
        }

    @app.post("/api/v1/support/telegram/webhook")
    async def telegram_webhook(
        payload: dict[str, Any],
        db: Session = Depends(get_db),
        push_provider: PushNotificationService = Depends(get_push_provider),
        secret_header: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> dict[str, Any]:
        settings = get_settings()
        configured_secret = str(settings.telegram_support_webhook_secret or "").strip()
        if not configured_secret or not hmac.compare_digest(str(secret_header or ""), configured_secret):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Telegram webhook secret")

        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return {"ok": True, "ignored": "no_message"}

        text = str(message.get("text") or "").strip()
        if not text:
            return {"ok": True, "ignored": "empty_text"}

        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = int(chat.get("id")) if chat.get("id") is not None else TELEGRAM_SUPPORT_CHAT_ID
        telegram_message_id = int(message.get("message_id")) if message.get("message_id") is not None else None

        user_id: str | None = None
        reply_block = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else None
        if reply_block is not None:
            reply_id = int(reply_block.get("message_id")) if reply_block.get("message_id") is not None else None
            if reply_id is not None:
                parent = db.scalar(select(TelegramSupportMessage).where(TelegramSupportMessage.telegram_message_id == reply_id))
                if parent is not None and parent.user_id:
                    user_id = parent.user_id
            if user_id is None:
                reply_text = str(reply_block.get("text") or "")
                user_id = _extract_user_id_from_text(reply_text)

        if user_id is None:
            user_id = _extract_user_id_from_text(text)

        if user_id is None and reply_block is not None:
            reply_phone = _extract_phone_from_text(str(reply_block.get("text") or ""))
            if reply_phone:
                mapped_user = _find_user_by_phone(db, reply_phone)
                if mapped_user is not None:
                    user_id = mapped_user.id

        if user_id is None:
            phone_from_text = _extract_phone_from_text(text)
            if phone_from_text:
                mapped_user = _find_user_by_phone(db, phone_from_text)
                if mapped_user is not None:
                    user_id = mapped_user.id

        row = TelegramSupportMessage(
            user_id=user_id,
            direction="admin_to_user",
            status="received",
            message_text=text,
            telegram_chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            provider_payload=payload,
        )
        db.add(row)
        db.flush()

        if user_id:
            tokens = db.scalars(
                select(PushDevice.token).where(PushDevice.user_id == user_id, PushDevice.active.is_(True))
            ).all()
            if tokens:
                try:
                    send_result = push_provider.send_push_notification(
                        tokens=tokens,
                        title="Support reply",
                        body=text[:2000],
                        data={"type": "support_reply", "supportMessageId": row.id},
                        channel_id="support",
                    )
                    if int(send_result.get("successCount") or 0) > 0:
                        row.push_delivery_status = "sent"
                    else:
                        row.push_delivery_status = "failed"
                except Exception as exc:  # pragma: no cover - provider runtime failure
                    row.push_delivery_status = "failed"
                    row.error_message = str(exc)
            else:
                row.push_delivery_status = "no_devices"

        row.updated_at = utcnow()
        db.commit()
        return {"ok": True, "messageId": row.id, "userId": user_id, "pushDeliveryStatus": row.push_delivery_status}
