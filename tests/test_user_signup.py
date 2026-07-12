from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from auth import hash_password, verify_password
from config import get_settings
from phone_utils import normalize_phone
from supabase_store import AdminUser, AppUser, Base, normalize_database_url
from verifyway import _mint_verification_token


def _vtoken(phone: str) -> str:
    """Mint a valid WhatsApp-OTP verification token for ``phone``.

    Signup now requires a proof-of-phone token (auth.SignupPayload). The token
    binds to normalize_phone(phone); validate_verification_token re-normalizes
    the phone it is checked against, so an already-E.164 number is idempotent.
    Call this from inside a test method — i.e. AFTER setUp has set
    AUTH_SECRET_KEY and cleared the settings cache — so the mint secret matches
    the app's.
    """
    return _mint_verification_token(normalize_phone(phone))


class PublicUserSignupTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="user_signup_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name

        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()

        self.engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        with self.session_factory() as session:
            session.add(
                AppUser(
                    phone="+9647700000002",
                    name="Existing User",
                    status="active",
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.add(
                AdminUser(
                    phone="+9647700000001",
                    name="Admin User",
                    status="active",
                    role="admin",
                    can_send_push=True,
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def test_user_signup_creates_account_and_returns_session(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000100",
                    "name": "New Customer",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000100"),
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("phone"), "+9647700000100")
            self.assertEqual(payload.get("name"), "New Customer")
            self.assertTrue(payload.get("userId"))
            self.assertEqual(payload.get("id"), payload.get("userId"))
            self.assertEqual(payload.get("subjectType"), "user")

            me_response = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {payload['accessToken']}"},
            )
            self.assertEqual(me_response.status_code, 200)
            self.assertEqual(me_response.json().get("subjectType"), "user")
            self.assertEqual(me_response.json().get("phone"), "+9647700000100")

            login_response = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000100", "password": "StrongPass123"},
            )
            self.assertEqual(login_response.status_code, 200)
            login_payload = login_response.json()
            self.assertTrue(login_payload.get("accessToken"))
            self.assertEqual(login_payload.get("subjectType"), "user")
            self.assertEqual(login_payload.get("isAdmin"), False)
            self.assertEqual(login_payload.get("id"), login_payload.get("userId"))
            self.assertEqual(login_payload.get("phone"), "+9647700000100")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000100"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "active")
            self.assertTrue(verify_password("StrongPass123", row.password_hash))

    def test_login_matches_user_stored_in_legacy_formatted_phone(self) -> None:
        with self.session_factory() as session:
            legacy_user = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
            self.assertIsNotNone(legacy_user)
            assert legacy_user is not None
            legacy_user.phone = "0750 000 0002"
            session.commit()

        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647500000002", "password": "StrongPass123"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload.get("subjectType"), "user")

    def test_user_register_alias_works(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/register",
                json={
                    "phone": "+9647700000101",
                    "name": "Alias User",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000101"),
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("phone"), "+9647700000101")

    def test_signup_duplicate_user_returns_409(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000002",
                    "name": "Duplicate User",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000002"),
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("already exists", str(response.json().get("detail", "")).lower())

    def test_signup_admin_phone_returns_409(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000001",
                    "name": "Conflicting User",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000001"),
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("admin", str(response.json().get("detail", "")).lower())

    def test_signup_too_short_local_phone_returns_422(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "0770000123",
                    "name": "Invalid Phone",
                    "password": "StrongPass123",
                    # Field must be present to clear pydantic; the phone 422s
                    # before the token is ever validated, so a dummy is fine.
                    "verificationToken": _vtoken("0770000123"),
                },
            )
            self.assertEqual(response.status_code, 422)

    def test_signup_requires_verification_token(self) -> None:
        with TestClient(create_app()) as client:
            # Present but invalid token → handler rejects with 400 AUTH_OTP_REQUIRED.
            invalid_token = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000150",
                    "name": "No OTP User",
                    "password": "StrongPass123",
                    "verificationToken": "not-a-real-token",
                },
            )
            self.assertEqual(invalid_token.status_code, 400)
            self.assertEqual(
                (invalid_token.json().get("detail") or {}).get("code"),
                "AUTH_OTP_REQUIRED",
            )

            # A valid token minted for a DIFFERENT phone must not pass either.
            wrong_phone_token = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000150",
                    "name": "No OTP User",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000151"),
                },
            )
            self.assertEqual(wrong_phone_token.status_code, 400)
            self.assertEqual(
                (wrong_phone_token.json().get("detail") or {}).get("code"),
                "AUTH_OTP_REQUIRED",
            )

            # Field absent entirely → pydantic rejects with 422 before the handler.
            missing_token = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000150",
                    "name": "No OTP User",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(missing_token.status_code, 422)

        # Nothing was persisted for the rejected signups.
        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000150"))
            self.assertIsNone(row)

    def test_authenticated_user_can_self_delete(self) -> None:
        with TestClient(create_app()) as client:
            signup_response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000199",
                    "name": "Delete Me",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000199"),
                },
            )
            self.assertEqual(signup_response.status_code, 200)
            access_token = signup_response.json()["accessToken"]

            delete_response = client.delete(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(delete_response.status_code, 200)
            delete_payload = delete_response.json()
            self.assertTrue(delete_payload.get("deleted"))
            self.assertEqual(delete_payload.get("status"), "deleted")
            self.assertEqual(delete_payload.get("id"), delete_payload.get("userId"))

            delete_alias_response = client.post(
                "/api/v1/auth/user/delete",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(delete_alias_response.status_code, 200)
            self.assertTrue(delete_alias_response.json().get("deleted"))

            delete_unversioned_alias = client.delete(
                "/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(delete_unversioned_alias.status_code, 200)
            self.assertEqual(delete_unversioned_alias.json().get("status"), "deleted")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000199"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "deleted")
            self.assertIsNotNone(row.deleted_at)

    def test_authenticated_user_can_update_own_name_via_auth_me_patch(self) -> None:
        with TestClient(create_app()) as client:
            signup_response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000201",
                    "name": "Before Name",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000201"),
                },
            )
            self.assertEqual(signup_response.status_code, 200)
            access_token = signup_response.json()["accessToken"]

            update_response = client.patch(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"name": "After Name"},
            )
            self.assertEqual(update_response.status_code, 200)
            payload = update_response.json()
            self.assertEqual(payload.get("subjectType"), "user")
            self.assertEqual(payload.get("name"), "After Name")
            self.assertEqual(payload.get("id"), payload.get("userId"))

            me_response = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(me_response.status_code, 200)
            self.assertEqual(me_response.json().get("name"), "After Name")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000201"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.name, "After Name")

    def test_auth_me_patch_supports_email_only_and_clear_email(self) -> None:
        with TestClient(create_app()) as client:
            signup_response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000202",
                    "name": "Email User",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000202"),
                },
            )
            self.assertEqual(signup_response.status_code, 200)
            access_token = signup_response.json()["accessToken"]

            add_email = client.patch(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"email": "Email.User@Example.com"},
            )
            self.assertEqual(add_email.status_code, 200)
            add_payload = add_email.json()
            self.assertEqual(add_payload.get("email"), "email.user@example.com")

            me_after_add = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(me_after_add.status_code, 200)
            self.assertEqual(me_after_add.json().get("email"), "email.user@example.com")

            clear_email = client.patch(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"email": None},
            )
            self.assertEqual(clear_email.status_code, 200)
            self.assertIsNone(clear_email.json().get("email"))

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000202"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertIsNone(row.email)

    def test_auth_me_patch_invalid_email_returns_422(self) -> None:
        with TestClient(create_app()) as client:
            signup_response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000203",
                    "name": "Invalid Email User",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken("+9647700000203"),
                },
            )
            self.assertEqual(signup_response.status_code, 200)
            access_token = signup_response.json()["accessToken"]

            invalid_email = client.patch(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"email": "not-an-email"},
            )
            self.assertEqual(invalid_email.status_code, 422)
            detail = invalid_email.json().get("detail") or {}
            self.assertEqual(detail.get("code"), "AUTH_INVALID_EMAIL")

    def test_auth_me_stamps_reported_app_version(self) -> None:
        with TestClient(create_app()) as client:
            login = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000002", "password": "StrongPass123"},
            )
            self.assertEqual(login.status_code, 200)
            headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}

            # No header → nothing stamped.
            self.assertEqual(client.get("/api/v1/auth/me", headers=headers).status_code, 200)
            with self.session_factory() as session:
                row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
                self.assertIsNone(row.app_version)

            # Header present → version stamped with a timestamp.
            resp = client.get("/api/v1/auth/me", headers={**headers, "X-App-Version": "1.4.0"})
            self.assertEqual(resp.status_code, 200)
            with self.session_factory() as session:
                row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
                self.assertEqual(row.app_version, "1.4.0")
                self.assertIsNotNone(row.app_version_updated_at)

            # Newer build reported → value updated.
            client.get("/api/v1/auth/me", headers={**headers, "X-App-Version": "1.5.0"})
            with self.session_factory() as session:
                row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
                self.assertEqual(row.app_version, "1.5.0")

    def test_profiles_my_also_stamps_app_version(self) -> None:
        # Broadened capture: any authenticated user request stamps the build,
        # not just /auth/me (require_active_subject wires it into profiles/my).
        with TestClient(create_app()) as client:
            login = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000002", "password": "StrongPass123"},
            )
            headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
            resp = client.get(
                "/api/v1/esim-access/profiles/my",
                headers={**headers, "X-App-Version": "2.0.1"},
            )
            self.assertEqual(resp.status_code, 200)
            with self.session_factory() as session:
                row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
                self.assertEqual(row.app_version, "2.0.1")

    def test_stamp_app_version_does_not_commit_unrelated_pending_state(self) -> None:
        # Audit #10: the stamp is an isolated write in its own short-lived
        # session — it must not flush/commit other pending changes sitting in
        # the request session of an otherwise read-only handler.
        from auth import _stamp_app_version

        with self.session_factory() as db:
            user = db.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
            admin = db.scalar(select(AdminUser).where(AdminUser.phone == "+9647700000001"))
            self.assertIsNotNone(user)
            self.assertIsNotNone(admin)
            assert user is not None and admin is not None
            admin.name = "Pending Rename"  # unrelated dirty state, never committed

            _stamp_app_version(db, user, "3.1.4")

            # The loaded row reflects the stamp without being marked dirty.
            self.assertEqual(user.app_version, "3.1.4")
            self.assertNotIn(user, db.dirty)

        with self.session_factory() as session:
            stamped = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
            assert stamped is not None
            self.assertEqual(stamped.app_version, "3.1.4")
            self.assertIsNotNone(stamped.app_version_updated_at)
            untouched = session.scalar(select(AdminUser).where(AdminUser.phone == "+9647700000001"))
            assert untouched is not None
            self.assertEqual(untouched.name, "Admin User")

    def test_auth_me_patch_changes_password_for_user(self) -> None:
        # Audit #11: self-service password change moved from the retired
        # non-admin branch of POST /admin/users to PATCH /auth/me.
        with TestClient(create_app()) as client:
            login = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000002", "password": "StrongPass123"},
            )
            self.assertEqual(login.status_code, 200)
            headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}

            too_short = client.patch(
                "/api/v1/auth/me",
                headers=headers,
                json={"password": "short", "currentPassword": "StrongPass123"},
            )
            self.assertEqual(too_short.status_code, 422)

            # SEC hardening: a bearer token alone must not be able to re-key
            # the account — the current password is required and verified.
            missing_current = client.patch(
                "/api/v1/auth/me",
                headers=headers,
                json={"password": "NewStrongPass456"},
            )
            self.assertEqual(missing_current.status_code, 401)

            wrong_current = client.patch(
                "/api/v1/auth/me",
                headers=headers,
                json={"password": "NewStrongPass456", "currentPassword": "WrongPass999"},
            )
            self.assertEqual(wrong_current.status_code, 401)

            update = client.patch(
                "/api/v1/auth/me",
                headers=headers,
                json={"password": "NewStrongPass456", "currentPassword": "StrongPass123"},
            )
            self.assertEqual(update.status_code, 200, update.text)

            relogin_old = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000002", "password": "StrongPass123"},
            )
            self.assertEqual(relogin_old.status_code, 401)

            relogin_new = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000002", "password": "NewStrongPass456"},
            )
            self.assertEqual(relogin_new.status_code, 200)

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000002"))
            assert row is not None
            self.assertTrue(verify_password("NewStrongPass456", row.password_hash))


if __name__ == "__main__":
    unittest.main()
