import time
import unittest
from pathlib import Path
from unittest import mock

from agent_web_fetch.cache import Cache

from helpers import TempHomeCase


def page(url="https://example.com/", backend="jina", **kw):
    p = {"url": url, "final_url": url, "backend": backend, "status": 200, "title": "T", "content": "body"}
    p.update(kw)
    return p


class CacheTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cache = Cache(Path(self.home) / "cache" / "cache.db")
        self.addCleanup(self.cache.close)

    def test_put_get_roundtrip(self):
        self.cache.put(page())
        got = self.cache.get("https://example.com/")
        self.assertEqual(got["content"], "body")
        self.assertEqual(got["backend"], "jina")
        self.assertEqual(got["status"], 200)
        self.assertIsNone(self.cache.get("https://example.com/other"))

    def test_ttl_expiry(self):
        self.cache.put(page(fetched_at=time.time() - 3700))
        self.assertIsNone(self.cache.get("https://example.com/"))
        self.assertIsNotNone(self.cache.get("https://example.com/", ttl=7200))

    def test_newer_than(self):
        self.cache.put(page(fetched_at=time.time() - 10))
        self.assertIsNone(self.cache.get("https://example.com/", newer_than=time.time() - 5))

    def test_mode_filter(self):
        self.cache.put(page(backend="jina"))
        self.assertIsNotNone(self.cache.get("https://example.com/", mode="auto"))
        self.assertIsNotNone(self.cache.get("https://example.com/", mode="jina"))
        self.assertIsNone(self.cache.get("https://example.com/", mode="browser"))

    def test_negative_cache(self):
        self.cache.put_error("https://example.com/", "auto", {"category": "blocked_by_site", "message": "x"})
        self.assertEqual(self.cache.get_error("https://example.com/", "auto")["category"], "blocked_by_site")
        self.assertIsNone(self.cache.get_error("https://example.com/", "jina"))
        self.assertIsNone(self.cache.get_error("https://example.com/", "auto", ttl=-1))
        self.cache.put(page())  # success clears errors
        self.assertIsNone(self.cache.get_error("https://example.com/", "auto"))

    def test_leases(self):
        other = Cache(self.cache.path)
        self.addCleanup(other.close)
        self.assertTrue(self.cache.try_acquire("k", 60))
        self.assertTrue(self.cache.try_acquire("k", 60))  # re-entrant for the same owner
        self.assertFalse(other.try_acquire("k", 60))
        self.cache.release("k")
        self.assertTrue(other.try_acquire("k", 60))
        other.release("k")

    def test_lease_expiry(self):
        other = Cache(self.cache.path)
        self.addCleanup(other.close)
        self.assertTrue(self.cache.try_acquire("k", 0.01))
        time.sleep(0.05)
        self.assertTrue(other.try_acquire("k", 60))

    def test_release_only_own_lease(self):
        other = Cache(self.cache.path)
        self.addCleanup(other.close)
        self.assertTrue(self.cache.try_acquire("k", 60))
        other.release("k")
        self.assertFalse(other.try_acquire("k", 60))

    def test_domain_pacing(self):
        self.assertEqual(self.cache.domain_wait("example.com"), 0.0)
        self.cache.set_domain("example.com", 30, backoff=20)
        self.assertGreater(self.cache.domain_wait("example.com"), 25)
        self.assertEqual(self.cache.domain_backoff("example.com"), 20)
        self.cache.set_domain("example.com", 1)  # never shortens an existing hold
        self.assertGreater(self.cache.domain_wait("example.com"), 25)
        self.assertEqual(self.cache.domain_backoff("example.com"), 20)


if __name__ == "__main__":
    unittest.main()
