from __future__ import annotations

import hmac
import mimetypes
import re
import uuid
from typing import Any, Callable
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_token_claims, require_active_subject
from config import get_settings
from push_notification import PushNotificationService
from phone_utils import phone_lookup_candidates
from supabase_store import AdminUser, AppUser, PushDevice, TelegramSupportMessage, utcnow

TELEGRAM_SUPPORT_CHAT_ID = -5169340336
USER_ID_PATTERN = re.compile(r"User ID:\s*([0-9a-fA-F-]{36})")
PHONE_PATTERN = re.compile(r"Phone:\s*(\+?[0-9][0-9\s\-]{6,})")
ALLOWED_SUPPORT_UPLOAD_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
MAX_SUPPORT_ATTACHMENTS = 5

try:
    import boto3
    import botocore.handlers
    from botocore.config import Config as BotoConfig
except Exception:  # pragma: no cover - runtime dependency handling
    boto3 = None
    botocore = None
    BotoConfig = None


class SupportAttachmentPayload(BaseModel):
    object_path: str | None = Field(default=None, alias="objectPath")
    public_url: str | None = Field(default=None, alias="publicUrl")
    file_name: str | None = Field(default=None, alias="fileName")
    content_type: str | None = Field(default=None, alias="contentType")
    size_bytes: int | None = Field(default=None, alias="sizeBytes")


class SupportMessagePayload(BaseModel):
    message: str = Field(default="", max_length=4000)
    user_id: str | None = Field(default=None, alias="userId")
    attachments: list[SupportAttachmentPayload] = Field(default_factory=list, max_length=MAX_SUPPORT_ATTACHMENTS)

    @model_validator(mode="after")
    def validate_message_or_attachments(self) -> "SupportMessagePayload":
        if self.message.strip() or self.attachments:
            return self
        raise ValueError("message or attachments is required")


class SupportUploadPresignPayload(BaseModel):
    file_name: str = Field(min_length=1, max_length=255, alias="fileName")
    content_type: str = Field(min_length=1, max_length=128, alias="contentType")
    size_bytes: int = Field(gt=0, alias="sizeBytes")

    @model_validator(mode="after")
    def validate_content_type(self) -> "SupportUploadPresignPayload":
        normalized = self.content_type.lower().strip()
        if normalized not in ALLOWED_SUPPORT_UPLOAD_CONTENT_TYPES:
            allowed = ", ".join(sorted(ALLOWED_SUPPORT_UPLOAD_CONTENT_TYPES))
            raise ValueError(f"Unsupported contentType. Allowed: {allowed}")
        self.content_type = normalized
        return self


def _normalize_support_attachment(attachment: SupportAttachmentPayload) -> dict[str, Any]:
    return {
        "objectPath": str(attachment.object_path or "").strip() or None,
        "publicUrl": str(attachment.public_url or "").strip() or None,
        "fileName": str(attachment.file_name or "").strip() or None,
        "contentType": str(attachment.content_type or "").strip() or None,
        "sizeBytes": int(attachment.size_bytes or 0) if attachment.size_bytes is not None else None,
    }


def _normalize_support_attachments(attachments: list[SupportAttachmentPayload]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for attachment in attachments:
        item = _normalize_support_attachment(attachment)
        if not item["publicUrl"] and not item["objectPath"]:
            continue
        normalized.append(item)
    return normalized


def _build_support_upload_client(settings: Any):
    if boto3 is None or BotoConfig is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Support upload dependencies are not available in this runtime.",
        )
    endpoint = str(settings.support_uploads_s3_endpoint or "").strip()
    access_key_id = str(settings.support_uploads_access_key_id or "").strip()
    secret_access_key = str(settings.support_uploads_secret_access_key or "").strip()
    if not endpoint or not access_key_id or not secret_access_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Support uploads are not configured on this deployment.",
        )
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=str(settings.support_uploads_s3_region or "").strip() or "ap-southeast-2",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=BotoConfig(signature_version="s3v4"),
    )
    # Supabase bucket names can include spaces (e.g. "Tulip Mobile APP"), while
    # botocore enforces AWS bucket-name regex in local pre-validation.
    # Remove only this client-side validation hook so presign can proceed.
    if botocore is not None:
        client.meta.events.unregister("before-parameter-build.s3", botocore.handlers.validate_bucket_name)
        client.meta.events.unregister("before-parameter-build.s3.PutObject", botocore.handlers.validate_bucket_name)
    return client


def _sanitize_support_filename(name: str) -> str:
    value = str(name or "").strip().replace("\\", "_").replace("/", "_")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value or "support_image"


def _derive_supabase_public_base_url_from_s3_endpoint(endpoint: str) -> str | None:
    marker = "/storage/v1/s3"
    if marker not in endpoint:
        return None
    return endpoint.split(marker, 1)[0] + "/storage/v1/object/public"


def _build_support_public_url(*, settings: Any, bucket: str, object_path: str) -> str | None:
    configured = str(settings.support_uploads_public_base_url or "").strip()
    encoded_bucket = quote(bucket, safe="")
    encoded_object_path = quote(object_path, safe="/")
    if configured:
        base = configured.rstrip("/")
        if "{bucket}" in base:
            base = base.replace("{bucket}", encoded_bucket)
            return f"{base}/{encoded_object_path}"
        return f"{base}/{encoded_bucket}/{encoded_object_path}"
    endpoint = str(settings.support_uploads_s3_endpoint or "").strip()
    derived = _derive_supabase_public_base_url_from_s3_endpoint(endpoint)
    if not derived:
        return None
    return f"{derived.rstrip('/')}/{encoded_bucket}/{encoded_object_path}"


def _load_support_attachment_bytes(
    *,
    settings: Any,
    object_path: str,
) -> tuple[bytes, str | None]:
    bucket = str(settings.support_uploads_bucket or "").strip()
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Support uploads bucket is not configured on this deployment.",
        )
    client = _build_support_upload_client(settings)
    response = client.get_object(Bucket=bucket, Key=object_path)
    body = response["Body"].read()
    content_type = response.get("ContentType")
    return body, str(content_type).strip() if content_type else None


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


async def _telegram_send_photo(
    *,
    bot_token: str,
    chat_id: int,
    photo_url: str | None = None,
    photo_bytes: bytes | None = None,
    photo_filename: str | None = None,
    photo_content_type: str | None = None,
    caption: str | None = None,
    reply_to: int | None = None,
) -> dict[str, Any]:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    if not photo_url and photo_bytes is None:
        raise HTTPException(status_code=422, detail="photo_url or photo_bytes is required")
    payload: dict[str, Any] = {"chat_id": str(chat_id)}
    if caption:
        payload["caption"] = caption[:1024]
    if reply_to is not None:
        payload["reply_parameters"] = {"message_id": reply_to}
    async with httpx.AsyncClient(timeout=20.0) as client:
        if photo_bytes is not None:
            files = {
                "photo": (
                    photo_filename or "support-image.jpg",
                    photo_bytes,
                    photo_content_type or "image/jpeg",
                )
            }
            response = await client.post(api_url, data=payload, files=files)
        else:
            payload["photo"] = str(photo_url)
            response = await client.post(api_url, json=payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Telegram provider rejected sendPhoto request")
    body = response.json()
    if not bool(body.get("ok")):
        raise HTTPException(status_code=502, detail="Telegram provider returned an unsuccessful response")
    return body


async def _telegram_get_file_path(*, bot_token: str, file_id: str) -> str | None:
    api_url = f"https://api.telegram.org/bot{bot_token}/getFile"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(api_url, json={"file_id": file_id})
    if response.status_code >= 400:
        return None
    body = response.json()
    if not bool(body.get("ok")):
        return None
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    file_path = str(result.get("file_path") or "").strip()
    return file_path or None


async def _telegram_download_file(*, bot_token: str, file_path: str) -> tuple[bytes, str | None]:
    api_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(api_url)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Telegram provider rejected file download request")
    return response.content, response.headers.get("content-type")


async def _telegram_get_webhook_info(*, bot_token: str) -> dict[str, Any]:
    api_url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(api_url)
    if response.status_code >= 400:
        return {}
    body = response.json()
    if not bool(body.get("ok")):
        return {}
    result = body.get("result")
    return result if isinstance(result, dict) else {}


async def _telegram_set_webhook(*, bot_token: str, webhook_url: str, secret_token: str | None) -> bool:
    api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    payload: dict[str, Any] = {"url": webhook_url}
    if secret_token:
        payload["secret_token"] = secret_token
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(api_url, data=payload)
    if response.status_code >= 400:
        return False
    body = response.json()
    return bool(body.get("ok"))


def _build_telegram_webhook_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/support/telegram/webhook"


async def _ensure_telegram_webhook(bot_token: str, webhook_secret: str | None, webhook_base_url: str) -> None:
    info = await _telegram_get_webhook_info(bot_token=bot_token)
    desired_url = _build_telegram_webhook_url(webhook_base_url)
    current_url = str(info.get("url") or "").strip()
    # Self-heal if webhook is missing or points to a different endpoint.
    if current_url != desired_url:
        await _telegram_set_webhook(bot_token=bot_token, webhook_url=desired_url, secret_token=webhook_secret)


def _render_user_message_for_telegram(*, actor: AppUser, text: str, attachments: list[dict[str, Any]]) -> str:
    body_text = text.strip()
    if not body_text:
        body_text = "[Attachment only]"
    attachment_lines: list[str] = []
    for index, attachment in enumerate(attachments, start=1):
        url = str(attachment.get("publicUrl") or "").strip()
        object_path = str(attachment.get("objectPath") or "").strip()
        label = str(attachment.get("fileName") or "").strip() or f"Attachment {index}"
        destination = url or object_path
        if not destination:
            continue
        attachment_lines.append(f"{label}: {destination}")
    attachment_block = ""
    if attachment_lines:
        attachment_block = "\n\nAttachments:\n" + "\n".join(attachment_lines)
    return (
        "📩 Support message\n"
        f"User ID: {actor.id}\n"
        f"Phone: {actor.phone}\n"
        f"Name: {actor.name}\n"
        "\n"
        f"{body_text}{attachment_block}"
    )


def _extract_message_text_content(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("caption") or "").strip()


def _is_image_attachment(attachment: dict[str, Any]) -> bool:
    content_type = str(attachment.get("contentType") or "").strip().lower()
    if content_type.startswith("image/"):
        return True
    value = str(attachment.get("publicUrl") or attachment.get("objectPath") or "").strip().lower()
    return bool(re.search(r"\.(jpg|jpeg|png|webp|heic|heif)(?:$|[?#])", value))


def _build_inbound_support_object_path(*, settings: Any, file_name: str) -> str:
    date_prefix = utcnow().strftime("%Y/%m/%d")
    object_prefix = str(settings.support_uploads_object_prefix or "support").strip().strip("/")
    safe_name = _sanitize_support_filename(file_name)
    return f"{object_prefix}/telegram/inbound/{date_prefix}/{uuid.uuid4().hex}_{safe_name}"


def _upload_support_file_bytes(
    *,
    settings: Any,
    object_path: str,
    file_bytes: bytes,
    content_type: str | None,
) -> str | None:
    bucket = str(settings.support_uploads_bucket or "").strip()
    if not bucket:
        return None
    client = _build_support_upload_client(settings)
    params: dict[str, Any] = {
        "Bucket": bucket,
        "Key": object_path,
        "Body": file_bytes,
    }
    if content_type:
        params["ContentType"] = content_type
    client.put_object(**params)
    return _build_support_public_url(settings=settings, bucket=bucket, object_path=object_path)


async def _mirror_telegram_attachment_to_support_bucket(
    *,
    settings: Any,
    bot_token: str,
    file_id: str,
    fallback_name: str,
    fallback_content_type: str | None,
) -> dict[str, Any] | None:
    file_path = await _telegram_get_file_path(bot_token=bot_token, file_id=file_id)
    if not file_path:
        return None
    file_bytes, downloaded_content_type = await _telegram_download_file(bot_token=bot_token, file_path=file_path)
    guessed_name = file_path.split("/")[-1] if "/" in file_path else file_path
    file_name = _sanitize_support_filename(guessed_name or fallback_name)
    content_type = downloaded_content_type or fallback_content_type
    if not content_type:
        guessed_content_type, _ = mimetypes.guess_type(file_name)
        content_type = guessed_content_type or "application/octet-stream"
    object_path = _build_inbound_support_object_path(settings=settings, file_name=file_name)
    public_url = _upload_support_file_bytes(
        settings=settings,
        object_path=object_path,
        file_bytes=file_bytes,
        content_type=content_type,
    )
    return {
        "objectPath": object_path,
        "publicUrl": public_url,
        "fileName": file_name,
        "contentType": content_type,
        "sizeBytes": len(file_bytes),
        "source": "telegram",
        "telegramFileId": file_id,
    }


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


def _is_valid_webhook_secret(
    *,
    configured_secret: str,
    header_secret: str | None,
    query_secret: str | None,
    path_secret: str | None,
) -> bool:
    values = [str(header_secret or ""), str(query_secret or ""), str(path_secret or "")]
    return any(hmac.compare_digest(candidate, configured_secret) for candidate in values)


def _extract_telegram_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return None


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
        attachments = _normalize_support_attachments(payload.attachments)
        message_text = payload.message.strip()
        if isinstance(actor, AppUser):
            settings = get_settings()
            bot_token = str(settings.telegram_support_bot_token or "").strip()
            if not bot_token:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Telegram support is not configured")
            webhook_secret = str(settings.telegram_support_webhook_secret or "").strip() or None
            webhook_base_url = str(settings.telegram_support_webhook_base_url or "").strip()
            await _ensure_telegram_webhook(
                bot_token=bot_token,
                webhook_secret=webhook_secret,
                webhook_base_url=webhook_base_url,
            )

            row = TelegramSupportMessage(
                user_id=actor.id,
                direction="user_to_admin",
                status="pending",
                message_text=message_text or "[Attachment only]",
                telegram_chat_id=TELEGRAM_SUPPORT_CHAT_ID,
                provider_payload={"attachments": attachments},
            )
            db.add(row)
            db.flush()

            image_attachments = [
                attachment
                for attachment in attachments
                if _is_image_attachment(attachment)
            ]
            non_image_attachments = [attachment for attachment in attachments if attachment not in image_attachments]
            base_text = _render_user_message_for_telegram(actor=actor, text=message_text, attachments=[])
            try:
                sent_responses: list[dict[str, Any]] = []
                photo_send_errors: list[dict[str, str]] = []
                failed_image_attachments: list[dict[str, Any]] = []
                if image_attachments:
                    for index, attachment in enumerate(image_attachments):
                        caption = base_text if index == 0 else None
                        sent: dict[str, Any] | None = None
                        object_path = str(attachment.get("objectPath") or "").strip()
                        try:
                            if object_path:
                                photo_bytes, content_type = _load_support_attachment_bytes(
                                    settings=settings,
                                    object_path=object_path,
                                )
                                sent = await _telegram_send_photo(
                                    bot_token=bot_token,
                                    chat_id=TELEGRAM_SUPPORT_CHAT_ID,
                                    photo_bytes=photo_bytes,
                                    photo_filename=str(attachment.get("fileName") or "support-image.jpg"),
                                    photo_content_type=content_type or str(attachment.get("contentType") or "").strip() or None,
                                    caption=caption,
                                )
                            else:
                                sent = await _telegram_send_photo(
                                    bot_token=bot_token,
                                    chat_id=TELEGRAM_SUPPORT_CHAT_ID,
                                    photo_url=str(attachment.get("publicUrl")),
                                    caption=caption,
                                )
                        except Exception as exc:  # noqa: BLE001
                            photo_send_errors.append(
                                {
                                    "attachment": str(attachment.get("fileName") or attachment.get("publicUrl") or "unknown"),
                                    "error": str(exc),
                                }
                            )
                            failed_image_attachments.append(attachment)
                        if sent is not None:
                            sent_responses.append(sent)

                    remaining_attachments = [*non_image_attachments, *failed_image_attachments]
                    if remaining_attachments or not sent_responses:
                        other_text = _render_user_message_for_telegram(
                            actor=actor,
                            text=message_text if not sent_responses else "[Additional attachments]",
                            attachments=remaining_attachments,
                        )
                        sent_responses.append(
                            await _telegram_send_message(
                                bot_token=bot_token,
                                chat_id=TELEGRAM_SUPPORT_CHAT_ID,
                                text=other_text,
                            )
                        )
                else:
                    outbound_text = _render_user_message_for_telegram(
                        actor=actor,
                        text=message_text,
                        attachments=non_image_attachments,
                    )
                    sent_responses.append(
                        await _telegram_send_message(
                            bot_token=bot_token,
                            chat_id=TELEGRAM_SUPPORT_CHAT_ID,
                            text=outbound_text,
                        )
                    )
                first_result = (sent_responses[0].get("result") or {}) if sent_responses else {}
                row.telegram_message_id = (
                    int(first_result.get("message_id"))
                    if first_result.get("message_id") is not None
                    else None
                )
                row.provider_payload = {
                    "telegramResponses": sent_responses,
                    "attachments": attachments,
                    "photoSendErrors": photo_send_errors,
                }
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
                    "attachments": attachments,
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
            message_text=message_text or "[Attachment only]",
            push_delivery_status="pending",
            provider_payload={"attachments": attachments},
        )
        db.add(row)
        db.flush()

        tokens = db.scalars(select(PushDevice.token).where(PushDevice.user_id == target_user_id, PushDevice.active.is_(True))).all()
        if tokens:
            try:
                send_result = push_provider.send_push_notification(
                    tokens=tokens,
                    title="Support reply",
                    body=(message_text or "Support sent an attachment.")[:2000],
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
                "attachments": attachments,
                "createdAt": row.created_at,
            }
        }

    @app.post("/api/v1/support/uploads/presign")
    async def create_support_upload_presign(
        payload: SupportUploadPresignPayload,
        actor: AppUser | AdminUser = Depends(_require_active_actor),
    ) -> dict[str, Any]:
        settings = get_settings()
        max_file_bytes = int(settings.support_uploads_max_file_bytes)
        if payload.size_bytes > max_file_bytes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File is too large. Max allowed is {max_file_bytes} bytes.",
            )
        bucket = str(settings.support_uploads_bucket or "").strip()
        if not bucket:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Support uploads bucket is not configured on this deployment.",
            )

        safe_name = _sanitize_support_filename(payload.file_name)
        subject_type = "admin" if isinstance(actor, AdminUser) else "user"
        date_prefix = utcnow().strftime("%Y/%m/%d")
        object_prefix = str(settings.support_uploads_object_prefix or "support").strip().strip("/")
        object_path = (
            f"{object_prefix}/{subject_type}/{actor.id}/{date_prefix}/"
            f"{uuid.uuid4().hex}_{safe_name}"
        )
        expires_in_seconds = max(60, min(int(settings.support_uploads_url_ttl_seconds), 3600))
        client = _build_support_upload_client(settings)
        upload_url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": object_path, "ContentType": payload.content_type},
            ExpiresIn=expires_in_seconds,
        )
        public_url = _build_support_public_url(settings=settings, bucket=bucket, object_path=object_path)
        return {
            "upload": {
                "bucket": bucket,
                "objectPath": object_path,
                "publicUrl": public_url,
                "uploadUrl": upload_url,
                "method": "PUT",
                "requiredHeaders": {"Content-Type": payload.content_type},
                "expiresInSeconds": expires_in_seconds,
                "maxFileBytes": max_file_bytes,
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
                    "attachments": (
                        row.provider_payload.get("attachments", [])
                        if isinstance(row.provider_payload, dict)
                        else []
                    ),
                    "createdAt": row.created_at,
                    "pushDeliveryStatus": row.push_delivery_status,
                }
                for row in rows
            ],
            "pagination": {"limit": limit, "offset": offset, "count": len(rows)},
        }

    async def _process_telegram_webhook(
        payload: dict[str, Any],
        db: Session = Depends(get_db),
        push_provider: PushNotificationService = Depends(get_push_provider),
        secret_header: str | None = None,
        query_secret: str | None = None,
        path_secret: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        configured_secret = str(settings.telegram_support_webhook_secret or "").strip()
        if not configured_secret or not _is_valid_webhook_secret(
            configured_secret=configured_secret,
            header_secret=secret_header,
            query_secret=query_secret,
            path_secret=path_secret,
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Telegram webhook secret")

        message = _extract_telegram_message(payload) if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return {"ok": True, "ignored": "no_message"}

        bot_token = str(settings.telegram_support_bot_token or "").strip()
        text = _extract_message_text_content(message)

        mirrored_attachments: list[dict[str, Any]] = []
        photo_items = message.get("photo") if isinstance(message.get("photo"), list) else []
        if photo_items:
            best_photo = None
            for item in photo_items:
                if not isinstance(item, dict) or not item.get("file_id"):
                    continue
                if best_photo is None or int(item.get("file_size") or 0) >= int(best_photo.get("file_size") or 0):
                    best_photo = item
            if best_photo is not None and bot_token:
                mirrored = await _mirror_telegram_attachment_to_support_bucket(
                    settings=settings,
                    bot_token=bot_token,
                    file_id=str(best_photo.get("file_id")),
                    fallback_name="telegram_photo.jpg",
                    fallback_content_type="image/jpeg",
                )
                if mirrored:
                    mirrored_attachments.append(mirrored)

        document = message.get("document") if isinstance(message.get("document"), dict) else None
        if document and document.get("file_id"):
            mime_type = str(document.get("mime_type") or "").strip().lower()
            if mime_type.startswith("image/") and bot_token:
                mirrored = await _mirror_telegram_attachment_to_support_bucket(
                    settings=settings,
                    bot_token=bot_token,
                    file_id=str(document.get("file_id")),
                    fallback_name=str(document.get("file_name") or "telegram_image"),
                    fallback_content_type=mime_type or None,
                )
                if mirrored:
                    mirrored_attachments.append(mirrored)

        if not text and not mirrored_attachments:
            return {"ok": True, "ignored": "empty_text"}

        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = int(chat.get("id")) if chat.get("id") is not None else TELEGRAM_SUPPORT_CHAT_ID
        telegram_message_id = int(message.get("message_id")) if message.get("message_id") is not None else None
        if telegram_message_id is not None:
            existing = db.scalar(select(TelegramSupportMessage).where(TelegramSupportMessage.telegram_message_id == telegram_message_id))
            if existing is not None:
                return {
                    "ok": True,
                    "duplicate": True,
                    "messageId": existing.id,
                    "userId": existing.user_id,
                    "pushDeliveryStatus": existing.push_delivery_status,
                }

        user_id: str | None = None
        reply_block = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else None
        if reply_block is not None:
            reply_id = int(reply_block.get("message_id")) if reply_block.get("message_id") is not None else None
            if reply_id is not None:
                parent = db.scalar(select(TelegramSupportMessage).where(TelegramSupportMessage.telegram_message_id == reply_id))
                if parent is not None and parent.user_id:
                    user_id = parent.user_id
            if user_id is None:
                reply_text = _extract_message_text_content(reply_block)
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

        if user_id is None:
            recent_thread = db.scalar(
                select(TelegramSupportMessage)
                .where(
                    TelegramSupportMessage.telegram_chat_id == chat_id,
                    TelegramSupportMessage.direction == "user_to_admin",
                    TelegramSupportMessage.user_id.is_not(None),
                )
                .order_by(TelegramSupportMessage.created_at.desc())
                .limit(1)
            )
            if recent_thread is not None and recent_thread.user_id:
                user_id = recent_thread.user_id

        row = TelegramSupportMessage(
            user_id=user_id,
            direction="admin_to_user",
            status="received",
            message_text=text or "[Attachment only]",
            telegram_chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            provider_payload={"telegramWebhook": payload, "attachments": mirrored_attachments},
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            if telegram_message_id is not None:
                existing = db.scalar(
                    select(TelegramSupportMessage).where(TelegramSupportMessage.telegram_message_id == telegram_message_id)
                )
                if existing is not None:
                    return {
                        "ok": True,
                        "duplicate": True,
                        "messageId": existing.id,
                        "userId": existing.user_id,
                        "pushDeliveryStatus": existing.push_delivery_status,
                    }
            raise

        if user_id:
            tokens = db.scalars(
                select(PushDevice.token).where(PushDevice.user_id == user_id, PushDevice.active.is_(True))
            ).all()
            if tokens:
                try:
                    send_result = push_provider.send_push_notification(
                        tokens=tokens,
                        title="Support reply",
                        body=(text or "Support sent an image.")[:2000],
                        data={
                            "type": "support_reply",
                            "supportMessageId": row.id,
                            "attachmentCount": len(mirrored_attachments),
                        },
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
        else:
            row.push_delivery_status = "unmapped"

        row.updated_at = utcnow()
        db.commit()
        return {"ok": True, "messageId": row.id, "userId": user_id, "pushDeliveryStatus": row.push_delivery_status}

    @app.post("/api/v1/support/telegram/webhook")
    async def telegram_webhook(
        payload: dict[str, Any],
        db: Session = Depends(get_db),
        push_provider: PushNotificationService = Depends(get_push_provider),
        secret_header: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
        query_secret: str | None = Query(default=None, alias="secret"),
    ) -> dict[str, Any]:
        return await _process_telegram_webhook(
            payload,
            db,
            push_provider,
            secret_header=secret_header,
            query_secret=query_secret,
        )

    @app.post("/api/v1/support/telegram/webhooks")
    async def telegram_webhooks_alias(
        payload: dict[str, Any],
        db: Session = Depends(get_db),
        push_provider: PushNotificationService = Depends(get_push_provider),
        secret_header: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
        query_secret: str | None = Query(default=None, alias="secret"),
    ) -> dict[str, Any]:
        return await _process_telegram_webhook(
            payload,
            db,
            push_provider,
            secret_header=secret_header,
            query_secret=query_secret,
        )

    @app.post("/api/v1/support/telegram/webhook/{path_secret}")
    async def telegram_webhook_with_secret(
        path_secret: str,
        payload: dict[str, Any],
        db: Session = Depends(get_db),
        push_provider: PushNotificationService = Depends(get_push_provider),
        secret_header: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> dict[str, Any]:
        return await _process_telegram_webhook(
            payload,
            db,
            push_provider,
            secret_header=secret_header,
            path_secret=path_secret,
        )

    @app.post("/api/v1/support/telegram/webhooks/{path_secret}")
    async def telegram_webhooks_alias_with_secret(
        path_secret: str,
        payload: dict[str, Any],
        db: Session = Depends(get_db),
        push_provider: PushNotificationService = Depends(get_push_provider),
        secret_header: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> dict[str, Any]:
        return await _process_telegram_webhook(
            payload,
            db,
            push_provider,
            secret_header=secret_header,
            path_secret=path_secret,
        )
