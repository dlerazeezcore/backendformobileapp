from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from auth import create_access_token, hash_password
from config import get_settings
from supabase_store import AdminUser, AppUser, Base, PushDevice, TelegramSupportMessage, normalize_database_url


class TelegramSupportRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="telegram_support_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        os.environ["TELEGRAM_SUPPORT_BOT_TOKEN"] = "fake-token"
        os.environ["TELEGRAM_SUPPORT_WEBHOOK_SECRET"] = "webhook-secret"
        get_settings.cache_clear()

        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
            session.add(
                AdminUser(
                    id="11111111-1111-1111-1111-111111111111",
                    phone="+9647700000001",
                    name="Admin",
                    status="active",
                    role="admin",
                    can_manage_users=True,
                    can_manage_orders=True,
                    can_manage_pricing=True,
                    can_manage_content=True,
                    can_send_push=True,
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.add(
                AppUser(
                    id="22222222-2222-2222-2222-222222222222",
                    phone="+9647700000002",
                    name="Standard User",
                    status="active",
                )
            )
            session.add(
                PushDevice(
                    user_id="22222222-2222-2222-2222-222222222222",
                    token="push-token-1",
                    platform="ios",
                    active=True,
                )
            )
            session.commit()
        engine.dispose()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _user_token(self) -> str:
        return create_access_token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )

    def _admin_token(self) -> str:
        return create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )

    def test_send_support_message_creates_row_and_returns_sent(self) -> None:
        import telegram_support

        async def fake_send_message(*, bot_token: str, chat_id: int, text: str, reply_to: int | None = None):
            _ = reply_to
            self.assertEqual(bot_token, "fake-token")
            self.assertEqual(chat_id, -5169340336)
            self.assertIn("Phone: +9647700000002", text)
            self.assertIn("Name: Standard User", text)
            return {"ok": True, "result": {"message_id": 789}}

        original = telegram_support._telegram_send_message
        telegram_support._telegram_send_message = fake_send_message
        try:
            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/v1/support/telegram/messages",
                    headers={"Authorization": f"Bearer {self._user_token()}"},
                    json={"message": "Need help with order #1001"},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["message"]["status"], "sent")
        finally:
            telegram_support._telegram_send_message = original

    def test_admin_can_send_support_reply(self) -> None:
        app = create_app()

        captured = {}

        def fake_push(*, tokens, title, body, data, channel_id, image=None):
            _ = image
            captured["tokens"] = tokens
            captured["title"] = title
            captured["body"] = body
            captured["data"] = data
            captured["channel_id"] = channel_id
            return {"successCount": 1, "failureCount": 0, "invalidTokens": []}

        with TestClient(app) as client:
            client.app.state.push_notification_service.send_push_notification = fake_push
            response = client.post(
                "/api/v1/support/telegram/messages",
                headers={"Authorization": f"Bearer {self._admin_token()}"},
                json={"userId": "22222222-2222-2222-2222-222222222222", "message": "We are checking your issue."},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()["message"]
            self.assertEqual(payload["direction"], "admin_to_user")
            self.assertEqual(payload["status"], "sent")
            self.assertEqual(payload["pushDeliveryStatus"], "sent")

        self.assertEqual(captured["tokens"], ["push-token-1"])
        self.assertEqual(captured["title"], "Support reply")
        self.assertEqual(captured["channel_id"], "support")

    def test_admin_can_list_support_messages(self) -> None:
        import telegram_support

        async def fake_send_message(*, bot_token: str, chat_id: int, text: str, reply_to: int | None = None):
            _ = (bot_token, chat_id, text, reply_to)
            return {"ok": True, "result": {"message_id": 900}}

        original = telegram_support._telegram_send_message
        telegram_support._telegram_send_message = fake_send_message
        try:
            with TestClient(create_app()) as client:
                send_response = client.post(
                    "/api/v1/support/telegram/messages",
                    headers={"Authorization": f"Bearer {self._user_token()}"},
                    json={"message": "Need help with order #1002"},
                )
                self.assertEqual(send_response.status_code, 200)

                response = client.get(
                    "/api/v1/support/telegram/messages?limit=200&offset=0",
                    headers={"Authorization": f"Bearer {self._admin_token()}"},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertGreaterEqual(payload["pagination"]["count"], 1)
                self.assertEqual(payload["messages"][0]["userId"], "22222222-2222-2222-2222-222222222222")
        finally:
            telegram_support._telegram_send_message = original

    def test_webhook_reply_records_message_and_sends_push(self) -> None:
        app = create_app()

        def fake_push(*, tokens, title, body, data, channel_id, image=None):
            _ = image
            self.assertEqual(tokens, ["push-token-1"])
            self.assertEqual(title, "Support reply")
            self.assertEqual(channel_id, "support")
            self.assertEqual(data.get("type"), "support_reply")
            return {"successCount": 1, "failureCount": 0, "invalidTokens": []}

        # Insert a sent row directly to simulate existing telegram message mapping.
        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
            parent = TelegramSupportMessage(
                user_id="22222222-2222-2222-2222-222222222222",
                direction="user_to_admin",
                status="sent",
                message_text="Original user support message",
                telegram_chat_id=-5169340336,
                telegram_message_id=555,
            )
            session.add(parent)
            session.commit()

        with TestClient(app) as client:
            client.app.state.push_notification_service.send_push_notification = fake_push
            webhook = client.post(
                "/api/v1/support/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                json={
                    "message": {
                        "message_id": 556,
                        "text": "Hello from support team",
                        "chat": {"id": -5169340336},
                        "reply_to_message": {
                            "message_id": 555,
                            "text": "📩 Support message\nPhone: +9647700000002\nName: Standard User",
                        },
                    }
                },
            )
            self.assertEqual(webhook.status_code, 200)
            self.assertEqual(webhook.json().get("pushDeliveryStatus"), "sent")

        with session_factory() as session:
            row = session.scalar(
                select(TelegramSupportMessage).where(TelegramSupportMessage.telegram_message_id == 556)
            )
            assert row is not None
            self.assertEqual(row.user_id, "22222222-2222-2222-2222-222222222222")
            self.assertEqual(row.push_delivery_status, "sent")
        engine.dispose()

    def test_webhook_reply_maps_user_by_phone_when_no_reply_mapping(self) -> None:
        app = create_app()

        def fake_push(*, tokens, title, body, data, channel_id, image=None):
            _ = image
            self.assertEqual(tokens, ["push-token-1"])
            self.assertEqual(title, "Support reply")
            self.assertEqual(channel_id, "support")
            return {"successCount": 1, "failureCount": 0, "invalidTokens": []}

        with TestClient(app) as client:
            client.app.state.push_notification_service.send_push_notification = fake_push
            webhook = client.post(
                "/api/v1/support/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                json={
                    "message": {
                        "message_id": 557,
                        "text": "Resolved. Please check.\nPhone: +9647700000002",
                        "chat": {"id": -5169340336},
                    }
                },
            )
            self.assertEqual(webhook.status_code, 200)
            self.assertEqual(webhook.json().get("pushDeliveryStatus"), "sent")

    def test_webhooks_alias_route_accepts_telegram_payload(self) -> None:
        app = create_app()

        def fake_push(*, tokens, title, body, data, channel_id, image=None):
            _ = image
            self.assertEqual(tokens, ["push-token-1"])
            self.assertEqual(title, "Support reply")
            self.assertEqual(channel_id, "support")
            return {"successCount": 1, "failureCount": 0, "invalidTokens": []}

        with TestClient(app) as client:
            client.app.state.push_notification_service.send_push_notification = fake_push
            webhook = client.post(
                "/api/v1/support/telegram/webhooks",
                headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                json={
                    "message": {
                        "message_id": 558,
                        "text": "Alias route test\nPhone: +9647700000002",
                        "chat": {"id": -5169340336},
                    }
                },
            )
            self.assertEqual(webhook.status_code, 200)
            self.assertEqual(webhook.json().get("pushDeliveryStatus"), "sent")

if __name__ == "__main__":
    unittest.main()
