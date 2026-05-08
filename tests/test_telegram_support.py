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
                AdminUser(
                    id="44444444-4444-4444-4444-444444444444",
                    phone="+9647700000004",
                    name="Second Admin",
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
            session.add(
                PushDevice(
                    admin_user_id="11111111-1111-1111-1111-111111111111",
                    token="push-token-admin",
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
            self.assertIn("User ID: 22222222-2222-2222-2222-222222222222", text)
            self.assertIn("Phone: +9647700000002", text)
            self.assertIn("Name: Standard User", text)
            return {"ok": True, "result": {"message_id": 789}}

        async def fake_ensure_webhook(*, bot_token: str, webhook_secret: str | None, webhook_base_url: str):
            _ = (bot_token, webhook_secret, webhook_base_url)
            return None

        original = telegram_support._telegram_send_message
        original_ensure = telegram_support._ensure_telegram_webhook
        telegram_support._telegram_send_message = fake_send_message
        telegram_support._ensure_telegram_webhook = fake_ensure_webhook
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
                self.assertEqual(payload["message"]["direction"], "user_to_support")
                self.assertEqual(payload["message"]["senderType"], "user")
                self.assertEqual(payload["message"]["isFromCurrentActor"], True)
                self.assertTrue(str(payload["message"]["conversationId"]).startswith("user:"))
                self.assertEqual(payload["message"]["senderUserId"], "22222222-2222-2222-2222-222222222222")
                self.assertIsNone(payload["message"]["senderAdminUserId"])
        finally:
            telegram_support._telegram_send_message = original
            telegram_support._ensure_telegram_webhook = original_ensure

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
            self.assertEqual(payload["senderType"], "admin")
            self.assertEqual(payload["isFromCurrentActor"], True)
            self.assertEqual(payload["adminUserId"], "11111111-1111-1111-1111-111111111111")
            self.assertEqual(payload["senderAdminUserId"], "11111111-1111-1111-1111-111111111111")
            self.assertIsNone(payload["senderUserId"])
            self.assertTrue(str(payload["conversationId"]).startswith("user:"))

        self.assertEqual(captured["tokens"], ["push-token-1"])
        self.assertEqual(captured["title"], "Support reply")
        self.assertEqual(captured["channel_id"], "support")

    def test_admin_can_send_message_without_target_user_as_self_conversation(self) -> None:
        import telegram_support

        async def fake_send_message(*, bot_token: str, chat_id: int, text: str, reply_to: int | None = None):
            _ = reply_to
            self.assertEqual(bot_token, "fake-token")
            self.assertEqual(chat_id, -5169340336)
            self.assertIn("Admin ID: 11111111-1111-1111-1111-111111111111", text)
            self.assertIn("Phone: +9647700000001", text)
            self.assertIn("Name: Admin", text)
            return {"ok": True, "result": {"message_id": 990}}

        async def fake_ensure_webhook(*, bot_token: str, webhook_secret: str | None, webhook_base_url: str):
            _ = (bot_token, webhook_secret, webhook_base_url)
            return None

        original_send_message = telegram_support._telegram_send_message
        original_ensure = telegram_support._ensure_telegram_webhook
        telegram_support._telegram_send_message = fake_send_message
        telegram_support._ensure_telegram_webhook = fake_ensure_webhook
        try:
            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/v1/support/telegram/messages",
                    headers={"Authorization": f"Bearer {self._admin_token()}"},
                    json={"message": "Need support for admin account."},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()["message"]
                self.assertEqual(payload["direction"], "admin_to_support")
                self.assertEqual(payload["senderType"], "admin")
                self.assertEqual(payload["isFromCurrentActor"], True)
                self.assertEqual(payload["adminUserId"], "11111111-1111-1111-1111-111111111111")
                self.assertEqual(payload["senderAdminUserId"], "11111111-1111-1111-1111-111111111111")
                self.assertTrue(str(payload["conversationId"]).startswith("admin:"))
        finally:
            telegram_support._telegram_send_message = original_send_message
            telegram_support._ensure_telegram_webhook = original_ensure

    def test_admin_can_send_support_reply_using_support_message_id(self) -> None:
        app = create_app()

        def fake_push(*, tokens, title, body, data, channel_id, image=None):
            _ = (title, body, data, channel_id, image)
            self.assertEqual(tokens, ["push-token-1"])
            return {"successCount": 1, "failureCount": 0, "invalidTokens": []}

        with TestClient(app) as client:
            with client.app.state.db_session_factory() as session:
                source = TelegramSupportMessage(
                    user_id="22222222-2222-2222-2222-222222222222",
                    direction="user_to_admin",
                    status="sent",
                    message_text="Original user message",
                    telegram_chat_id=-5169340336,
                    telegram_message_id=111,
                )
                session.add(source)
                session.commit()
                support_message_id = source.id

            client.app.state.push_notification_service.send_push_notification = fake_push
            response = client.post(
                "/api/v1/support/telegram/messages",
                headers={"Authorization": f"Bearer {self._admin_token()}"},
                json={"supportMessageId": support_message_id, "message": "Reply via support message context."},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()["message"]
            self.assertEqual(payload["userId"], "22222222-2222-2222-2222-222222222222")
            self.assertEqual(payload["direction"], "admin_to_user")
            self.assertEqual(payload["senderType"], "admin")
            self.assertTrue(payload["isFromCurrentActor"])
            self.assertTrue(str(payload["conversationId"]).startswith("user:"))

    def test_admin_reply_mode_with_blank_target_returns_422(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/support/telegram/messages",
                headers={"Authorization": f"Bearer {self._admin_token()}"},
                json={"userId": "   ", "message": "Should fail as reply mode."},
            )
            self.assertEqual(response.status_code, 422)
            self.assertIn("userId is required for admin reply mode", response.json().get("detail", ""))

    def test_admin_can_list_support_messages(self) -> None:
        import telegram_support

        async def fake_send_message(*, bot_token: str, chat_id: int, text: str, reply_to: int | None = None):
            _ = (bot_token, chat_id, text, reply_to)
            return {"ok": True, "result": {"message_id": 900}}

        async def fake_ensure_webhook(*, bot_token: str, webhook_secret: str | None, webhook_base_url: str):
            _ = (bot_token, webhook_secret, webhook_base_url)
            return None

        original = telegram_support._telegram_send_message
        original_ensure = telegram_support._ensure_telegram_webhook
        telegram_support._telegram_send_message = fake_send_message
        telegram_support._ensure_telegram_webhook = fake_ensure_webhook
        try:
            with TestClient(create_app()) as client:
                send_response = client.post(
                    "/api/v1/support/telegram/messages",
                    headers={"Authorization": f"Bearer {self._user_token()}"},
                    json={"message": "Need help with order #1002"},
                )
                self.assertEqual(send_response.status_code, 200)

                response = client.get(
                    "/api/v1/support/telegram/messages?limit=200&offset=0&allUsers=true",
                    headers={"Authorization": f"Bearer {self._admin_token()}"},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload.get("success"), True)
                data = payload["data"]
                self.assertGreaterEqual(data["pagination"]["count"], 1)
                self.assertEqual(data["messages"][0]["userId"], "22222222-2222-2222-2222-222222222222")
                self.assertIn("senderType", data["messages"][0])
                self.assertIn("senderRole", data["messages"][0])
                self.assertIn("isFromCurrentActor", data["messages"][0])
                self.assertIn("adminUserId", data["messages"][0])
                self.assertIn("conversationId", data["messages"][0])
                self.assertIn("senderUserId", data["messages"][0])
                self.assertIn("senderAdminUserId", data["messages"][0])
        finally:
            telegram_support._telegram_send_message = original
            telegram_support._ensure_telegram_webhook = original_ensure

    def test_admin_list_defaults_to_own_conversation_scope(self) -> None:
        with TestClient(create_app()) as client:
            with client.app.state.db_session_factory() as session:
                own_row = TelegramSupportMessage(
                    admin_user_id="11111111-1111-1111-1111-111111111111",
                    direction="admin_to_support",
                    status="sent",
                    message_text="Own thread",
                )
                other_admin_row = TelegramSupportMessage(
                    admin_user_id="44444444-4444-4444-4444-444444444444",
                    direction="admin_to_support",
                    status="sent",
                    message_text="Other admin thread",
                )
                admin_reply_row = TelegramSupportMessage(
                    user_id="22222222-2222-2222-2222-222222222222",
                    admin_user_id="11111111-1111-1111-1111-111111111111",
                    direction="admin_to_user",
                    status="sent",
                    message_text="Admin reply to app user thread",
                )
                session.add(own_row)
                session.add(other_admin_row)
                session.add(admin_reply_row)
                session.commit()
                own_row_id = own_row.id
                other_admin_row_id = other_admin_row.id
                admin_reply_row_id = admin_reply_row.id

            response = client.get(
                "/api/v1/support/telegram/messages?limit=200&offset=0",
                headers={"Authorization": f"Bearer {self._admin_token()}"},
            )
            self.assertEqual(response.status_code, 200)
            rows = response.json().get("data", {}).get("messages", [])
            message_ids = {item.get("id") for item in rows}
            self.assertIn(own_row_id, message_ids)
            self.assertNotIn(other_admin_row_id, message_ids)
            self.assertNotIn(admin_reply_row_id, message_ids)

    def test_list_normalizes_legacy_unknown_direction_to_canonical_values(self) -> None:
        with TestClient(create_app()) as client:
            with client.app.state.db_session_factory() as session:
                legacy_row = TelegramSupportMessage(
                    user_id="22222222-2222-2222-2222-222222222222",
                    direction="legacy_unknown_direction",
                    status="sent",
                    message_text="Legacy row",
                )
                session.add(legacy_row)
                session.commit()
                legacy_row_id = legacy_row.id

            response = client.get(
                "/api/v1/support/telegram/messages?limit=200&offset=0&allUsers=true",
                headers={"Authorization": f"Bearer {self._admin_token()}"},
            )
            self.assertEqual(response.status_code, 200)
            messages_payload = response.json().get("data", {}).get("messages", [])
            row = next((item for item in messages_payload if item.get("id") == legacy_row_id), None)
            self.assertIsNotNone(row)
            self.assertEqual(row["direction"], "user_to_support")
            self.assertEqual(row["senderType"], "user")
            self.assertTrue(str(row["conversationId"]).startswith("user:"))

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
            self.assertEqual(row.direction, "support_to_user")
            self.assertEqual(row.push_delivery_status, "sent")
        engine.dispose()

    def test_webhook_reply_records_admin_thread_and_sends_admin_push(self) -> None:
        app = create_app()

        def fake_push(*, tokens, title, body, data, channel_id, image=None):
            _ = (title, body, data, channel_id, image)
            self.assertEqual(tokens, ["push-token-admin"])
            return {"successCount": 1, "failureCount": 0, "invalidTokens": []}

        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
            parent = TelegramSupportMessage(
                admin_user_id="11111111-1111-1111-1111-111111111111",
                direction="admin_to_support",
                status="sent",
                message_text="Admin initiated support message",
                telegram_chat_id=-5169340336,
                telegram_message_id=777,
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
                        "message_id": 778,
                        "text": "Reply for admin thread",
                        "chat": {"id": -5169340336},
                        "reply_to_message": {
                            "message_id": 777,
                            "text": "📩 Admin support message\nAdmin ID: 11111111-1111-1111-1111-111111111111",
                        },
                    }
                },
            )
            self.assertEqual(webhook.status_code, 200)
            self.assertEqual(webhook.json().get("pushDeliveryStatus"), "sent")
            self.assertEqual(webhook.json().get("adminUserId"), "11111111-1111-1111-1111-111111111111")

        with session_factory() as session:
            row = session.scalar(
                select(TelegramSupportMessage).where(TelegramSupportMessage.telegram_message_id == 778)
            )
            assert row is not None
            self.assertEqual(row.admin_user_id, "11111111-1111-1111-1111-111111111111")
            self.assertEqual(row.direction, "support_to_admin")
            self.assertEqual(row.push_delivery_status, "sent")
        engine.dispose()

    def test_support_upload_presign_and_attachment_only_message(self) -> None:
        import telegram_support
        outer_self = self

        class FakeS3Client:
            def generate_presigned_url(self, operation_name: str, Params: dict[str, str], ExpiresIn: int) -> str:
                outer_self.assertIn(operation_name, {"put_object", "get_object"})
                outer_self.assertEqual(Params["Bucket"], "Tulip Mobile APP")
                outer_self.assertIn("support/user/22222222-2222-2222-2222-222222222222", Params["Key"])
                if operation_name == "put_object":
                    outer_self.assertEqual(Params["ContentType"], "image/jpeg")
                outer_self.assertGreaterEqual(ExpiresIn, 60)
                return "https://example-upload.local/presigned"

            def get_object(self, Bucket: str, Key: str) -> dict[str, object]:
                import io

                outer_self.assertEqual(Bucket, "Tulip Mobile APP")
                outer_self.assertIn("support/user/22222222-2222-2222-2222-222222222222", Key)
                return {"Body": io.BytesIO(b"fake-image-bytes"), "ContentType": "image/jpeg"}

        async def fake_send_message(*, bot_token: str, chat_id: int, text: str, reply_to: int | None = None):
            _ = (bot_token, chat_id, text, reply_to)
            self.fail("sendMessage should not be used for image attachments when publicUrl is present.")

        async def fake_send_photo(
            *,
            bot_token: str,
            chat_id: int,
            photo_url: str | None = None,
            photo_bytes: bytes | None = None,
            photo_filename: str | None = None,
            photo_content_type: str | None = None,
            caption: str | None = None,
            reply_to: int | None = None,
        ):
            _ = (bot_token, chat_id, reply_to)
            self.assertIsNone(photo_url)
            self.assertEqual(photo_bytes, b"fake-image-bytes")
            self.assertEqual(photo_filename, "issue-photo.jpg")
            self.assertEqual(photo_content_type, "image/jpeg")
            self.assertIn("User ID: 22222222-2222-2222-2222-222222222222", caption or "")
            self.assertIn("[Attachment only]", caption or "")
            return {"ok": True, "result": {"message_id": 901}}

        async def fake_ensure_webhook(*, bot_token: str, webhook_secret: str | None, webhook_base_url: str):
            _ = (bot_token, webhook_secret, webhook_base_url)
            return None

        original_client_builder = telegram_support._build_support_upload_client
        original_send = telegram_support._telegram_send_message
        original_send_photo = telegram_support._telegram_send_photo
        original_ensure = telegram_support._ensure_telegram_webhook
        telegram_support._build_support_upload_client = lambda settings: FakeS3Client()
        telegram_support._telegram_send_message = fake_send_message
        telegram_support._telegram_send_photo = fake_send_photo
        telegram_support._ensure_telegram_webhook = fake_ensure_webhook
        os.environ["SUPPORT_UPLOADS_S3_ENDPOINT"] = "https://splzxivzahitxmjcqstn.storage.supabase.co/storage/v1/s3"
        os.environ["SUPPORT_UPLOADS_ACCESS_KEY_ID"] = "key"
        os.environ["SUPPORT_UPLOADS_SECRET_ACCESS_KEY"] = "secret"
        os.environ["SUPPORT_UPLOADS_BUCKET"] = "Tulip Mobile APP"
        get_settings.cache_clear()
        try:
            with TestClient(create_app()) as client:
                presign = client.post(
                    "/api/v1/support/uploads/presign",
                    headers={"Authorization": f"Bearer {self._user_token()}"},
                    json={"fileName": "issue-photo.jpg", "contentType": "image/jpeg", "sizeBytes": 1024},
                )
                self.assertEqual(presign.status_code, 200)
                upload = presign.json()["upload"]
                self.assertEqual(upload["method"], "PUT")
                self.assertEqual(upload["requiredHeaders"]["Content-Type"], "image/jpeg")
                self.assertTrue(upload["publicUrl"])
                send = client.post(
                    "/api/v1/support/telegram/messages",
                    headers={"Authorization": f"Bearer {self._user_token()}"},
                    json={
                        "message": "",
                        "attachments": [
                            {
                                "objectPath": upload["objectPath"],
                                "publicUrl": upload["publicUrl"],
                                "fileName": "issue-photo.jpg",
                                "contentType": "image/jpeg",
                                "sizeBytes": 1024,
                            }
                        ],
                    },
                )
                self.assertEqual(send.status_code, 200)
                self.assertEqual(send.json()["message"]["status"], "sent")
                self.assertEqual(len(send.json()["message"]["attachments"]), 1)
                attachment = send.json()["message"]["attachments"][0]
                self.assertTrue(attachment.get("objectPath"))
                self.assertTrue(attachment.get("publicUrl"))
                self.assertEqual(attachment.get("contentType"), "image/jpeg")
                self.assertEqual(attachment.get("fileName"), "issue-photo.jpg")
                self.assertEqual(attachment.get("sizeBytes"), 1024)
        finally:
            telegram_support._build_support_upload_client = original_client_builder
            telegram_support._telegram_send_message = original_send
            telegram_support._telegram_send_photo = original_send_photo
            telegram_support._ensure_telegram_webhook = original_ensure
            get_settings.cache_clear()

    def test_webhook_photo_only_message_is_mirrored_and_not_ignored(self) -> None:
        import telegram_support

        async def fake_mirror_attachment(**kwargs):
            _ = kwargs
            return {
                "objectPath": "support/telegram/inbound/2026/04/11/photo.jpg",
                "publicUrl": "https://example.com/photo.jpg",
                "fileName": "photo.jpg",
                "contentType": "image/jpeg",
                "sizeBytes": 12345,
                "source": "telegram",
                "telegramFileId": "file-123",
            }

        original_mirror = telegram_support._mirror_telegram_attachment_to_support_bucket
        telegram_support._mirror_telegram_attachment_to_support_bucket = fake_mirror_attachment
        try:
            with TestClient(create_app()) as client:
                webhook = client.post(
                    "/api/v1/support/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                    json={
                        "message": {
                            "message_id": 561,
                            "photo": [{"file_id": "file-123", "file_size": 999}],
                            "chat": {"id": -5169340336},
                        }
                    },
                )
                self.assertEqual(webhook.status_code, 200)
                self.assertTrue(webhook.json().get("ok"))
                self.assertNotEqual(webhook.json().get("ignored"), "empty_text")
        finally:
            telegram_support._mirror_telegram_attachment_to_support_bucket = original_mirror

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

    def test_webhook_accepts_secret_from_query_param(self) -> None:
        with TestClient(create_app()) as client:
            webhook = client.post(
                "/api/v1/support/telegram/webhook?secret=webhook-secret",
                json={
                    "message": {
                        "message_id": 559,
                        "text": "Query secret test\nPhone: +9647700000002",
                        "chat": {"id": -5169340336},
                    }
                },
            )
            self.assertEqual(webhook.status_code, 200)
            self.assertTrue(webhook.json().get("ok"))

    def test_webhook_duplicate_message_id_is_idempotent(self) -> None:
        with TestClient(create_app()) as client:
            payload = {
                "message": {
                    "message_id": 560,
                    "text": "Duplicate test\nPhone: +9647700000002",
                    "chat": {"id": -5169340336},
                }
            }
            first = client.post(
                "/api/v1/support/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                json=payload,
            )
            self.assertEqual(first.status_code, 200)
            second = client.post(
                "/api/v1/support/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                json=payload,
            )
            self.assertEqual(second.status_code, 200)
            self.assertTrue(second.json().get("duplicate"))

    # ------------------------------------------------------------------
    # Phase 1.4 hardening tests added with migration 0032
    # ------------------------------------------------------------------

    def test_webhook_rejects_when_secret_empty_in_config(self) -> None:
        """Empty TELEGRAM_SUPPORT_WEBHOOK_SECRET must close the webhook entirely.

        The previous behaviour returned 200 because hmac.compare_digest("","")
        is True; that left the bot impersonable whenever the env var was unset.
        """
        os.environ["TELEGRAM_SUPPORT_WEBHOOK_SECRET"] = ""
        get_settings.cache_clear()
        try:
            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/v1/support/telegram/webhook",
                    json={
                        "message": {
                            "message_id": 700,
                            "text": "noop",
                            "chat": {"id": -5169340336},
                        }
                    },
                )
                self.assertEqual(response.status_code, 403)
        finally:
            os.environ["TELEGRAM_SUPPORT_WEBHOOK_SECRET"] = "webhook-secret"
            get_settings.cache_clear()

    def test_webhook_attachment_oversize_is_rejected(self) -> None:
        """A Telegram-inbound attachment exceeding the configured cap raises 413
        from the streaming downloader and must NOT land an S3 object.
        """
        import telegram_support
        from fastapi import HTTPException

        async def fake_get_file_path(*, bot_token: str, file_id: str):
            return f"photos/{file_id}.jpg"

        async def fake_download_oversize(*, bot_token: str, file_path: str, max_bytes: int):
            raise HTTPException(
                status_code=413,
                detail=f"Telegram attachment exceeds the maximum allowed size ({max_bytes} bytes)",
            )

        upload_calls: list[tuple] = []

        def fake_upload(*, settings, object_path, file_bytes, content_type):
            upload_calls.append((object_path, len(file_bytes)))
            return f"https://example.local/{object_path}"

        original_path = telegram_support._telegram_get_file_path
        original_download = telegram_support._telegram_download_file
        original_upload = telegram_support._upload_support_file_bytes
        telegram_support._telegram_get_file_path = fake_get_file_path
        telegram_support._telegram_download_file = fake_download_oversize
        telegram_support._upload_support_file_bytes = fake_upload
        try:
            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/v1/support/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                    json={
                        "message": {
                            "message_id": 711,
                            "chat": {"id": -5169340336},
                            "photo": [{"file_id": "huge", "file_size": 50 * 1024 * 1024}],
                            "caption": "Phone: +9647700000002",
                        }
                    },
                )
                # Webhook returns 502/413/etc on the chained HTTPException;
                # the critical assertion is that NO S3 object was written.
                self.assertNotEqual(response.status_code, 200)
                self.assertEqual(upload_calls, [])
        finally:
            telegram_support._telegram_get_file_path = original_path
            telegram_support._telegram_download_file = original_download
            telegram_support._upload_support_file_bytes = original_upload

    def test_attachment_objectpath_must_match_actor_prefix(self) -> None:
        """A user submitting an attachment whose objectPath belongs to another
        user (or to no actor at all) gets a 422 — closes the spoofing surface.
        """
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/support/telegram/messages",
                headers={"Authorization": f"Bearer {self._user_token()}"},
                json={
                    "message": "",
                    "attachments": [
                        {
                            # this prefix belongs to a different user
                            "objectPath": "support/user/99999999-9999-9999-9999-999999999999/2026/05/08/foreign.jpg",
                            "publicUrl": "https://example.local/foreign.jpg",
                            "fileName": "foreign.jpg",
                            "contentType": "image/jpeg",
                            "sizeBytes": 1024,
                        }
                    ],
                },
            )
            self.assertEqual(response.status_code, 422)
            self.assertIn("upload prefix", str(response.json()).lower())

    def test_orm_index_topology_matches_migration_0032(self) -> None:
        """Regression guard: the ORM's __table_args__ must expose the new
        ix_telegram_support_messages_chat_created composite (added in 0032)
        and must NOT redeclare the dropped single-column indexes for
        ``direction`` or ``status``. The partial index
        ix_telegram_support_messages_admin_self_created is intentionally
        migration-only (partial WHERE clauses aren't portable in SQLAlchemy
        declarative) so it isn't asserted here.
        """
        from sqlalchemy import create_engine, inspect
        from supabase_store import normalize_database_url

        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        try:
            inspector = inspect(engine)
            index_names = {ix["name"] for ix in inspector.get_indexes("telegram_support_messages")}
            self.assertIn("ix_telegram_support_messages_chat_created", index_names)
            # The redundant single-column indexes dropped in 0032 must NOT come
            # back via the ORM declarative on table-create-from-metadata.
            self.assertNotIn("ix_telegram_support_messages_direction", index_names)
            self.assertNotIn("ix_telegram_support_messages_status", index_names)
        finally:
            engine.dispose()

    def test_provider_payload_is_truncated_when_huge(self) -> None:
        """An inbound Telegram update larger than 16KB must be stored in
        truncated form (provider_payload['_truncated'] == True) so we don't
        bloat the table with full Telegram update bodies.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from supabase_store import normalize_database_url

        # Build a payload whose JSON serialization is well over 16KB.
        big_text = "x" * 32768
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/support/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
                json={
                    "message": {
                        "message_id": 911,
                        "text": f"{big_text}\nPhone: +9647700000002",
                        "chat": {"id": -5169340336},
                    }
                },
            )
            self.assertEqual(response.status_code, 200)
            message_id = response.json().get("messageId")
            self.assertTrue(message_id)

        # Re-open the DB and verify the row's stored provider_payload is
        # the truncated marker, not the full ~32KB body.
        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        try:
            session_factory = sessionmaker(bind=engine, future=True)
            with session_factory() as session:
                row = session.scalar(
                    select(TelegramSupportMessage).where(TelegramSupportMessage.id == message_id)
                )
                self.assertIsNotNone(row)
                payload = row.provider_payload
                self.assertIsInstance(payload, dict)
                self.assertTrue(payload.get("_truncated"))
                self.assertGreater(int(payload.get("originalSize") or 0), 16384)
                # The preview is bounded by max_bytes // 2 = 8192 chars.
                self.assertLessEqual(len(str(payload.get("preview") or "")), 8192)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
