from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from typing import Any, Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from auth import create_access_token
from config import get_settings
from push_notification import PushNotificationService, register_push_notification_routes
from supabase_store import AdminUser, AppUser, Base, ESimProfile, PushDevice, PushNotification, SupabaseStore


class _FakePushProvider(PushNotificationService):
    def __init__(self, *, configured: bool = True) -> None:
        super().__init__(default_channel_id="general")
        self._configured = configured
        self.sent_payloads: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return self._configured

    def send_push_notification(
        self,
        *,
        tokens: list[str],
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        channel_id: str | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        self.sent_payloads.append(
            {
                "tokens": tokens,
                "title": title,
                "body": body,
                "data": data or {},
                "channelId": channel_id,
                "image": image,
            }
        )
        if "bad-token" in tokens:
            return {
                "successCount": max(0, len(tokens) - 1),
                "failureCount": 1,
                "invalidTokens": ["bad-token"],
            }
        return {
            "successCount": len(tokens),
            "failureCount": 0,
            "invalidTokens": [],
        }


class PushNotificationRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-access-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret-key"
        get_settings.cache_clear()

        temp_db = tempfile.NamedTemporaryFile(prefix="push_test_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.engine = create_engine(
            f"sqlite+pysqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

        self.provider = _FakePushProvider(configured=True)
        app = FastAPI()

        def _get_provider() -> _FakePushProvider:
            return self.provider

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        register_push_notification_routes(app, _get_provider, _get_db)
        self.client = TestClient(app)

        self.user_id = str(uuid.uuid4())
        self.loyalty_user_id = str(uuid.uuid4())
        self.active_esim_user_id = str(uuid.uuid4())
        self.blocked_user_id = str(uuid.uuid4())
        self.admin_id = str(uuid.uuid4())
        self.limited_admin_id = str(uuid.uuid4())
        with self.session_factory() as session:
            session.add(
                AppUser(
                    id=self.user_id,
                    phone="+9647700000100",
                    name="Push User",
                    status="active",
                )
            )
            session.add(
                AppUser(
                    id=self.loyalty_user_id,
                    phone="+9647700000103",
                    name="Loyalty User",
                    status="active",
                    is_loyalty=True,
                )
            )
            session.add(
                AppUser(
                    id=self.active_esim_user_id,
                    phone="+9647700000104",
                    name="eSIM User",
                    status="active",
                )
            )
            session.add(
                AppUser(
                    id=self.blocked_user_id,
                    phone="+9647700000105",
                    name="Blocked User",
                    status="blocked",
                )
            )
            session.add(
                ESimProfile(
                    user_id=self.active_esim_user_id,
                    app_status="ACTIVE",
                    installed=True,
                )
            )
            session.add(
                AdminUser(
                    id=self.admin_id,
                    phone="+9647700000101",
                    name="Push Admin",
                    status="active",
                    role="admin",
                    can_send_push=True,
                )
            )
            session.add(
                AdminUser(
                    id=self.limited_admin_id,
                    phone="+9647700000102",
                    name="Limited Admin",
                    status="active",
                    role="admin",
                    can_send_push=False,
                )
            )
            SupabaseStore(session).upsert_push_device(
                user_id=self.blocked_user_id,
                token="blocked-token",
                platform="android",
                device_id="blocked-device",
            )
            session.commit()

        self.user_headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject_id=self.user_id,
                phone="+9647700000100",
                subject_type="user",
                secret_key="test-auth-secret",
                ttl_seconds=3600,
            )
        }
        self.admin_headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject_id=self.admin_id,
                phone="+9647700000101",
                subject_type="admin",
                secret_key="test-auth-secret",
                ttl_seconds=3600,
            )
        }
        self.loyalty_user_headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject_id=self.loyalty_user_id,
                phone="+9647700000103",
                subject_type="user",
                secret_key="test-auth-secret",
                ttl_seconds=3600,
            )
        }
        self.active_esim_user_headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject_id=self.active_esim_user_id,
                phone="+9647700000104",
                subject_type="user",
                secret_key="test-auth-secret",
                ttl_seconds=3600,
            )
        }
        self.limited_admin_headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject_id=self.limited_admin_id,
                phone="+9647700000102",
                subject_type="admin",
                secret_key="test-auth-secret",
                ttl_seconds=3600,
            )
        }

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def test_register_list_unregister_push_device(self) -> None:
        register_response = self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={
                "token": "token-1",
                "platform": "android",
                "deviceId": "device-a",
                "appVersion": "1.0.0",
                "customFields": {"build": "100"},
            },
            headers=self.user_headers,
        )
        self.assertEqual(register_response.status_code, 200)
        self.assertEqual(register_response.json()["device"]["token"], "token-1")
        self.assertEqual(register_response.json()["device"]["active"], True)

        list_response = self.client.get(
            "/api/v1/push-notifications/devices",
            headers=self.user_headers,
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json().get("devices", [])), 1)

        unregister_response = self.client.post(
            "/api/v1/push-notifications/devices/unregister",
            json={"token": "token-1"},
            headers=self.user_headers,
        )
        self.assertEqual(unregister_response.status_code, 200)
        self.assertEqual(unregister_response.json().get("updated"), 1)

        list_after_response = self.client.get(
            "/api/v1/push-notifications/devices?activeOnly=true",
            headers=self.user_headers,
        )
        self.assertEqual(list_after_response.status_code, 200)
        self.assertEqual(len(list_after_response.json().get("devices", [])), 0)

    def test_register_invalid_platform_returns_422(self) -> None:
        response = self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "token-2", "platform": "desktop"},
            headers=self.user_headers,
        )
        self.assertEqual(response.status_code, 422)

    def test_admin_token_register_and_unregister_push_device(self) -> None:
        register_response = self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={
                "token": "admin-token-1",
                "platform": "ios",
                "deviceId": "admin-device-1",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(register_response.status_code, 200)
        device_payload = register_response.json()["device"]
        self.assertEqual(device_payload["token"], "admin-token-1")
        self.assertEqual(device_payload["customFields"]["subjectType"], "admin")

        unregister_response = self.client.post(
            "/api/v1/push-notifications/devices/unregister",
            json={"token": "admin-token-1"},
            headers=self.admin_headers,
        )
        self.assertEqual(unregister_response.status_code, 200)
        self.assertEqual(unregister_response.json().get("updated"), 1)

        with self.session_factory() as session:
            row = session.scalar(select(PushDevice).where(PushDevice.token == "admin-token-1"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.active, False)
            self.assertEqual(row.admin_user_id, self.admin_id)
            self.assertIsNone(row.user_id)

    def test_anonymous_register_and_unregister_push_device(self) -> None:
        register_response = self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={
                "token": "anon-token-1",
                "platform": "android",
                "deviceId": "anon-device-1",
            },
        )
        self.assertEqual(register_response.status_code, 200)
        device_payload = register_response.json()["device"]
        self.assertEqual(device_payload["token"], "anon-token-1")
        self.assertEqual(device_payload["customFields"]["subjectType"], "anonymous")
        self.assertIsNone(device_payload["userId"])
        self.assertIsNone(device_payload["adminUserId"])

        unregister_response = self.client.post(
            "/api/v1/push-notifications/devices/unregister",
            json={"token": "anon-token-1"},
        )
        self.assertEqual(unregister_response.status_code, 200)
        self.assertEqual(unregister_response.json().get("updated"), 1)

        with self.session_factory() as session:
            row = session.scalar(select(PushDevice).where(PushDevice.token == "anon-token-1"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.active, False)
            self.assertIsNone(row.user_id)
            self.assertIsNone(row.admin_user_id)

    def test_send_to_all_active_includes_admin_registered_devices(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "user-token-only", "platform": "android"},
            headers=self.user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "admin-token-only", "platform": "ios"},
            headers=self.admin_headers,
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "All active users",
                "body": "User audience only.",
                "sendToAllActive": True,
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # Baseline test fixture already has one active blocked-user token.
        # "all" now truly means all active devices, including admin-owned tokens.
        self.assertEqual(payload["delivery"]["requestedTokens"], 3)
        self.assertEqual(payload["delivery"]["successCount"], 3)

    def test_send_to_all_active_includes_anonymous_devices(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "anon-token-only", "platform": "android"},
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "All active users",
                "body": "Includes anonymous devices.",
                "sendToAllActive": True,
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # Baseline includes blocked-user token + anonymous token.
        self.assertEqual(payload["delivery"]["requestedTokens"], 2)
        self.assertEqual(payload["delivery"]["successCount"], 2)

    def test_admin_send_with_audience_admins(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "admin-token-only", "platform": "ios"},
            headers=self.admin_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "user-token-only", "platform": "android"},
            headers=self.user_headers,
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Admin test",
                "body": "Admin-only audience delivery.",
                "audience": "admins",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["delivery"]["requestedTokens"], 1)
        self.assertEqual(payload["delivery"]["successCount"], 1)
        self.assertEqual(payload["notification"]["recipientScope"], "audience:admins")

    def test_admin_send_with_audience_all_devices(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "admin-token-only", "platform": "ios"},
            headers=self.admin_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "user-token-only", "platform": "android"},
            headers=self.user_headers,
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "All devices test",
                "body": "Both admin and user devices.",
                "audience": "all_devices",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # Baseline includes blocked-user token + user-token-only + admin-token-only.
        self.assertEqual(payload["delivery"]["requestedTokens"], 3)
        self.assertEqual(payload["delivery"]["successCount"], 3)
        self.assertEqual(payload["notification"]["recipientScope"], "audience:all_devices")

    def test_admin_send_app_update_notification(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "anon-token-update", "platform": "ios"},
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send-app-update",
            json={
                "title": "Update Tulip",
                "body": "A new update is available.",
                "appStoreUrl": "https://apps.apple.com/app/id000000000",
                "playStoreUrl": "https://play.google.com/store/apps/details?id=com.tulip.app",
                "iosExternalUrl": "https://example.com/ios-external",
                "androidExternalUrl": "https://example.com/android-external",
                "iosUrl": "tulip://ios-update",
                "androidUrl": "tulip://android-update",
                "audience": "all",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["delivery"]["requestedTokens"], 1)
        self.assertEqual(payload["notification"]["recipientScope"], "audience:all")
        self.assertEqual(len(self.provider.sent_payloads), 1)
        sent_data = self.provider.sent_payloads[0]["data"]
        self.assertEqual(sent_data.get("kind"), "app_update")
        self.assertEqual(sent_data.get("type"), "app_update")
        self.assertEqual(sent_data.get("notificationType"), "app_update")
        self.assertEqual(sent_data.get("action"), "open_store_update")
        self.assertEqual(sent_data.get("appStoreUrl"), "https://apps.apple.com/app/id000000000")
        self.assertEqual(
            sent_data.get("playStoreUrl"),
            "https://play.google.com/store/apps/details?id=com.tulip.app",
        )
        self.assertEqual(sent_data.get("iosExternalUrl"), "https://example.com/ios-external")
        self.assertEqual(sent_data.get("androidExternalUrl"), "https://example.com/android-external")
        self.assertEqual(sent_data.get("iosUrl"), "tulip://ios-update")
        self.assertEqual(sent_data.get("androidUrl"), "tulip://android-update")

    def test_admin_send_generic_app_update_preserves_top_level_fields(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "ios-generic-update", "platform": "ios"},
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Update now",
                "body": "Important app update.",
                "audience": "all",
                "kind": "app_update",
                "type": "app_update",
                "notificationType": "app_update",
                "appStoreUrl": "https://apps.apple.com/app/id111111111",
                "playStoreUrl": "https://play.google.com/store/apps/details?id=com.tulip.generic",
                "iosExternalUrl": "https://example.com/generic-ios-external",
                "androidExternalUrl": "https://example.com/generic-android-external",
                "iosUrl": "tulip://generic-ios-update",
                "androidUrl": "tulip://generic-android-update",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.provider.sent_payloads), 1)
        sent_data = self.provider.sent_payloads[0]["data"]
        self.assertEqual(sent_data.get("kind"), "app_update")
        self.assertEqual(sent_data.get("type"), "app_update")
        self.assertEqual(sent_data.get("notificationType"), "app_update")
        self.assertEqual(sent_data.get("appStoreUrl"), "https://apps.apple.com/app/id111111111")
        self.assertEqual(
            sent_data.get("playStoreUrl"),
            "https://play.google.com/store/apps/details?id=com.tulip.generic",
        )
        self.assertEqual(sent_data.get("iosExternalUrl"), "https://example.com/generic-ios-external")
        self.assertEqual(sent_data.get("androidExternalUrl"), "https://example.com/generic-android-external")
        self.assertEqual(sent_data.get("iosUrl"), "tulip://generic-ios-update")
        self.assertEqual(sent_data.get("androidUrl"), "tulip://generic-android-update")

    def test_inactive_token_error_detection_handles_requested_entity_not_found(self) -> None:
        self.assertEqual(
            PushNotificationService._is_inactive_token_error(RuntimeError("Requested entity was not found.")),
            True,
        )
        self.assertEqual(
            PushNotificationService._is_inactive_token_error(RuntimeError("Sender ID mismatch.")),
            True,
        )
        self.assertEqual(
            PushNotificationService._is_inactive_token_error(RuntimeError("Temporarily unavailable.")),
            False,
        )

    def test_no_eligible_tokens_returns_diagnostics(self) -> None:
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Admin dry",
                "body": "No admin tokens yet.",
                "audience": "admins",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload.get("errorCode"), "NO_ELIGIBLE_PUSH_TOKENS")
        self.assertEqual(payload.get("requestedAudience"), "admins")
        self.assertEqual(payload.get("requestedUserIdsCount"), 0)
        self.assertEqual(payload.get("requestedTokensCount"), 0)
        self.assertEqual(payload.get("matchedAudienceUserIdsCount"), 0)
        self.assertEqual(payload.get("matchedAudienceTokensCount"), 0)
        self.assertEqual(payload.get("matchedDirectUserTokensCount"), 0)
        self.assertEqual(payload.get("totalDedupedTokens"), 0)
        self.assertEqual(payload.get("activeUserTokens"), 1)
        self.assertEqual(payload.get("activeAdminTokens"), 0)
        self.assertEqual(payload.get("eligibleTokensForRequestedAudience"), 0)

    def test_legacy_admin_send_alias_route(self) -> None:
        response = self.client.post(
            "/api/esim-app/push/admin/send",
            json={
                "title": "Legacy route test",
                "body": "No admin tokens yet",
                "audience": "admins",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload.get("errorCode"), "NO_ELIGIBLE_PUSH_TOKENS")
        self.assertEqual(payload.get("requestedAudience"), "admins")

    def test_admin_send_with_audience_loyalty(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "normal-token", "platform": "android"},
            headers=self.user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "loyalty-token", "platform": "ios"},
            headers=self.loyalty_user_headers,
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Loyalty Offer",
                "body": "Special offer for loyalty users.",
                "audience": "loyalty",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["delivery"]["requestedTokens"], 1)
        self.assertEqual(payload["delivery"]["successCount"], 1)
        self.assertEqual(payload["notification"]["recipientScope"], "audience:loyalty")

    def test_admin_send_with_audience_active_esim(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "normal-token", "platform": "android"},
            headers=self.user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "esim-token", "platform": "android"},
            headers=self.active_esim_user_headers,
        )
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "eSIM Alert",
                "body": "Usage reminder for active eSIM users.",
                "audience": "active_esim",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["delivery"]["requestedTokens"], 1)
        self.assertEqual(payload["delivery"]["successCount"], 1)
        self.assertEqual(payload["notification"]["recipientScope"], "audience:active_esim")

    def test_admin_send_records_delivery_and_deactivates_invalid_tokens(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "good-token", "platform": "android"},
            headers=self.user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "bad-token", "platform": "android"},
            headers=self.user_headers,
        )

        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Order Update",
                "body": "Your eSIM is active.",
                "userIds": [self.user_id],
                "data": {"type": "order_status"},
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["notification"]["status"], "partial")
        self.assertEqual(payload["delivery"]["successCount"], 1)
        self.assertEqual(payload["delivery"]["failureCount"], 1)
        self.assertEqual(payload["delivery"]["invalidTokenCount"], 1)

        with self.session_factory() as session:
            notifications = session.scalars(select(PushNotification)).all()
            self.assertEqual(len(notifications), 1)
            devices = session.scalars(select(PushDevice).where(PushDevice.token == "bad-token")).all()
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0].active, False)

    def test_admin_send_user_ids_targets_only_requested_users_tokens(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "target-user-token", "platform": "android"},
            headers=self.user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "other-user-token", "platform": "ios"},
            headers=self.loyalty_user_headers,
        )

        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Targeted Notice",
                "body": "Only one user should receive this.",
                "userIds": [self.user_id],
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["delivery"]["requestedTokens"], 1)
        self.assertEqual(payload["delivery"]["successCount"], 1)
        self.assertGreaterEqual(len(self.provider.sent_payloads), 1)
        sent_tokens = self.provider.sent_payloads[-1]["tokens"]
        self.assertEqual(sent_tokens, ["target-user-token"])

    def test_admin_without_push_permission_gets_403(self) -> None:
        response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Notice",
                "body": "Body",
                "sendToAllActive": True,
            },
            headers=self.limited_admin_headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_push_summary_endpoint(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "token-user", "platform": "android"},
            headers=self.user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "token-loyalty", "platform": "ios"},
            headers=self.loyalty_user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "token-esim", "platform": "android"},
            headers=self.active_esim_user_headers,
        )

        summary_before = self.client.get(
            "/api/v1/admin/push-notifications/summary",
            headers=self.admin_headers,
        )
        self.assertEqual(summary_before.status_code, 200)
        before_payload = summary_before.json()
        self.assertEqual(before_payload["providerConfigured"], True)
        self.assertEqual(before_payload["totalDevices"], 4)
        self.assertEqual(before_payload["enabledDevices"], 4)
        self.assertEqual(before_payload["authenticatedDevices"], 3)
        self.assertEqual(before_payload["loyaltyDevices"], 1)
        self.assertEqual(before_payload["activeEsimDevices"], 1)
        self.assertEqual(before_payload["iosDevices"], 1)
        self.assertEqual(before_payload["androidDevices"], 3)
        self.assertIsNone(before_payload["lastCampaign"])

        send_response = self.client.post(
            "/api/v1/admin/push-notifications/send",
            json={
                "title": "Broadcast",
                "body": "Hello all active tokens.",
                "sendToAllActive": True,
            },
            headers=self.admin_headers,
        )
        self.assertEqual(send_response.status_code, 200)

        summary_after = self.client.get(
            "/api/v1/admin/push-notifications/summary",
            headers=self.admin_headers,
        )
        self.assertEqual(summary_after.status_code, 200)
        after_payload = summary_after.json()
        self.assertIsNotNone(after_payload["lastCampaign"])
        self.assertEqual(after_payload["lastCampaign"]["title"], "Broadcast")

    def test_admin_push_diagnostics_endpoint(self) -> None:
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "token-user", "platform": "android"},
            headers=self.user_headers,
        )
        self.client.post(
            "/api/v1/push-notifications/devices/register",
            json={"token": "token-admin", "platform": "ios"},
            headers=self.admin_headers,
        )
        response = self.client.get(
            "/api/v1/admin/push-notifications/diagnostics",
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("totalPushDevices", payload)
        self.assertIn("activePushDevices", payload)
        self.assertIn("activePushDevicesWithToken", payload)
        self.assertIn("activePushDevicesByPlatform", payload)
        self.assertIn("activePushDevicesWithUserId", payload)
        self.assertIn("activePushDevicesWithoutUserId", payload)
        self.assertIn("activePushDevicesWithAdminUserId", payload)
        self.assertIn("activeAnonymousPushDevices", payload)
        self.assertIn("sampleLatestDevices", payload)
        self.assertTrue(isinstance(payload["sampleLatestDevices"], list))
        self.assertLessEqual(len(payload["sampleLatestDevices"]), 10)
        if payload["sampleLatestDevices"]:
            sample = payload["sampleLatestDevices"][0]
            self.assertIn("id", sample)
            self.assertIn("platform", sample)
            self.assertIn("active", sample)
            self.assertIn("tokenPrefix", sample)
            self.assertIn("userId", sample)
            self.assertIn("adminUserId", sample)
            self.assertIn("subjectType", sample)
            self.assertIn("updatedAt", sample)

    # ---- Push hardening: retry / startup validation / rate limit / GC ----

    def test_retryable_error_classification(self) -> None:
        cls = PushNotificationService
        # Transient / unknown → retryable.
        self.assertTrue(cls._is_retryable_error(RuntimeError("temporarily unavailable")))
        self.assertTrue(cls._is_retryable_error(RuntimeError("connection reset by peer")))
        self.assertTrue(cls._is_retryable_error(RuntimeError("some brand new 5xx blip")))
        # Auth / credential → permanent, never retried.
        self.assertFalse(cls._is_retryable_error(RuntimeError("unauthenticated")))
        self.assertFalse(cls._is_retryable_error(RuntimeError("invalid credential supplied")))
        # Token-validity errors are handled per-token, not whole-call retried.
        self.assertFalse(cls._is_retryable_error(RuntimeError("Requested entity was not found")))

    def test_send_multicast_retries_transient_then_succeeds(self) -> None:
        import push_notification as pushmod

        calls = {"n": 0}
        sentinel = object()

        class _FakeMessaging:
            @staticmethod
            def send_each_for_multicast(message: Any, app: Any = None) -> Any:
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("temporarily unavailable, please retry")
                return sentinel

        original = pushmod.messaging
        pushmod.messaging = _FakeMessaging
        try:
            svc = PushNotificationService(max_send_attempts=3, retry_base_delay=0.0, sleep=lambda _: None)
            result = svc._send_multicast_with_retry(object(), app=object())
            self.assertIs(result, sentinel)
            self.assertEqual(calls["n"], 3)
        finally:
            pushmod.messaging = original

    def test_send_multicast_does_not_retry_permanent_error(self) -> None:
        import push_notification as pushmod

        calls = {"n": 0}

        class _FakeMessaging:
            @staticmethod
            def send_each_for_multicast(message: Any, app: Any = None) -> Any:
                calls["n"] += 1
                raise RuntimeError("unauthenticated: invalid credential")

        original = pushmod.messaging
        pushmod.messaging = _FakeMessaging
        try:
            svc = PushNotificationService(max_send_attempts=3, retry_base_delay=0.0, sleep=lambda _: None)
            with self.assertRaises(RuntimeError):
                svc._send_multicast_with_retry(object(), app=object())
            self.assertEqual(calls["n"], 1)  # permanent → attempted exactly once
        finally:
            pushmod.messaging = original

    def test_validate_configuration_reports_unconfigured(self) -> None:
        svc = PushNotificationService()  # no Firebase creds
        self.assertEqual(
            svc.validate_configuration(),
            {"configured": False, "valid": None, "error": None},
        )

    def test_cleanup_stale_anonymous_push_devices(self) -> None:
        from datetime import timedelta

        from supabase_store import utcnow

        old = utcnow() - timedelta(days=200)
        recent = utcnow() - timedelta(days=5)
        with self.session_factory() as session:
            session.add(PushDevice(token="anon-old", platform="ios", last_seen_at=old, created_at=old, updated_at=old))
            session.add(PushDevice(token="anon-recent", platform="ios", last_seen_at=recent, created_at=recent, updated_at=recent))
            session.add(
                PushDevice(
                    token="owned-old",
                    platform="ios",
                    user_id=self.user_id,
                    last_seen_at=old,
                    created_at=old,
                    updated_at=old,
                )
            )
            session.commit()

        with self.session_factory() as session:
            store = SupabaseStore(session)
            self.assertEqual(store.count_stale_anonymous_push_devices(older_than_days=90), 1)
            deleted = store.delete_stale_anonymous_push_devices(older_than_days=90)
            session.commit()
            self.assertEqual(deleted, 1)
            # Non-positive window is a guarded no-op.
            self.assertEqual(store.delete_stale_anonymous_push_devices(older_than_days=0), 0)

        with self.session_factory() as session:
            remaining = set(session.scalars(select(PushDevice.token)).all())
            # anon-old deleted; anon-recent (fresh) and owned-old (has an owner)
            # survive. "blocked-token" comes from the fixture and is owned by the
            # blocked user, so it must also survive (owned devices are never GC'd).
            self.assertEqual(remaining, {"anon-recent", "owned-old", "blocked-token"})

    def test_admin_send_is_rate_limited(self) -> None:
        import rate_limit

        prev_bypass = os.environ.get("RATE_LIMIT_BYPASS_IN_TESTS")
        os.environ["RATE_LIMIT_BYPASS_IN_TESTS"] = "false"
        os.environ["PUSH_SEND_RATE_LIMIT_MAX"] = "2"
        os.environ["PUSH_SEND_RATE_LIMIT_WINDOW_SECONDS"] = "3600"
        get_settings.cache_clear()
        rate_limit.reset()
        try:
            self.client.post(
                "/api/v1/push-notifications/devices/register",
                json={"token": "rl-token", "platform": "android"},
                headers=self.user_headers,
            )
            body = {"title": "t", "body": "b", "sendToAllActive": True}
            codes = [
                self.client.post(
                    "/api/v1/admin/push-notifications/send", json=body, headers=self.admin_headers
                ).status_code
                for _ in range(3)
            ]
            self.assertEqual(codes, [200, 200, 429])
        finally:
            rate_limit.reset()
            os.environ.pop("PUSH_SEND_RATE_LIMIT_MAX", None)
            os.environ.pop("PUSH_SEND_RATE_LIMIT_WINDOW_SECONDS", None)
            if prev_bypass is None:
                os.environ.pop("RATE_LIMIT_BYPASS_IN_TESTS", None)
            else:
                os.environ["RATE_LIMIT_BYPASS_IN_TESTS"] = prev_bypass
            get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
