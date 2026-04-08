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

    def test_send_to_all_active_excludes_admin_registered_devices(self) -> None:
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
        # Expect exactly 2 user-owned tokens (blocked + newly registered), not admin token.
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
        self.assertEqual(payload.get("activeUserTokens"), 1)
        self.assertEqual(payload.get("activeAdminTokens"), 0)
        self.assertEqual(payload.get("eligibleTokensForRequestedAudience"), 0)

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


if __name__ == "__main__":
    unittest.main()
