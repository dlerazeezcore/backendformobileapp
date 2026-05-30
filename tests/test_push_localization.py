"""Unit tests for push_localization helpers + payload validators."""

from __future__ import annotations

import pytest

from push_localization import (
    APP_UPDATE_MESSAGES,
    DEFAULT_LANG,
    SUPPORTED_LANGS,
    app_update_text,
    normalize_maps,
    pick_text,
    resolve_locale,
)
from push_notification import SendAppUpdateNotificationPayload, SendPushNotificationPayload


# ─── resolve_locale ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "device_locale, user_pref, expected",
    [
        ("en", None, "en"),
        ("ar", None, "ar"),
        ("ku", None, "ku"),
        ("ar-IQ", None, "ar"),
        ("ku-Arab-IQ", None, "ku"),
        ("EN_US", None, "en"),
        ("fr", None, "en"),  # unsupported → fallback
        ("", "ar", "ar"),
        (None, "ku", "ku"),
        (None, None, "en"),
        ("xx", "yy", "en"),
        ("fr", "ar", "ar"),  # device unsupported, user preferred wins
    ],
)
def test_resolve_locale(device_locale, user_pref, expected):
    assert resolve_locale(device_locale, user_pref) == expected


def test_default_lang_is_en():
    assert DEFAULT_LANG == "en"


def test_supported_langs():
    assert set(SUPPORTED_LANGS) == {"en", "ar", "ku"}


# ─── normalize_maps ─────────────────────────────────────────────────────────


def test_normalize_maps_lowercases_keys_and_normalizes():
    out = normalize_maps({"EN": "hello", "AR-iq": "مرحبا", "ku": "سڵاو"})
    assert out == {"en": "hello", "ar": "مرحبا", "ku": "سڵاو"}


def test_normalize_maps_drops_blanks_and_unsupported():
    out = normalize_maps({"en": " hi ", "ar": "  ", "fr": "bonjour", "es": ""})
    assert out == {"en": "hi"}


def test_normalize_maps_empty():
    assert normalize_maps({}) is None
    assert normalize_maps(None) is None
    assert normalize_maps({"fr": "x"}) is None  # all unsupported


# ─── pick_text ──────────────────────────────────────────────────────────────


def test_pick_text_returns_matching_lang():
    assert pick_text({"en": "Hello", "ar": "مرحبا"}, "fb", "ar") == "مرحبا"


def test_pick_text_falls_back_to_en_then_fallback():
    assert pick_text({"en": "Hello"}, "fb", "ku") == "Hello"
    assert pick_text({}, "fb", "ku") == "fb"
    assert pick_text(None, "fb", "ku") == "fb"


# ─── app_update_text ────────────────────────────────────────────────────────


def test_app_update_text_all_languages():
    for lang in SUPPORTED_LANGS:
        title, body = app_update_text(lang)
        assert title and body


def test_app_update_text_includes_pre_baked_copy():
    en_title, en_body = APP_UPDATE_MESSAGES["en"]
    assert "Update" in en_title
    assert "Tulip" in en_body


# ─── SendPushNotificationPayload validator ──────────────────────────────────


def test_send_payload_requires_targets():
    with pytest.raises(Exception):
        SendPushNotificationPayload(title="t", body="b")  # no audience / userIds / tokens


def test_send_payload_accepts_single_text():
    p = SendPushNotificationPayload(title="t", body="b", audience="all")
    assert p.titles is None and p.bodies is None


def test_send_payload_localized_must_have_both_maps():
    with pytest.raises(Exception):
        SendPushNotificationPayload(
            title="t",
            body="b",
            audience="all",
            titles={"en": "Hi", "ar": "Marhaba"},
            # no bodies
        )


def test_send_payload_localized_must_have_en():
    with pytest.raises(Exception):
        SendPushNotificationPayload(
            title="t",
            body="b",
            audience="all",
            titles={"ar": "Marhaba"},
            bodies={"ar": "Ahlan"},
        )


def test_send_payload_localized_normalizes_keys():
    p = SendPushNotificationPayload(
        title="t",
        body="b",
        audience="all",
        titles={"EN": "Hi", "AR-iq": "مرحبا"},
        bodies={"en": "Hello", "ar": "أهلا"},
    )
    assert "en" in (p.titles or {}) and "ar" in (p.titles or {})
    assert "en" in (p.bodies or {})


# ─── SendAppUpdateNotificationPayload validator ─────────────────────────────


def test_app_update_payload_minimal():
    p = SendAppUpdateNotificationPayload(
        appStoreUrl="https://apps.apple.com/x",
        playStoreUrl="https://play.google.com/x",
    )
    assert p.title is None and p.body is None
    assert p.audience == "all"


def test_app_update_payload_url_validation():
    with pytest.raises(Exception):
        SendAppUpdateNotificationPayload(
            appStoreUrl="ftp://bad",
            playStoreUrl="https://play.google.com/x",
        )
