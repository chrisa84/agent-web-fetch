import asyncio
import unittest
from pathlib import Path
from unittest import mock

import httpx

from agent_web_fetch import fetch as fetch_mod
from agent_web_fetch.backends import FetchError
from agent_web_fetch.backends import jina as jina_mod
from agent_web_fetch.cache import Cache

from helpers import TempHomeCase

GOOD = "Frogs are amphibians that live near ponds. " * 20


class FakeBackend:
    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.calls = 0

    async def __call__(self, url, **kw):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        r = {"final_url": url, "title": "Frogs", "content": GOOD, "status": 200}
        r.update(self.result or {})
        return r


class FetchTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.jina = FakeBackend()
        self.browser = FakeBackend()
        for target, fake in ((fetch_mod.jina_backend, self.jina), (fetch_mod.camoufox_backend, self.browser)):
            p = mock.patch.object(target, "fetch", fake)
            p.start()
            self.addCleanup(p.stop)

    def run_fetch(self, url="https://example.com/frogs", **kw):
        return asyncio.run(fetch_mod.fetch(url, **kw))

    def test_jina_success_skips_browser(self):
        r = self.run_fetch()
        self.assertIsNone(r["error"])
        self.assertEqual(r["backend"], "jina")
        self.assertFalse(r["cached"])
        self.assertEqual((self.jina.calls, self.browser.calls), (1, 0))

    def test_jina_challenge_falls_back_to_browser(self):
        self.jina.result = {"title": "Just a moment...", "content": "Checking your browser"}
        r = self.run_fetch()
        self.assertIsNone(r["error"])
        self.assertEqual(r["backend"], "browser")
        self.assertEqual((self.jina.calls, self.browser.calls), (1, 1))

    def test_jina_error_falls_back_to_browser(self):
        self.jina.error = FetchError("timeout", "Jina Reader timed out", transient=True)
        r = self.run_fetch()
        self.assertEqual(r["backend"], "browser")

    def test_both_fail_structured_error(self):
        self.jina.result = {"content": ""}
        self.browser.error = FetchError("browser_error", "navigation failed")
        r = self.run_fetch()
        self.assertEqual(r["error"]["category"], "browser_error")
        self.assertEqual([a["backend"] for a in r["error"]["attempts"]], ["jina", "browser"])
        self.assertIsNone(r["content"])

    def test_not_found_is_permanent(self):
        self.jina.result = {"status": 404}
        r = self.run_fetch()
        self.assertEqual(r["error"]["category"], "not_found")
        self.assertEqual(self.browser.calls, 0)

    def test_explicit_jina_never_uses_browser(self):
        self.jina.result = {"content": ""}
        r = self.run_fetch(backend="jina")
        self.assertEqual(r["error"]["category"], "empty_content")
        self.assertEqual(self.browser.calls, 0)

    def test_explicit_browser_skips_jina(self):
        r = self.run_fetch(backend="browser")
        self.assertEqual(r["backend"], "browser")
        self.assertEqual(self.jina.calls, 0)

    def test_second_call_served_from_cache(self):
        self.run_fetch()
        r = self.run_fetch("https://EXAMPLE.com:443/frogs#section")
        self.assertTrue(r["cached"])
        self.assertEqual(r["content"], GOOD.strip())
        self.assertEqual(self.jina.calls, 1)

    def test_refresh_bypasses_cache(self):
        self.run_fetch()
        r = self.run_fetch(refresh=True)
        self.assertFalse(r["cached"])
        self.assertEqual(self.jina.calls, 2)

    def test_negative_cache_prevents_hammering(self):
        self.jina.result = {"status": 404}
        self.run_fetch()
        r = self.run_fetch()
        self.assertTrue(r["cached"])
        self.assertEqual(r["error"]["category"], "not_found")
        self.assertEqual(self.jina.calls, 1)

    def test_invalid_and_blocked_urls_never_reach_backends(self):
        for url in ["file:///etc/passwd", "http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/"]:
            with self.subTest(url=url):
                r = self.run_fetch(url)
                self.assertIn(r["error"]["category"], ("invalid_url", "blocked_address"))
        self.assertEqual((self.jina.calls, self.browser.calls), (0, 0))

    def test_final_url_on_private_address_rejected(self):
        self.jina.result = {"final_url": "http://127.0.0.1/admin"}
        self.browser.result = {"final_url": "http://127.0.0.1/admin"}
        r = self.run_fetch()
        self.assertEqual(r["error"]["category"], "blocked_address")

    def test_rate_limit_hold_reported_without_fetching(self):
        cache = Cache(Path(self.home) / "cache" / "cache.db")
        self.addCleanup(cache.close)
        cache.set_domain("example.com", 600)
        r = self.run_fetch(cache=cache)
        self.assertEqual(r["error"]["category"], "rate_limited")
        self.assertGreater(r["error"]["retry_after"], 500)
        self.assertEqual(self.jina.calls, 0)

    def test_browser_429_holds_domain(self):
        self.jina.result = {"content": ""}
        self.browser.error = FetchError("rate_limited", "429", status=429, retry_after=120)
        self.run_fetch()
        cache = Cache(Path(self.home) / "cache" / "cache.db")
        self.addCleanup(cache.close)
        self.assertGreater(cache.domain_wait("example.com"), 100)

    def test_concurrent_duplicate_requests_fetch_once(self):
        self.jina.delay = 0.5
        db = Path(self.home) / "cache" / "cache.db"

        async def main():
            c1, c2 = Cache(db), Cache(db)
            try:
                return await asyncio.gather(fetch_mod.fetch("https://example.com/frogs", cache=c1),
                                            fetch_mod.fetch("https://example.com/frogs", cache=c2))
            finally:
                c1.close()
                c2.close()

        r1, r2 = asyncio.run(main())
        self.assertEqual(self.jina.calls, 1)
        self.assertIsNone(r1["error"])
        self.assertIsNone(r2["error"])
        self.assertEqual(sorted([r1["cached"], r2["cached"]]), [False, True])


class JinaBackendTests(unittest.TestCase):
    def run_with(self, handler, **kw):
        real = httpx.AsyncClient

        def client(**ckw):
            ckw["transport"] = httpx.MockTransport(handler)
            return real(**ckw)

        with mock.patch.object(jina_mod.httpx, "AsyncClient", client), \
                mock.patch.object(jina_mod.asyncio, "sleep", mock.AsyncMock()):
            return asyncio.run(jina_mod.fetch("https://example.com/a", **kw))

    def test_success_and_auth_header(self):
        seen = {}

        def handler(req):
            seen["url"] = str(req.url)
            seen["auth"] = req.headers.get("authorization")
            return httpx.Response(200, json={"code": 200, "data": {
                "title": "A", "url": "https://example.com/b", "content": "hello"}})

        r = self.run_with(handler, api_key="k")
        self.assertEqual(seen["url"], "https://r.jina.ai/https://example.com/a")
        self.assertEqual(seen["auth"], "Bearer k")
        self.assertEqual(r, {"final_url": "https://example.com/b", "title": "A", "content": "hello", "status": 200})

    def test_no_key_no_auth_header(self):
        def handler(req):
            assert "authorization" not in req.headers
            return httpx.Response(200, json={"data": {"title": "", "url": "https://example.com/a", "content": "x"}})

        self.run_with(handler)

    def test_target_status_from_warning(self):
        def handler(req):
            return httpx.Response(200, json={"data": {"title": "", "url": "https://example.com/a", "content": "x",
                                                      "warning": "Target URL returned error 403: Forbidden"}})

        self.assertEqual(self.run_with(handler)["status"], 403)

    def test_429_not_retried(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})

        with self.assertRaises(FetchError) as cm:
            self.run_with(handler)
        self.assertEqual(cm.exception.category, "rate_limited")
        self.assertEqual(cm.exception.retry_after, 30.0)
        self.assertEqual(len(calls), 1)

    def test_transient_retried_once(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(503, text="busy")

        with self.assertRaises(FetchError):
            self.run_with(handler)
        self.assertEqual(len(calls), 2)

    def test_error_payload(self):
        def handler(req):
            return httpx.Response(422, json={"data": None, "readableMessage": "Target URL returned error 404"})

        with self.assertRaises(FetchError) as cm:
            self.run_with(handler)
        self.assertEqual(cm.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
