from __future__ import annotations


def normalize_phone(phone: str) -> str:
    raw = (phone or "").strip()
    cleaned = (
        raw.replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )
    if cleaned.startswith("00"):
        cleaned = f"+{cleaned[2:]}"
    if cleaned and cleaned[0] != "+" and cleaned.isdigit() and cleaned.startswith("964"):
        cleaned = f"+{cleaned}"
    return cleaned


def phone_lookup_candidates(phone: str) -> list[str]:
    raw = (phone or "").strip()
    normalized = normalize_phone(raw)
    candidates: list[str] = []
    for value in (raw, normalized):
        if value and value not in candidates:
            candidates.append(value)
    if normalized.startswith("+") and normalized[1:] not in candidates:
        candidates.append(normalized[1:])
    if normalized.startswith("+964") and f"00{normalized[1:]}" not in candidates:
        candidates.append(f"00{normalized[1:]}")
    return candidates
