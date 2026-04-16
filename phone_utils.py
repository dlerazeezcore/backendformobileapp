from __future__ import annotations


def normalize_phone(phone: str) -> str:
    raw = (phone or "").strip()
    if not raw:
        return ""

    has_plus_prefix = raw.startswith("+")
    digits_only = "".join(ch for ch in raw if ch.isdigit())
    if not digits_only:
        return ""

    if has_plus_prefix:
        cleaned = f"+{digits_only}"
    elif digits_only.startswith("00"):
        cleaned = f"+{digits_only[2:]}"
    elif digits_only.startswith("964"):
        cleaned = f"+{digits_only}"
    elif digits_only.startswith("07") and len(digits_only) == 11:
        # Iraqi local mobile input like 0750xxxxxxx -> +964750xxxxxxx
        cleaned = f"+964{digits_only[1:]}"
    else:
        cleaned = digits_only

    if cleaned.startswith("+9640"):
        # Remove trunk zero after Iraqi country code: +964075... -> +96475...
        cleaned = f"+964{cleaned[5:]}"
    return cleaned


def phone_lookup_candidates(phone: str) -> list[str]:
    raw = (phone or "").strip()
    normalized = normalize_phone(raw)
    candidates: list[str] = []

    def _append(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    _append(raw)
    _append(normalized)

    if normalized.startswith("+"):
        normalized_digits = normalized[1:]
        _append(normalized_digits)
        _append(f"00{normalized_digits}")

    if normalized.startswith("+964"):
        local_without_country = normalized[4:]
        if local_without_country:
            _append(f"0{local_without_country}")
            _append(f"+9640{local_without_country}")
            _append(f"9640{local_without_country}")

    return candidates
