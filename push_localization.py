"""Push notification localization helpers.

Single source of truth for:
- The pre-baked "app update available" message in EN / AR / KU.
- Locale normalization (e.g. "ar-IQ" -> "ar").
- Picking the right text per recipient when admin supplies localized maps.

The backend uses Firebase Cloud Messaging directly. FCM's native title_loc_key
mechanism requires translations to be bundled in the mobile app at build time;
since we want the admin to author custom messages on the fly, we instead resolve
the locale server-side and send one multicast per language with pre-translated
text.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

SupportedLang = Literal["en", "ar", "ku"]
SUPPORTED_LANGS: tuple[SupportedLang, ...] = ("en", "ar", "ku")
DEFAULT_LANG: SupportedLang = "en"


# Pre-baked update message. User-confirmed copy (see plan).
APP_UPDATE_MESSAGES: dict[SupportedLang, tuple[str, str]] = {
    "en": (
        "Update available 🌷",
        "A new version of Tulip is ready. Tap to update for the smoothest experience.",
    ),
    "ar": (
        "تحديث متوفر 🌷",
        "إصدار جديد من توليب جاهز. اضغط للتحديث للحصول على أفضل تجربة.",
    ),
    "ku": (
        "نوێکراوەیەک بەردەستە 🌷",
        "وەشانێکی نوێی Tulip ئامادەیە. کلیک بکە بۆ نوێکردنەوە.",
    ),
}


def _normalize(raw: str | None) -> str | None:
    """Lower-case + take the language subtag of a BCP-47-ish code.

    "ar-IQ"        -> "ar"
    "ku-Arab-IQ"   -> "ku"
    "EN_US"        -> "en"
    None / ""      -> None
    """
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    # Split on common separators; first segment is the language.
    for sep in ("-", "_", "."):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s or None


def resolve_locale(
    device_locale: str | None,
    user_pref: str | None = None,
) -> SupportedLang:
    """Pick the language to use for a single recipient.

    Priority: device.locale -> user.preferred_language -> DEFAULT_LANG.
    Anything outside SUPPORTED_LANGS falls back to DEFAULT_LANG.
    """
    for raw in (device_locale, user_pref):
        norm = _normalize(raw)
        if norm in SUPPORTED_LANGS:
            return norm  # type: ignore[return-value]
    return DEFAULT_LANG


def pick_text(
    maps: Mapping[str, str] | None,
    fallback: str,
    lang: SupportedLang,
) -> str:
    """Return maps[lang] if present and non-empty, else maps['en'], else fallback."""
    if maps:
        value = maps.get(lang)
        if value and str(value).strip():
            return str(value).strip()
        en = maps.get(DEFAULT_LANG)
        if en and str(en).strip():
            return str(en).strip()
    return fallback


def normalize_maps(maps: Mapping[str, Any] | None) -> dict[SupportedLang, str] | None:
    """Normalize a user-supplied {lang: text} map.

    - Lower-cases + normalizes language codes ("AR-iq" -> "ar").
    - Drops blank/empty values.
    - Keeps only supported languages.
    - Returns None if the result is empty.
    """
    if not maps:
        return None
    out: dict[SupportedLang, str] = {}
    for raw_key, raw_val in maps.items():
        key = _normalize(str(raw_key) if raw_key is not None else None)
        if key not in SUPPORTED_LANGS:
            continue
        if raw_val is None:
            continue
        val = str(raw_val).strip()
        if not val:
            continue
        out[key] = val  # type: ignore[index]
    return out or None


def app_update_text(lang: SupportedLang) -> tuple[str, str]:
    """Return (title, body) for the pre-baked update message in `lang`."""
    return APP_UPDATE_MESSAGES.get(lang, APP_UPDATE_MESSAGES[DEFAULT_LANG])
