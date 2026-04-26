from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock

from esim_access_api import ESimAccessAPI, PackageQueryRequest


class PackagesQueryCacheTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for name in (
            "ESIM_PACKAGES_CACHE_TTL_SECONDS",
            "ESIM_PACKAGES_CACHE_MAX_ENTRIES",
        ):
            os.environ.pop(name, None)

    async def test_get_packages_reuses_cached_response_for_identical_payload(self) -> None:
        os.environ["ESIM_PACKAGES_CACHE_TTL_SECONDS"] = "20"
        provider = ESimAccessAPI(access_code="code", secret_key="secret")
        provider._post = AsyncMock(side_effect=[{"obj": {"items": [1]}}, {"obj": {"items": [2]}}])  # type: ignore[method-assign]

        try:
            payload = PackageQueryRequest(location_code="IQ", type="COUNTRY")
            response_one = await provider.get_packages(payload)
            response_two = await provider.get_packages(payload)
            self.assertEqual(provider._post.await_count, 1)
            self.assertEqual(response_one, response_two)
        finally:
            await provider.close()

    async def test_get_packages_bypasses_cache_when_ttl_is_disabled(self) -> None:
        os.environ["ESIM_PACKAGES_CACHE_TTL_SECONDS"] = "0"
        provider = ESimAccessAPI(access_code="code", secret_key="secret")
        provider._post = AsyncMock(side_effect=[{"obj": {"items": [1]}}, {"obj": {"items": [2]}}])  # type: ignore[method-assign]

        try:
            payload = PackageQueryRequest(location_code="IQ", type="COUNTRY")
            response_one = await provider.get_packages(payload)
            response_two = await provider.get_packages(payload)
            self.assertEqual(provider._post.await_count, 2)
            self.assertNotEqual(response_one, response_two)
        finally:
            await provider.close()

    async def test_get_packages_cache_key_changes_with_payload(self) -> None:
        os.environ["ESIM_PACKAGES_CACHE_TTL_SECONDS"] = "20"
        provider = ESimAccessAPI(access_code="code", secret_key="secret")
        provider._post = AsyncMock(side_effect=[{"obj": {"country": "IQ"}}, {"obj": {"country": "TR"}}])  # type: ignore[method-assign]

        try:
            iq_payload = PackageQueryRequest(location_code="IQ", type="COUNTRY")
            tr_payload = PackageQueryRequest(location_code="TR", type="COUNTRY")
            await provider.get_packages(iq_payload)
            await provider.get_packages(tr_payload)
            self.assertEqual(provider._post.await_count, 2)
        finally:
            await provider.close()


if __name__ == "__main__":
    unittest.main()

