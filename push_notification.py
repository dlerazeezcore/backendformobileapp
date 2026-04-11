from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Callable, Iterable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from auth import decode_access_token, extract_bearer_token, get_token_claims, require_active_subject
from config import get_settings
from supabase_store import AdminUser, AppUser, PushDevice, PushNotification, SupabaseStore, utcnow

ALLOWED_PUSH_AUDIENCES = {"all", "authenticated", "loyalty", "active_esim", "admins", "all_devices"}
ALLOWED_APP_UPDATE_AUDIENCES = {"all", "authenticated", "loyalty", "active_esim", "all_devices"}
logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except Exception:  # pragma: no cover - runtime dependency handling
    firebase_admin = None
    credentials = None
    messaging = None


class PushNotificationService:
    def __init__(
        self,
        *,
        service_account_file: str | None = None,
        service_account_json: str | None = None,
        default_channel_id: str = "general",
    ) -> None:
        self.service_account_file = (service_account_file or "").strip()
        self.service_account_json = (service_account_json or "").strip()
        self.default_channel_id = default_channel_id.strip() or "general"

    def is_configured(self) -> bool:
        return bool(firebase_admin and (self.service_account_file or self.service_account_json))

    def _load_credential_object(self) -> Any:
        if credentials is None:
            raise RuntimeError("firebase-admin is not installed in the runtime.")
        if self.service_account_file:
            return credentials.Certificate(self.service_account_file)
        if not self.service_account_json:
            raise RuntimeError("Firebase service account credentials are not configured.")
        try:
            payload = json.loads(self.service_account_json)
        except Exception as exc:  # pragma: no cover - invalid env config
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
        return credentials.Certificate(payload)

    @lru_cache(maxsize=1)
    def _get_firebase_app(self):
        if firebase_admin is None or messaging is None:
            raise RuntimeError("firebase-admin is not installed in the runtime.")
        try:
            return firebase_admin.get_app()
        except ValueError:
            return firebase_admin.initialize_app(self._load_credential_object())

    @staticmethod
    def _normalize_data(payload: dict[str, Any] | None) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in (payload or {}).items():
            name = str(key or "").strip()
            if not name or value is None:
                continue
            normalized[name] = str(value)
        return normalized

    @staticmethod
    def _normalize_tokens(tokens: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            value = str(token or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @staticmethod
    def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
        for index in range(0, len(values), size):
            yield values[index : index + size]

    def send_push_notification(
        self,
        *,
        tokens: Iterable[str],
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        channel_id: str | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        normalized_tokens = self._normalize_tokens(tokens)
        if not normalized_tokens:
            return {"successCount": 0, "failureCount": 0, "invalidTokens": []}
        if not self.is_configured():
            raise RuntimeError("Firebase push provider is not configured.")

        app = self._get_firebase_app()
        assert messaging is not None
        payload = self._normalize_data(data)
        resolved_channel = (channel_id or self.default_channel_id).strip() or self.default_channel_id

        success_count = 0
        failure_count = 0
        invalid_tokens: list[str] = []

        for batch in self._chunked(normalized_tokens, 500):
            message = messaging.MulticastMessage(
                tokens=batch,
                notification=messaging.Notification(
                    title=str(title or "").strip(),
                    body=str(body or "").strip(),
                    image=str(image or "").strip() or None,
                ),
                data=payload,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(channel_id=resolved_channel),
                ),
                apns=messaging.APNSConfig(
                    headers={
                        "apns-priority": "10",
                        "apns-push-type": "alert",
                    },
                    payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
                ),
            )
            response = messaging.send_each_for_multicast(message, app=app)
            success_count += int(response.success_count)
            failure_count += int(response.failure_count)
            for index, item in enumerate(response.responses):
                if item.success:
                    continue
                error_text = str(item.exception or "").lower()
                if "not registered" in error_text or "invalid registration token" in error_text:
                    invalid_tokens.append(batch[index])

        return {
            "successCount": success_count,
            "failureCount": failure_count,
            "invalidTokens": self._normalize_tokens(invalid_tokens),
        }


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class RegisterPushDevicePayload(Model):
    token: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    device_id: str | None = Field(default=None, alias="deviceId")
    app_version: str | None = Field(default=None, alias="appVersion")
    locale: str | None = None
    timezone_name: str | None = Field(default=None, alias="timezone")
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


class UnregisterPushDevicePayload(Model):
    token: str | None = None
    device_id: str | None = Field(default=None, alias="deviceId")

    @model_validator(mode="after")
    def validate_selector(self) -> "UnregisterPushDevicePayload":
        if not self.token and not self.device_id:
            raise ValueError("Either token or deviceId is required")
        return self


class SendPushNotificationPayload(Model):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)
    audience: str | None = None
    user_ids: list[str] = Field(default_factory=list, alias="userIds")
    tokens: list[str] = Field(default_factory=list)
    send_to_all_active: bool = Field(default=False, alias="sendToAllActive")
    channel_id: str | None = Field(default=None, alias="channelId")
    image: str | None = None
    dry_run: bool = Field(default=False, alias="dryRun")

    @model_validator(mode="after")
    def validate_targets(self) -> "SendPushNotificationPayload":
        normalized_audience = str(self.audience or "").strip().lower()
        if normalized_audience:
            if normalized_audience not in ALLOWED_PUSH_AUDIENCES:
                raise ValueError(
                    "audience must be one of: all, authenticated, loyalty, active_esim, admins, all_devices"
                )
            self.audience = normalized_audience
        has_any_target = bool(self.send_to_all_active or self.user_ids or self.tokens or normalized_audience)
        if not has_any_target:
            raise ValueError(
                "Provide at least one target: audience, userIds, tokens, or sendToAllActive=true"
            )
        return self


class SendAppUpdateNotificationPayload(Model):
    app_store_url: str = Field(alias="appStoreUrl", min_length=1)
    play_store_url: str = Field(alias="playStoreUrl", min_length=1)
    title: str = Field(default="Update Available", min_length=1, max_length=255)
    body: str = Field(
        default="A new version is available. Please update to continue with the best experience.",
        min_length=1,
        max_length=2000,
    )
    audience: str = Field(default="all")
    data: dict[str, Any] = Field(default_factory=dict)
    channel_id: str | None = Field(default=None, alias="channelId")
    image: str | None = None
    dry_run: bool = Field(default=False, alias="dryRun")

    @model_validator(mode="after")
    def validate_payload(self) -> "SendAppUpdateNotificationPayload":
        normalized_audience = str(self.audience or "").strip().lower()
        if normalized_audience not in ALLOWED_APP_UPDATE_AUDIENCES:
            raise ValueError("audience must be one of: all, authenticated, loyalty, active_esim, all_devices")
        self.audience = normalized_audience
        app_store_url = str(self.app_store_url or "").strip()
        play_store_url = str(self.play_store_url or "").strip()
        if not app_store_url.lower().startswith(("https://", "http://")):
            raise ValueError("appStoreUrl must be a valid URL.")
        if not play_store_url.lower().startswith(("https://", "http://")):
            raise ValueError("playStoreUrl must be a valid URL.")
        self.app_store_url = app_store_url
        self.play_store_url = play_store_url
        return self


def _serialize_push_device(row: PushDevice) -> dict[str, Any]:
    subject_type = "user"
    if isinstance(row.custom_fields, dict):
        subject_type = str(row.custom_fields.get("subjectType") or "user").strip().lower() or "user"
    return {
        "id": row.id,
        "userId": row.user_id,
        "adminUserId": row.admin_user_id,
        "subjectType": subject_type,
        "token": row.token,
        "platform": row.platform,
        "deviceId": row.device_id,
        "appVersion": row.app_version,
        "locale": row.locale,
        "timezone": row.timezone_name,
        "active": row.active,
        "lastSeenAt": row.last_seen_at,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "customFields": row.custom_fields,
    }


def _serialize_push_notification(row: PushNotification) -> dict[str, Any]:
    return {
        "id": row.id,
        "recipientScope": row.recipient_scope,
        "title": row.title,
        "body": row.body,
        "channelId": row.channel_id,
        "imageUrl": row.image_url,
        "status": row.status,
        "provider": row.provider,
        "successCount": row.success_count,
        "failureCount": row.failure_count,
        "invalidTokenCount": row.invalid_token_count,
        "invalidTokens": row.invalid_tokens,
        "errorMessage": row.error_message,
        "sentByAdminId": row.sent_by_admin_id,
        "targetUserIds": row.target_user_ids,
        "sentAt": row.sent_at,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }

def _serialize_last_campaign(row: PushNotification | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "title": row.title,
        "status": row.status,
        "successCount": row.success_count,
        "failureCount": row.failure_count,
        "invalidTokenCount": row.invalid_token_count,
        "sentAt": row.sent_at,
        "createdAt": row.created_at,
    }


def register_push_notification_routes(
    app: FastAPI,
    get_push_provider: Callable[..., PushNotificationService],
    get_db: Callable[..., Any],
) -> None:
    async def _require_user_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AppUser:
        row = require_active_subject(db, claims=claims, subject_type="user")
        assert isinstance(row, AppUser)
        return row

    def _resolve_optional_push_actor(
        *,
        db: Session,
        authorization: str | None,
    ) -> tuple[str, AppUser | AdminUser | None]:
        token = extract_bearer_token(authorization)
        if token is None:
            return "anonymous", None
        claims = decode_access_token(token, secret_key=get_settings().auth_secret_key)
        row = require_active_subject(db, claims=claims)
        assert isinstance(row, (AppUser, AdminUser))
        subject_type = str(claims.get("typ") or "").strip().lower()
        if subject_type not in {"user", "admin"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unsupported auth subject type")
        return subject_type, row

    async def _require_admin_sender(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser:
        row = require_active_subject(db, claims=claims, subject_type="admin")
        assert isinstance(row, AdminUser)
        if not row.can_send_push and row.role not in {"super_admin", "owner"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin user cannot send push notifications")
        return row

    @app.post("/api/v1/push-notifications/devices/register")
    async def register_push_device(
        payload: RegisterPushDevicePayload,
        db: Session = Depends(get_db),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        store = SupabaseStore(db)
        actor = _resolve_optional_push_actor(db=db, authorization=authorization)
        subject_type, subject_row = actor
        user_id = subject_row.id if isinstance(subject_row, AppUser) else None
        admin_user_id = subject_row.id if isinstance(subject_row, AdminUser) else None
        custom_fields = dict(payload.custom_fields or {})
        custom_fields["subjectType"] = subject_type
        try:
            row = store.upsert_push_device(
                user_id=user_id,
                admin_user_id=admin_user_id,
                token=payload.token,
                platform=payload.platform,
                device_id=payload.device_id,
                app_version=payload.app_version,
                locale=payload.locale,
                timezone_name=payload.timezone_name,
                custom_fields=custom_fields,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        db.commit()
        db.refresh(row)
        return {"device": _serialize_push_device(row)}

    @app.post("/api/v1/push-notifications/devices/unregister")
    async def unregister_push_device(
        payload: UnregisterPushDevicePayload,
        db: Session = Depends(get_db),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _, subject_row = _resolve_optional_push_actor(db=db, authorization=authorization)
        user_id = subject_row.id if isinstance(subject_row, AppUser) else None
        admin_user_id = subject_row.id if isinstance(subject_row, AdminUser) else None
        try:
            store = SupabaseStore(db)
            if subject_row is None:
                affected = store.deactivate_push_devices_public(token=payload.token, device_id=payload.device_id)
            else:
                affected = store.deactivate_push_devices(
                    user_id=user_id,
                    admin_user_id=admin_user_id,
                    token=payload.token,
                    device_id=payload.device_id,
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        db.commit()
        return {"updated": affected}

    @app.get("/api/v1/push-notifications/devices")
    async def list_my_push_devices(
        active_only: bool = Query(default=True, alias="activeOnly"),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: AppUser = Depends(_require_user_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_push_devices_for_user(
            user_id=user.id,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )
        return {
            "devices": [_serialize_push_device(item) for item in rows],
            "pagination": {"limit": limit, "offset": offset, "count": len(rows)},
        }

    @app.post("/api/esim-app/push/admin/send", response_model=None)
    @app.post("/api/v1/admin/push-notifications/send", response_model=None)
    async def send_push_notification(
        payload: SendPushNotificationPayload,
        provider: PushNotificationService = Depends(get_push_provider),
        db: Session = Depends(get_db),
        admin_user: AdminUser = Depends(_require_admin_sender),
    ) -> Any:
        store = SupabaseStore(db)
        requested_user_ids = PushNotificationService._normalize_tokens(
            [str(item).strip() for item in payload.user_ids if str(item).strip()]
        )
        direct_tokens = [str(item).strip() for item in payload.tokens if str(item).strip()]
        audience = str(payload.audience or "").strip().lower()
        if payload.send_to_all_active and not audience:
            audience = "all"

        audience_tokens: list[str] = []
        audience_user_ids: list[str] = []
        recipient_scope = "direct_tokens"
        if audience:
            audience_tokens, audience_user_ids = store.list_push_tokens_for_audience(
                audience=audience,
                limit=20000,
            )
            recipient_scope = f"audience:{audience}"

        store_tokens: list[str] = []
        if requested_user_ids:
            recipient_scope = "users"
            store_tokens.extend(store.list_push_tokens(user_ids=requested_user_ids, active_only=True))
        if requested_user_ids and audience:
            recipient_scope = "mixed"
        if requested_user_ids and direct_tokens:
            recipient_scope = "mixed"
        if direct_tokens and audience:
            recipient_scope = "mixed"

        logger.info(
            "push.send.pre_resolution audience=%s send_to_all_active=%s normalized_user_ids=%s tokens_count=%s recipient_scope=%s",
            payload.audience,
            payload.send_to_all_active,
            requested_user_ids,
            len(direct_tokens),
            recipient_scope,
        )

        deduped_tokens = PushNotificationService._normalize_tokens(
            [*direct_tokens, *audience_tokens, *store_tokens]
        )
        merged_target_user_ids = PushNotificationService._normalize_tokens(
            [*audience_user_ids, *requested_user_ids]
        )
        requested_user_ids_count = len(requested_user_ids)
        audience_user_ids_count = len(audience_user_ids)
        store_tokens_count = len(store_tokens)
        deduped_tokens_count = len(deduped_tokens)
        merged_target_user_ids_count = len(merged_target_user_ids)
        debug_payload = {
            "recipientScope": recipient_scope,
            "requestedAudience": audience or None,
            "requestedUserIdsCount": requested_user_ids_count,
            "matchedAudienceUserIdsCount": audience_user_ids_count,
            "matchedDirectUserTokensCount": store_tokens_count,
            "totalDedupedTokens": deduped_tokens_count,
        }
        logger.info(
            "push.send.post_resolution requested_user_ids_count=%s audience_user_ids_count=%s store_tokens_count=%s deduped_tokens_count=%s merged_target_user_ids_count=%s",
            requested_user_ids_count,
            audience_user_ids_count,
            store_tokens_count,
            deduped_tokens_count,
            merged_target_user_ids_count,
        )

        if not deduped_tokens:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "success": False,
                    "errorCode": "NO_ELIGIBLE_PUSH_TOKENS",
                    "message": "No eligible push tokens found for the selected targets.",
                    "requestedAudience": audience or None,
                    "requestedUserIdsCount": requested_user_ids_count,
                    "requestedTokensCount": len(direct_tokens),
                    "matchedAudienceUserIdsCount": audience_user_ids_count,
                    "matchedAudienceTokensCount": len(audience_tokens),
                    "matchedDirectUserTokensCount": store_tokens_count,
                    "totalDedupedTokens": deduped_tokens_count,
                    "activeUserTokens": store.count_active_push_tokens(subject_type="user"),
                    "activeAdminTokens": store.count_active_push_tokens(subject_type="admin"),
                    "eligibleTokensForRequestedAudience": deduped_tokens_count,
                    "data": {"debug": debug_payload},
                },
            )

        notification = store.create_push_notification(
            recipient_scope=recipient_scope,
            title=payload.title,
            body=payload.body,
            provider="firebase_fcm",
            channel_id=payload.channel_id or provider.default_channel_id,
            image_url=payload.image,
            sent_by_admin_id=admin_user.id,
            target_user_ids=merged_target_user_ids,
            data_payload=payload.data,
            provider_response={
                "requestedTokens": len(deduped_tokens),
                "audience": audience or None,
                "requestedUserIds": requested_user_ids,
                "audienceUserIdsCount": audience_user_ids_count,
            },
            status="queued",
        )
        db.flush()

        if payload.dry_run:
            store.finalize_push_notification(
                row=notification,
                status="dry_run",
                success_count=len(deduped_tokens),
                failure_count=0,
                invalid_tokens=[],
                provider_response={
                    "dryRun": True,
                    "requestedTokens": len(deduped_tokens),
                    "audience": audience or None,
                },
            )
            db.commit()
            db.refresh(notification)
            return {
                "notification": _serialize_push_notification(notification),
                "delivery": {"requestedTokens": len(deduped_tokens), "dryRun": True},
                "data": {"debug": debug_payload},
            }

        if not provider.is_configured():
            store.finalize_push_notification(
                row=notification,
                status="failed",
                error_message="Firebase push provider is not configured.",
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Push notification provider is not configured on this deployment.",
            )

        try:
            result = provider.send_push_notification(
                tokens=deduped_tokens,
                title=payload.title,
                body=payload.body,
                data=payload.data,
                channel_id=payload.channel_id,
                image=payload.image,
            )
        except Exception as exc:
            store.finalize_push_notification(
                row=notification,
                status="failed",
                error_message=str(exc),
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Push provider request failed: {exc}",
            ) from exc

        invalid_tokens = PushNotificationService._normalize_tokens(result.get("invalidTokens", []))
        if invalid_tokens:
            store.deactivate_push_devices_by_tokens(invalid_tokens)

        success_count = int(result.get("successCount", 0))
        failure_count = int(result.get("failureCount", 0))
        if failure_count == 0:
            delivery_status = "sent"
        elif success_count > 0:
            delivery_status = "partial"
        else:
            delivery_status = "failed"

        store.finalize_push_notification(
            row=notification,
            status=delivery_status,
            success_count=success_count,
            failure_count=failure_count,
            invalid_tokens=invalid_tokens,
            provider_response=result,
            sent_at=utcnow(),
        )
        db.commit()
        db.refresh(notification)
        return {
            "notification": _serialize_push_notification(notification),
            "delivery": {
                "requestedTokens": len(deduped_tokens),
                "successCount": success_count,
                "failureCount": failure_count,
                "invalidTokenCount": len(invalid_tokens),
                "invalidTokens": invalid_tokens,
            },
            "data": {"debug": debug_payload},
        }

    @app.post("/api/esim-app/push/admin/send-app-update", response_model=None)
    @app.post("/api/v1/admin/push-notifications/send-app-update", response_model=None)
    async def send_app_update_notification(
        payload: SendAppUpdateNotificationPayload,
        provider: PushNotificationService = Depends(get_push_provider),
        db: Session = Depends(get_db),
        admin_user: AdminUser = Depends(_require_admin_sender),
    ) -> Any:
        merged_data = dict(payload.data or {})
        merged_data.update(
            {
                "type": "app_update",
                "action": "open_store_update",
                "appStoreUrl": payload.app_store_url,
                "playStoreUrl": payload.play_store_url,
            }
        )
        send_payload = SendPushNotificationPayload(
            title=payload.title,
            body=payload.body,
            data=merged_data,
            audience=payload.audience,
            channel_id=payload.channel_id,
            image=payload.image,
            dry_run=payload.dry_run,
        )
        return await send_push_notification(
            payload=send_payload,
            provider=provider,
            db=db,
            admin_user=admin_user,
        )

    @app.get("/api/v1/admin/push-notifications")
    async def list_push_notifications(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_sender),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_push_notifications(limit=limit, offset=offset)
        return {
            "notifications": [_serialize_push_notification(item) for item in rows],
            "pagination": {"limit": limit, "offset": offset, "count": len(rows)},
        }

    @app.get("/api/v1/admin/push-notifications/summary")
    async def get_push_notifications_summary(
        provider: PushNotificationService = Depends(get_push_provider),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_sender),
    ) -> dict[str, Any]:
        summary = SupabaseStore(db).get_push_notification_summary()
        return {
            "providerConfigured": provider.is_configured(),
            "totalDevices": summary["totalDevices"],
            "enabledDevices": summary["enabledDevices"],
            "authenticatedDevices": summary["authenticatedDevices"],
            "loyaltyDevices": summary["loyaltyDevices"],
            "activeEsimDevices": summary["activeEsimDevices"],
            "iosDevices": summary["iosDevices"],
            "androidDevices": summary["androidDevices"],
            "lastCampaign": _serialize_last_campaign(summary["lastCampaign"]),
        }

    @app.get("/api/v1/admin/push-notifications/diagnostics")
    async def get_push_notifications_diagnostics(
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_sender),
    ) -> dict[str, Any]:
        return SupabaseStore(db).get_push_devices_diagnostics(sample_limit=10)
