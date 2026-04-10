from __future__ import annotations

import unittest

from supabase_store import normalize_usage_pair_to_mb


class UsageNormalizationTest(unittest.TestCase):
    def test_explicit_bytes_hint_converts_to_mb(self) -> None:
        total, used, detected = normalize_usage_pair_to_mb(
            total_raw=104857600,
            used_raw=1048576,
            unit_hint="bytes",
        )
        self.assertEqual(detected, "bytes")
        self.assertEqual(total, 100)
        self.assertEqual(used, 1)

    def test_explicit_kb_hint_converts_to_mb(self) -> None:
        total, used, detected = normalize_usage_pair_to_mb(
            total_raw=102400,
            used_raw=2048,
            unit_hint="KB",
        )
        self.assertEqual(detected, "kb")
        self.assertEqual(total, 100)
        self.assertEqual(used, 2)

    def test_heuristic_detects_kb_when_large_without_hint(self) -> None:
        total, used, detected = normalize_usage_pair_to_mb(
            total_raw=102400,
            used_raw=2048,
            unit_hint=None,
        )
        self.assertEqual(detected, "kb")
        self.assertEqual(total, 100)
        self.assertEqual(used, 2)


if __name__ == "__main__":
    unittest.main()
